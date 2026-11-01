"""Локальный эквивалент CI без загрузки весов и запуска Prefect-сервера."""
import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(command, cwd=ROOT):
    print('$', ' '.join(map(str, command)), flush=True)
    result = subprocess.run(command, cwd=cwd, env={**os.environ, 'PYTHONUTF8': '1'})
    if result.returncode:
        raise SystemExit(result.returncode)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--intentional-failure', action='store_true')
    args = parser.parse_args()
    if args.intentional_failure:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (ROOT / 'release.py').read_text(encoding='utf-8')
            original = "return sha256(canonical({'data_sha256': sha256(Path(data_path).read_bytes()), 'config': config}))"
            assert original in source
            (root / 'release.py').write_text(source.replace(original, "return '0' * 64"), encoding='utf-8')
            (root / 'tests').mkdir()
            (root / 'tests/test_release.py').write_bytes((ROOT / 'tests/test_release.py').read_bytes())
            print('Изолированная копия: намеренно сломан ключ кеша; исходный проект не изменён.', flush=True)
            run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], root)
        return
    files = sorted(p.name for p in ROOT.glob('*.py'))
    run([sys.executable, '-m', 'compileall', '-q', *files, 'tests', 'integration_tests'])
    run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'])
    run([sys.executable, '-m', 'unittest', 'discover', '-s', 'integration_tests', '-v'])
    digest = hashlib.sha256()
    for path in sorted((ROOT / 'tests').glob('*')):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    print('Отпечаток исходных tests/:', digest.hexdigest()[:12], flush=True)
    print('PASS: компиляция, 13 исходных и 13 новых тестов. Веса не загружались.', flush=True)


if __name__ == '__main__':
    main()
