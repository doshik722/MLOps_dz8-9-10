"""Ключ подготовки, строгий контракт выпуска и атомарная запись указателя."""
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

HASH_FIELDS = ('model_sha256', 'protocol_sha256', 'golden_sha256', 'runtime_sha256')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def cache_key(data_path: Path, config: dict) -> str:
    return sha256(canonical({'data_sha256': sha256(Path(data_path).read_bytes()), 'config': config}))


def validate_release(manifest: dict, report: dict) -> None:
    if not isinstance(manifest, dict) or not isinstance(report, dict):
        raise ValueError('Manifest и report должны быть объектами')
    if type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1:
        raise ValueError('Неподдерживаемая schema_version')
    for key in ('model_id', 'artifact_uri'):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ValueError(f'Отсутствует {key}')
    for key in HASH_FIELDS:
        if not isinstance(manifest.get(key), str) or not re.fullmatch('[0-9a-f]{64}', manifest[key]):
            raise ValueError(f'Некорректный {key}')
    runtime = manifest.get('runtime')
    if not isinstance(runtime, dict) or not runtime or sha256(canonical(runtime)) != manifest['runtime_sha256']:
        raise ValueError('Runtime не соответствует своему SHA-256')
    if report.get('status') != 'complete' or report.get('passed') is not True:
        raise ValueError('Неполная оценка или кандидат отклонён')
    if not isinstance(report.get('evaluation_run_id'), str) or not report['evaluation_run_id'].strip():
        raise ValueError('Отсутствует evaluation_run_id')
    for key in ('model_id', *HASH_FIELDS):
        if report.get(key) != manifest[key]:
            raise ValueError(f'Отчёт не связан с manifest: {key}')
    metrics = report.get('metrics')
    if not isinstance(metrics, dict):
        raise ValueError('Отсутствуют метрики')
    for key in ('accuracy', 'format_rate'):
        value = metrics.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f'Некорректная метрика {key}')


def atomic_bytes(target: Path, payload: bytes) -> None:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() == payload:
        return
    descriptor, temporary = tempfile.mkstemp(prefix=target.name + '.', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(target, value):
    atomic_bytes(Path(target), canonical(value) + b'\n')


def publish(manifest: dict, report: dict, target: Path) -> None:
    validate_release(manifest, report)
    write_json(target, manifest)


def verify_files(manifest, workspace):
    """Проверка фактических байтов перед выпуском и перед загрузкой backend."""
    hashes = manifest.get('file_hashes')
    if not isinstance(hashes, dict) or not hashes or sha256(canonical(hashes)) != manifest['model_sha256']:
        raise ValueError('Нет закреплённого списка файлов или неверен model_sha256')
    artifact = Path(manifest['artifact_uri'])
    artifact = artifact if artifact.is_absolute() else Path(workspace) / artifact
    root = (artifact if artifact.is_dir() else artifact.parent).resolve()
    for name, expected in hashes.items():
        if not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('Некорректный относительный путь артефакта')
        if file_sha256(root / name) != expected:
            raise ValueError(f'Изменён файл артефакта: {name}')
    if manifest.get('kind') == 'adapter':
        if not isinstance(manifest.get('base_manifest'), dict):
            raise ValueError('Не закреплён артефакт базовой модели адаптера')
        verify_files(manifest['base_manifest'], workspace)
    return artifact


def release_id(manifest, report):
    return sha256(canonical({'manifest': manifest, 'decision': report}))


def read_events(root):
    path = Path(root) / 'publications.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()] if path.exists() else []


def recover_publication(root):
    root = Path(root)
    pending = root / 'pending-publication.json'
    if not pending.exists():
        return
    intent = json.loads(pending.read_text(encoding='utf-8'))
    target = root / 'accepted.json'
    if target.exists() and json.loads(target.read_text(encoding='utf-8')) == intent['manifest']:
        rows = read_events(root)
        if not any(row['release_id'] == intent['event']['release_id'] for row in rows):
            rows.append(intent['event'])
            atomic_bytes(root / 'publications.jsonl', b''.join(canonical(row) + b'\n' for row in rows))
    pending.unlink()


def journaled_publish(manifest, report, workspace, fail_after_pointer=False):
    """Один писатель: durable intent связывает accepted.json с журналом событий."""
    root = Path(workspace)
    validate_release(manifest, report)
    verify_files(manifest, root)
    recover_publication(root)
    identity = release_id(manifest, report)
    target = root / 'accepted.json'
    if target.exists() and json.loads(target.read_text(encoding='utf-8')) == manifest:
        if any(row['release_id'] == identity for row in read_events(root)):
            return {'release_id': identity, 'published': False}
    event = {'release_id': identity, 'model_id': manifest['model_id'],
             'runtime_sha256': manifest['runtime_sha256'], 'evaluation_run_id': report['evaluation_run_id']}
    write_json(root / 'pending-publication.json', {'manifest': manifest, 'event': event})
    try:
        publish(manifest, report, target)
    except Exception:
        (root / 'pending-publication.json').unlink(missing_ok=True)
        raise
    if fail_after_pointer:
        raise RuntimeError('Инъекция сбоя между accepted.json и журналом')
    recover_publication(root)
    return {'release_id': identity, 'published': True}
