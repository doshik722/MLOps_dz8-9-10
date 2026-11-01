"""Регрессии семи инцидентов: настоящие файлы, subprocess и localhost HTTP."""
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get('HW8910_TARGET', Path(__file__).resolve().parents[1])).resolve()


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / 'workspace'
        self.command('init', ok=True)

    def command(self, *args, ok=False):
        result = subprocess.run([sys.executable, str(ROOT / 'lab.py'), '--workspace', str(self.workspace), *args],
                                cwd=ROOT, capture_output=True, text=True, encoding='utf-8',
                                env={**os.environ, 'PYTHONUTF8': '1'}, timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def read(self, name):
        return json.loads((self.workspace / name).read_text(encoding='utf-8'))

    def test_subprocess_cache_recovery_and_changed_bytes(self):
        failed = self.command('run', '--fail-after-prepare')
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('PREPARED', failed.stdout)
        resumed = self.command('run', ok=True)
        self.assertIn('CACHE_HIT', resumed.stdout)
        (self.workspace / 'input.txt').write_text('Изменённые байты по прежнему пути\n', encoding='utf-8')
        changed = self.command('run', ok=True)
        self.assertIn('PREPARED', changed.stdout, 'После изменения входа использован прежний кеш')

    def test_subprocess_preparation_config_invalidates(self):
        self.command('run', ok=True)
        for key, value in [('tokenizer_revision', 'r2'), ('template_sha256', 'f' * 64),
                           ('max_length', 256), ('code_revision', 'prepare-v2')]:
            with self.subTest(setting=key):
                config = self.read('config.json')
                config[key] = value
                (self.workspace / 'config.json').write_text(json.dumps(config), encoding='utf-8')
                self.assertIn('PREPARED', self.command('run', ok=True).stdout)

    def test_subprocess_rejects_incomplete_and_foreign_report(self):
        pointer = self.workspace / 'accepted.json'
        before = hashlib.sha256(pointer.read_bytes()).hexdigest()
        for report in ['report-incomplete.json', 'report-A.json', 'report-reject.json']:
            with self.subTest(report=report):
                result = self.command('run', '--report', report)
                self.assertNotEqual(result.returncode, 0, 'Несовместимый отчёт получил выпуск')
                self.assertEqual(hashlib.sha256(pointer.read_bytes()).hexdigest(), before)

    def test_subprocess_repeat_creates_one_publication(self):
        self.command('run', ok=True)
        before = (self.workspace / 'accepted.json').read_bytes()
        self.command('run', ok=True)
        self.assertEqual((self.workspace / 'accepted.json').read_bytes(), before)
        rows = (self.workspace / 'publications.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(rows), 1, 'Повтор в новом процессе размножил публикации')

    def test_subprocess_atomic_replace_failure_preserves_pointer(self):
        code = '''import json,sys
from pathlib import Path
from unittest.mock import patch
import release
root=Path(sys.argv[1]);target=root/'accepted.json';before=target.read_bytes()
manifest=json.loads((root/'manifest-B.json').read_text(encoding='utf-8'))
report=json.loads((root/'report-B.json').read_text(encoding='utf-8'))
caught=False
with patch('os.replace',side_effect=OSError('injected disk failure')):
    try: release.publish(manifest,report,target)
    except OSError: caught=True
assert caught, 'Ошибка атомарной замены не проброшена'
assert target.read_bytes()==before, 'Старый указатель изменён при ошибке диска'
assert not list(root.glob('*.tmp'))
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.workspace)], cwd=ROOT,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def start_server(self, warmup=0):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        log = tempfile.TemporaryFile()
        process = subprocess.Popen([sys.executable, str(ROOT / 'service.py'), '--workspace', str(self.workspace),
                                    '--port', str(port), '--warmup-seconds', str(warmup)], cwd=ROOT,
                                   stdout=log, stderr=log, env={**os.environ, 'PYTHONUTF8': '1'})

        def stop():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            log.close()
        self.addCleanup(stop)
        self.stop_server = stop
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log.seek(0)
                self.fail('Сервис завершился: ' + log.read().decode('utf-8', errors='replace'))
            try:
                if self.http('/health')[0] == 200:
                    return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(.03)
        self.fail('Сервис не открыл HTTP за 20 секунд')

    def http(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.url + path, data=data, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def wait_ready(self):
        for _ in range(150):
            if self.http('/ready')[0] == 200:
                return
            time.sleep(.03)
        self.fail('Прогрев не завершён')

    def test_http_not_ready_before_warmup(self):
        self.start_server(warmup=2)
        self.assertFalse(self.http('/debug')[1]['warmup_finished'])
        self.assertEqual(self.http('/ready')[0], 503)
        before = self.http('/debug')[1]['calls']
        self.assertEqual(self.http('/generate', {'prompt': 'Пример', 'max_new_tokens': 32})[0], 503)
        self.assertEqual(self.http('/debug')[1]['calls'], before)
        self.wait_ready()
        self.assertEqual(self.http('/generate', {'prompt': 'Пример', 'max_new_tokens': 32})[0], 200)

    def test_http_loaded_version_and_rollback(self):
        self.start_server()
        self.wait_ready()
        request = {'prompt': 'Пример', 'max_new_tokens': 32}
        self.assertEqual(self.http('/generate', request)[1]['model_id'], 'fixture-A')
        self.command('run', ok=True)
        old = self.http('/generate', request)[1]
        self.assertEqual(old['model_id'], 'fixture-A', 'Указатель изменился, а backend ещё не перезагружен')
        self.assertIn('fixture-A', old['answer'])
        self.stop_server()
        self.start_server()
        self.wait_ready()
        new = self.http('/generate', request)[1]
        self.assertEqual(new['model_id'], 'fixture-B')
        self.assertIn('fixture-B', new['answer'])
        rollback = '''import json,sys
from pathlib import Path
import release
root=Path(sys.argv[1])
release.publish(json.loads((root/'manifest-A.json').read_text(encoding='utf-8')),
                json.loads((root/'report-A.json').read_text(encoding='utf-8')),root/'accepted.json')
'''
        result = subprocess.run([sys.executable, '-c', rollback, str(self.workspace)], cwd=ROOT,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.stop_server()
        self.start_server()
        self.wait_ready()
        self.assertEqual(self.http('/generate', request)[1]['model_id'], 'fixture-A')

    def test_http_invalid_request_never_calls_backend(self):
        self.start_server()
        self.wait_ready()
        invalid = [{'prompt': '', 'max_new_tokens': 32}, {'prompt': 'x', 'max_new_tokens': 0},
                   {'prompt': 'x', 'max_new_tokens': 100000}, {'prompt': 'x', 'max_new_tokens': '32'},
                   {'prompt': 'x', 'max_new_tokens': True}, {'prompt': ' '}, {'prompt': 'x' * 4097}]
        for payload in invalid:
            with self.subTest(payload=str(payload)[:80]):
                before = self.http('/debug')[1]['calls']
                status, _ = self.http('/generate', payload)
                self.assertTrue(400 <= status < 500, f'Некорректный запрос обслужен: {status}')
                self.assertEqual(self.http('/debug')[1]['calls'], before)
        status, result = self.http('/generate', {'prompt': 'Нормальный запрос', 'max_new_tokens': 32})
        self.assertEqual(status, 200)
        self.assertTrue(result['request_id'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
