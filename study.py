"""Реальный CPU smoke, повторная загрузка и одинаковые 8+8 HTTP-запросов."""
import concurrent.futures
import json
import os
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import release

ROOT = Path(__file__).resolve().parent
EVIDENCE = ROOT / 'evidence'
WORKSPACE = ROOT / '.lab-real'


def request(base, path, payload=None, timeout=90):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(base + path, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def start_server(label):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    log = (EVIDENCE / (label + '-server.log')).open('w', encoding='utf-8')
    process = subprocess.Popen([sys.executable, 'service.py', '--workspace', str(WORKSPACE), '--backend', 'real',
                                '--port', str(port), '--warmup-seconds', '0', '--debug'], cwd=ROOT,
                               stdout=log, stderr=subprocess.STDOUT,
                               env={**os.environ, 'PYTHONUTF8': '1', 'HF_HUB_OFFLINE': '1'})
    states = []
    deadline = time.monotonic() + 180
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('API завершился при загрузке; см. ' + label + '-server.log')
            try:
                health = request(base, '/health', timeout=2)
                ready = request(base, '/ready', timeout=2)
                if not states or states[-1]['ready_status'] != ready[0]:
                    states.append({'health_status': health[0], 'ready_status': ready[0], 'ready': ready[1]})
                if ready[0] == 200:
                    return base, process, log, states
                if isinstance(ready[1].get('detail'), str) and 'Error:' in ready[1]['detail']:
                    raise RuntimeError(ready[1]['detail'])
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(.1)
        raise TimeoutError('API не завершил загрузку за 180 секунд')
    except Exception:
        stop_server(process, log)
        raise


def stop_server(process, log):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    log.close()


def main():
    EVIDENCE.mkdir(exist_ok=True)
    golden = [json.loads(line) for line in (WORKSPACE / 'hw67/golden.jsonl').read_text(encoding='utf-8').splitlines()]
    expected = {row['id']: row['raw'] for row in json.loads((WORKSPACE / 'hw67/baseline-outputs.json').read_text(encoding='utf-8'))}
    examples = [golden[i] for i in [0, 1, 17, 18, 34, 35, 2, 19]]
    baseline = json.loads((WORKSPACE / 'accepted.json').read_text(encoding='utf-8'))
    base, process, log, states = start_server('real')
    all_rows, summary = [], []
    try:
        status, smoke = request(base, '/generate', {'prompt': examples[0]['input'], 'max_new_tokens': 32})
        assert status == 200 and smoke['model_id'] == baseline['model_id']
        assert smoke['runtime_sha256'] == baseline['runtime_sha256']
        assert smoke['answer'] == expected[examples[0]['id']], 'Smoke не совпал с исходной оценкой baseline'
        invalid = []
        for payload in [{'prompt': ''}, {'prompt': 'x', 'max_new_tokens': 0},
                        {'prompt': 'x', 'max_new_tokens': 100000}, {'prompt': 'x', 'max_new_tokens': '32'},
                        {'prompt': 'x', 'max_new_tokens': 1}, {'prompt': 'x' * 4097}]:
            before = request(base, '/debug')[1]['calls']
            code, body = request(base, '/generate', payload)
            after = request(base, '/debug')[1]['calls']
            assert 400 <= code < 500 and before == after
            invalid.append({'payload': payload if len(payload.get('prompt', '')) < 100 else {'prompt_chars': 4097},
                            'status': code, 'before_calls': before, 'after_calls': after, 'response': body})
        release.write_json(EVIDENCE / 'real-smoke.json', {'readiness': states, 'status': status, 'response': smoke,
            'matches_hw67_baseline_output': True, 'invalid_requests': invalid})

        def one(item, concurrency):
            started = time.perf_counter()
            row = {'client_id': str(uuid.uuid4()), 'example_id': item['id'], 'concurrency': concurrency,
                   'prompt': item['input'], 'max_new_tokens': 32}
            try:
                code, result = request(base, '/generate', {'prompt': item['input'], 'max_new_tokens': 32})
                row.update(status=code, response=result, error=None if code == 200 else str(result))
            except Exception as exc:
                row.update(status=None, response=None, error=f'{type(exc).__name__}: {exc}')
            row['client_seconds'] = time.perf_counter() - started
            return row

        for concurrency in [1, 2]:
            started = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                rows = list(pool.map(lambda item: one(item, concurrency), examples))
            duration = time.perf_counter() - started
            all_rows.extend(rows)
            good = [row for row in rows if row['status'] == 200]
            errors = [row for row in rows if row['status'] != 200]
            for row in good:
                assert row['response']['model_id'] == baseline['model_id']
                assert row['response']['runtime_sha256'] == baseline['runtime_sha256']
                assert row['response']['answer'] == expected[row['example_id']], 'Ответ отличается от исходного baseline'
            summary.append({'concurrency': concurrency, 'n': len(rows), 'successes': len(good), 'errors': len(errors),
                'batch_seconds': duration, 'successful_req_per_sec': len(good) / duration,
                'mean_client_seconds': statistics.mean(row['client_seconds'] for row in good) if good else None,
                'max_client_seconds': max((row['client_seconds'] for row in good), default=None),
                'mean_error_seconds': statistics.mean(row['client_seconds'] for row in errors) if errors else None,
                'max_error_seconds': max((row['client_seconds'] for row in errors), default=None),
                'mean_queue_seconds': statistics.mean(row['response']['queue_seconds'] for row in good) if good else None,
                'mean_generation_seconds': statistics.mean(row['response']['generation_seconds'] for row in good) if good else None,
                'output_tokens': [row['response']['output_tokens'] for row in good]})
            print('BENCHMARK', json.dumps(summary[-1], ensure_ascii=False), flush=True)
            release.atomic_bytes(EVIDENCE / 'requests.jsonl', b''.join(release.canonical(row) + b'\n' for row in all_rows))
            release.write_json(EVIDENCE / 'benchmark.json', summary)
    finally:
        stop_server(process, log)
    base, process, log, states = start_server('real-restart')
    try:
        status, response = request(base, '/generate', {'prompt': examples[0]['input'], 'max_new_tokens': 32})
        assert status == 200 and response['model_id'] == smoke['model_id']
        assert response['runtime_sha256'] == smoke['runtime_sha256'] and response['answer'] == smoke['answer']
        release.write_json(EVIDENCE / 'real-restart.json', {'readiness': states, 'response': response,
                                                          'same_model_runtime_and_answer': True})
    finally:
        stop_server(process, log)
    print('PASS: реальная генерация, 8+8 запросов, неправильные запросы и повторная загрузка.', flush=True)


if __name__ == '__main__':
    main()
