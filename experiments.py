"""Автоматические опыты Prefect и отрицательные проверки выпуска с реальными журналами."""
import copy
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import lab
import release

ROOT = Path(__file__).resolve().parent
EVIDENCE = ROOT / 'evidence'


def command(name, args, expected=0):
    path = EVIDENCE / (name + '.log')
    print('RUN', name, flush=True)
    with path.open('w', encoding='utf-8') as stream:
        stream.write('$ ' + ' '.join(args) + '\n')
        stream.flush()
        result = subprocess.run([sys.executable, *args], cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                env={**os.environ, 'PYTHONUTF8': '1', 'PREFECT_SERVER_ANALYTICS_ENABLED': 'false'}, timeout=180)
        stream.write(f'\nEXIT_CODE={result.returncode}\n')
    if result.returncode != expected:
        raise RuntimeError(f'{name}: exit={result.returncode}, ожидался {expected}; {path}')
    return {'command': args, 'exit_code': result.returncode, 'log': path.relative_to(ROOT).as_posix()}


def latest_attempt(root):
    path = max((root / 'flow-attempts').glob('*.json'), key=lambda p: p.stat().st_mtime_ns)
    return lab.read(path)


def flow_case(name, root, flags=(), expected=0, synthetic=True):
    args = ['flow.py', '--workspace', str(root)] + (['--synthetic'] if synthetic else []) + list(flags)
    result = command(name, args, expected)
    result['attempt'] = latest_attempt(root)
    result['publications'] = release.read_events(root)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prefix', default='.lab-experiments')
    args = parser.parse_args()
    EVIDENCE.mkdir(exist_ok=True)
    root = ROOT / (args.prefix + '-recovery')
    lab.initialize(root)
    config = lab.read(root / 'config.json')
    config['experiment'] = 'prefect-recovery-v1'
    release.write_json(root / 'config.json', config)
    results = []
    results.append(flow_case('flow-failure-after-prepare', root, ['--fail-after-prepare'], 1))
    results.append(flow_case('flow-resume', root))
    results.append(flow_case('flow-repeat', root))
    (root / 'input.txt').write_text('Новый вход по прежнему пути\n', encoding='utf-8')
    results.append(flow_case('flow-changed-input', root))
    config['max_length'] = 256
    release.write_json(root / 'config.json', config)
    results.append(flow_case('flow-changed-config', root))
    states = [row['attempt']['preparation_state'] for row in results]
    assert states == ['Completed', 'Cached', 'Cached', 'Completed', 'Completed'], states
    assert len(release.read_events(root)) == 1

    crash = ROOT / (args.prefix + '-publication-crash')
    lab.initialize(crash)
    results.append(flow_case('flow-crash-after-pointer', crash, ['--fail-after-publish'], 1))
    results.append(flow_case('flow-recover-publication', crash))
    results.append(flow_case('flow-repeat-publication', crash))
    assert len(release.read_events(crash)) == 1

    negative = ROOT / (args.prefix + '-negative')
    lab.initialize(negative)
    manifest = lab.read(negative / 'manifest-B.json')
    valid = lab.read(negative / 'report-B.json')
    reports = {'incomplete': {**valid, 'status': 'running'},
               'foreign': {**valid, 'model_id': 'fixture-A'},
               'rejected': {**valid, 'passed': False},
               'nan': {**valid, 'metrics': {'accuracy': float('nan'), 'format_rate': 1.0}}}
    negatives = []
    for name, report in reports.items():
        before = release.file_sha256(negative / 'accepted.json')
        try:
            release.journaled_publish(manifest, report, negative)
            raise AssertionError('Неверный отчёт принят')
        except ValueError as exc:
            reason = str(exc)
        after = release.file_sha256(negative / 'accepted.json')
        assert before == after
        negatives.append({'case': name, 'before': before, 'after': after, 'reason': reason})
    before = release.file_sha256(negative / 'accepted.json')
    with patch('os.replace', side_effect=OSError('injected atomic replace failure')):
        try:
            release.publish(manifest, valid, negative / 'accepted.json')
            raise AssertionError('Ожидалась ошибка диска')
        except OSError as exc:
            reason = str(exc)
    after = release.file_sha256(negative / 'accepted.json')
    assert before == after and not list(negative.glob('*.tmp'))
    negatives.append({'case': 'disk-replace-error', 'before': before, 'after': after, 'reason': reason})
    real = ROOT / '.lab-real'
    results.append(flow_case('real-flow-rejected', real, synthetic=False))
    assert results[-1]['attempt']['result']['status'] == 'rejected'
    assert results[-1]['attempt']['accepted_before'] == results[-1]['attempt']['accepted_after']
    release.write_json(EVIDENCE / 'flow-experiments.json', results)
    release.write_json(EVIDENCE / 'negative-releases.json', negatives)
    print('PASS: восстановление, Cached/Completed, инвалидация, одна публикация и сохранённый отказ.', flush=True)


if __name__ == '__main__':
    main()
