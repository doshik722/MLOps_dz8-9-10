"""Повторная проверка сохранённой полной оценки ДЗ 6+7 без новой генерации."""
import importlib.util
import json
from pathlib import Path

import release


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def legacy_module(path):
    spec = importlib.util.spec_from_file_location('hw67_quality_solution', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_fingerprint(value):
    return release.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8'))


def evaluate_release(root, manifest):
    root = Path(root)
    exported = root / 'hw67'
    provenance = read(exported / 'provenance.json')
    for relative, expected in provenance['file_sha256'].items():
        if release.sha256((exported / relative).read_bytes()) != expected:
            raise ValueError(f'Изменён исходный отчёт/протокол: {relative}')
    if manifest['runtime'] != provenance['runtime']:
        raise ValueError('Runtime API не совпадает с runtime сохранённой оценки')
    if manifest['runtime_sha256'] != release.sha256(release.canonical(provenance['runtime'])):
        raise ValueError('Некорректный runtime_sha256')
    reports = {role: read(exported / f'{role}-eval.json') for role in ('baseline', 'candidate')}
    rows = [json.loads(line) for line in (exported / 'golden.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    behavior = [json.loads(line) for line in (exported / 'behavioral.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    training = [json.loads(line) for line in (exported / 'training-index.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    code = {p.name: p.read_text(encoding='utf-8') for p in sorted((exported / 'evaluation-code').glob('*.py'))}
    config = read(exported / 'params.json')
    protocol = legacy_fingerprint(dict(config=config, golden=rows, behavioral=behavior,
                                      training_index=training, evaluation_code=code))
    solution = legacy_module(exported / 'evaluation-code/solution.py')
    for role, report in reports.items():
        if report['protocol_sha256'] != protocol or report['golden_sha256'] != legacy_fingerprint(rows):
            raise ValueError('Изменились протокол или golden')
        if report['baseline_spec_sha256'] != legacy_fingerprint(read(exported / 'baseline.json')):
            raise ValueError('Изменён baseline')
        outputs = read(exported / f'{role}-outputs.json')
        if report['outputs_sha256'] != legacy_fingerprint(outputs):
            raise ValueError('Изменены сырые ответы')
        by_id = {item['id']: item for item in outputs}
        if len(by_id) != len(outputs) or set(by_id) != {r['id'] for r in rows + behavior}:
            raise ValueError('Неполный набор ответов')
        score = solution.score(rows, [by_id[r['id']] for r in rows])
        if any(score[key] != report[key] for key in ('overall', 'slices', 'per_item')):
            raise ValueError('Метрики не соответствуют сырым ответам')
        if report['behavioral'] != solution.score(behavior, [by_id[r['id']] for r in behavior]):
            raise ValueError('Поведенческие метрики не соответствуют ответам')
    candidate = reports['candidate']
    if candidate['model'] != provenance['candidate_spec'] or reports['baseline']['model'] != read(exported / 'baseline.json'):
        raise ValueError('Исходная модель/кандидат не соответствуют provenance')
    if manifest['model_id'] != candidate['model']['run_id']:
        raise ValueError('Отчёт другого кандидата')
    if manifest['protocol_sha256'] != protocol or manifest['golden_sha256'] != candidate['golden_sha256']:
        raise ValueError('Manifest не связан с протоколом')
    if manifest['model_sha256'] != provenance['candidate_model_sha256']:
        raise ValueError('Изменён артефакт кандидата; нужна новая оценка')
    passed, reason = True, None
    try:
        solution.quality_gate(reports['baseline'], candidate, config['critical_slices'])
    except ValueError as exc:
        passed, reason = False, str(exc)
    report = {key: manifest[key] for key in ('model_id', *release.HASH_FIELDS)}
    report.update(status='complete', passed=passed, reason=reason,
                  evaluation_run_id=candidate['evaluation_run_id'],
                  metrics={key: candidate['overall'][key] for key in ('accuracy', 'format_rate')},
                  source_files={name: {'path': 'hw67/' + name, 'sha256': value}
                                for name, value in provenance['file_sha256'].items()},
                  training_run_id=candidate['model']['run_id'], synthetic=False)
    release.write_json(root / 'release-report.json', report)
    return report
