"""Подключение существующего HW67: копируются отчёты, веса остаются в исходных хранилищах."""
import argparse
import importlib.metadata
import json
import shutil
import subprocess
from pathlib import Path

import backend
import bridge
import release


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def collect(root, names=None):
    paths = [p for p in Path(root).iterdir() if p.is_file() and p.suffix in ('.json', '.safetensors', '.txt', '.jinja')]
    if names is not None:
        paths = [p for p in paths if p.name in names]
    return {p.name: release.file_sha256(p) for p in sorted(paths)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=Path('../mlops26-hw6-7/hw67-broken'))
    parser.add_argument('--workspace', type=Path, default=Path('.lab-real'))
    args = parser.parse_args()
    source = args.source.resolve()
    root = args.workspace.resolve()
    if root.exists():
        raise FileExistsError('Рабочий каталог уже существует; выбранные версии не перезаписываются')
    root.mkdir(parents=True)
    exported = root / 'hw67'
    exported.mkdir()
    files = {
        **{name: source / 'metrics' / name for name in ['baseline-eval.json', 'candidate-eval.json',
            'baseline-outputs.json', 'candidate-outputs.json', 'eval-diff.json', 'quality-gate.json',
            'candidate-model.json', 'sweep.json', 'offline-import.json']},
        **{name: source / 'data/eval' / name for name in ['golden.jsonl', 'behavioral.jsonl', 'training-index.jsonl']},
        'params.json': source / 'evaluation/params.json', 'baseline.json': source / 'evaluation/baseline.json'}
    for name, path in files.items():
        shutil.copyfile(path, exported / name)
    (exported / 'evaluation-code').mkdir()
    for path in (source / 'evaluation').glob('*.py'):
        shutil.copyfile(path, exported / 'evaluation-code' / path.name)
    candidate = read(exported / 'candidate-model.json')
    baseline = read(exported / 'baseline.json')
    evaluation = read(exported / 'candidate-eval.json')
    config = read(exported / 'params.json')
    package_names = ['torch', 'transformers', 'tokenizers', 'huggingface-hub', 'peft', 'numpy', 'safetensors', 'accelerate']
    source_python = source / '.venv/Scripts/python.exe'
    if not source_python.exists():
        source_python = source / '.venv/bin/python'
    libraries = json.loads(subprocess.check_output([str(source_python), '-c',
        'import importlib.metadata as m,json;print(json.dumps({n:m.version(n) for n in ' + repr(package_names) + '}))'], text=True))
    if any(importlib.metadata.version(name) != version for name, version in libraries.items()):
        raise ValueError('Версии генератора не совпадают с исходным окружением ДЗ 6+7')
    imports = read(exported / 'offline-import.json')
    offline_id = next(old for old, current in imports['mapping'].items() if current == candidate['run_id'])
    adapter = source / 'mlruns/1' / offline_id / 'artifacts/model/artifacts/adapter'
    adapter_hashes = collect(adapter)
    for name, digest in adapter_hashes.items():
        recorded = imports['verified_artifacts'][offline_id]['sha256']['model/artifacts/adapter/' + name]
        if digest != recorded:
            raise ValueError(f'Файл адаптера отличается от проверенного training run: {name}')
    from huggingface_hub import snapshot_download
    base = Path(snapshot_download(baseline['name'], revision=baseline['revision'], local_files_only=True))
    base_hashes = collect(base)
    from transformers import AutoTokenizer
    runtimes = {}
    manifests = {}
    for role, artifact, hashes in [('baseline', base, base_hashes), ('candidate', adapter, adapter_hashes)]:
        tokenizer = AutoTokenizer.from_pretrained(artifact, local_files_only=True)
        runtime = {'base_name': baseline['name'], 'base_revision': baseline['revision'],
                   'tokenizer_sha256': backend.tokenizer_hash(artifact),
                   'template_sha256': release.sha256(release.canonical(tokenizer.chat_template)),
                   'generation': {'system': config['system'], 'do_sample': False, 'seed': config['seed'],
                                  'max_new_tokens': config['max_new_tokens'], 'enable_thinking': config['enable_thinking']},
                   'dtype': 'float32', 'device': 'cpu', 'libraries': libraries}
        manifest = {'schema_version': 1, 'kind': 'base' if role == 'baseline' else 'adapter',
                    'model_id': baseline['name'] + '@' + baseline['revision'] if role == 'baseline' else candidate['run_id'],
                    'artifact_uri': str(artifact), 'model_sha256': release.sha256(release.canonical(hashes)),
                    'file_hashes': hashes, 'protocol_sha256': evaluation['protocol_sha256'],
                    'golden_sha256': evaluation['golden_sha256'], 'runtime_sha256': release.sha256(release.canonical(runtime)),
                    'runtime': runtime, 'evaluation_run_id': evaluation['evaluation_run_id'],
                    'training_run_id': None if role == 'baseline' else candidate['run_id']}
        runtimes[role] = runtime
        manifests[role] = manifest
    manifests['candidate']['base_manifest'] = manifests['baseline']
    provenance = {'source_directory': str(source),
                  'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip(),
                  'file_sha256': {p.relative_to(exported).as_posix(): release.file_sha256(p)
                                  for p in exported.rglob('*') if p.is_file()},
                  'candidate_spec': candidate, 'runtime': runtimes['candidate'],
                  'baseline_runtime': runtimes['baseline'],
                  'candidate_model_sha256': manifests['candidate']['model_sha256'],
                  'training_offline_run_id': offline_id,
                  'runtime_origin': 'Reconstructed from original evaluation.generate: tokenizer, exact revision, CPU/float32 and evaluation/params.json; no new generation settings.'}
    release.write_json(exported / 'provenance.json', provenance)
    for role, manifest in manifests.items():
        release.verify_files(manifest, root)
        release.write_json(root / f'manifest-{role}.json', manifest)
    manifests['baseline']['acceptance'] = {'basis': 'inherited-baseline-from-hw67',
                                         'baseline_spec_sha256': bridge.legacy_fingerprint(baseline),
                                         'note': 'Existing reference, not a newly passed candidate and not a synthetic gate.'}
    release.write_json(root / 'accepted.json', manifests['baseline'])
    shutil.copyfile(exported / 'golden.jsonl', root / 'input.txt')
    release.write_json(root / 'config.json', {'tokenizer_revision': baseline['revision'],
        'template_sha256': runtimes['candidate']['template_sha256'], 'max_length': 2048,
        'code_revision': release.file_sha256(Path(__file__).parent / 'flow.py'), 'format': 'golden-jsonl'})
    result = bridge.evaluate_release(root, manifests['candidate'])
    print(json.dumps({'workspace': str(root), 'candidate_passed': result['passed'],
                      'reason': result['reason'], 'accepted_model': manifests['baseline']['model_id']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
