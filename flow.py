"""Prefect-flow: подготовка → проверка артефакта → полный gate → публикация."""
import argparse
import json
import os
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault('PREFECT_HOME', str(ROOT / '.prefect'))
os.environ.setdefault('PREFECT_SERVER_ANALYTICS_ENABLED', 'false')

from prefect import flow, task
from prefect.cache_policies import NO_CACHE
from prefect.runtime import flow_run

import bridge
import release


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def separate_windows_state_timestamps():
    # У коротких задач Windows Pending/Running/Completed могут попасть в один тик часов.
    # Prefect SQLite требует уникальную пару task_run_id/timestamp.
    if os.name == 'nt':
        time.sleep(.025)


def preparation_key(context, parameters):
    root = Path(parameters['workspace'])
    config = {**read(root / 'config.json'), 'implementation_sha256': release.file_sha256(__file__)}
    return release.cache_key(root / 'input.txt', config)


@task(name='prepare-data', cache_key_fn=preparation_key, persist_result=True,
      result_storage=ROOT / '.cache/prefect-results', result_serializer='json')
def prepare_data(workspace):
    separate_windows_state_timestamps()
    root = Path(workspace)
    config = read(root / 'config.json')
    payload = (root / 'input.txt').read_bytes()
    rows = payload.decode('utf-8').splitlines()
    if config.get('format') == 'golden-jsonl':
        decoded = [json.loads(line) for line in rows if line.strip()]
        if not decoded or len({row['id'] for row in decoded}) != len(decoded):
            raise ValueError('Пустые/повторные ID подготовки')
        for row in decoded:
            if not isinstance(row['input'], str) or not row['input'].strip() or row['expected'] not in row['choices']:
                raise ValueError('Неверная схема входного примера')
    result = {'data_sha256': release.sha256(payload), 'rows': len(rows),
              'config_sha256': release.sha256(release.canonical(config)),
              'preparation_key': preparation_key(None, {'workspace': workspace})}
    print('PREPARED', result['preparation_key'], flush=True)
    return result


@task(name='obtain-verified-artifact', cache_policy=NO_CACHE, persist_result=False)
def obtain_artifact(workspace, manifest_name):
    separate_windows_state_timestamps()
    manifest = read(Path(workspace) / manifest_name)
    release.verify_files(manifest, workspace)
    print('ARTIFACT_VERIFIED', manifest['model_id'], flush=True)
    return manifest


@task(name='evaluate-full-gate', cache_policy=NO_CACHE, persist_result=False)
def assess(workspace, manifest, report_name, synthetic):
    separate_windows_state_timestamps()
    if synthetic:
        report = read(Path(workspace) / report_name)
        release.validate_release(manifest, report)
    else:
        root = Path(workspace)
        if (root / 'input.txt').read_bytes() != (root / 'hw67/golden.jsonl').read_bytes():
            raise ValueError('Подготовленный golden изменён: сохранённая оценка к нему не относится')
        report = bridge.evaluate_release(workspace, manifest)
        if report['passed']:
            release.validate_release(manifest, report)
    return report


@task(name='publish-or-preserve', cache_policy=NO_CACHE, persist_result=False)
def publish_or_preserve(workspace, manifest, report, fail_after_publish):
    separate_windows_state_timestamps()
    if report['passed'] is not True:
        return {'status': 'rejected', 'reason': report['reason'], 'published': False}
    return {'status': 'accepted', **release.journaled_publish(manifest, report, workspace, fail_after_publish)}


@flow(name='hw8910-release', log_prints=True, persist_result=False)
def release_flow(workspace, synthetic=False, manifest_name='manifest-candidate.json', report_name='report-B.json',
                 fail_after_prepare=False, fail_after_publish=False):
    root = Path(workspace).resolve()
    attempt = {'attempt_id': str(uuid.uuid4()), 'flow_run_id': str(flow_run.id), 'synthetic': synthetic}
    before = release.sha256((root / 'accepted.json').read_bytes())
    try:
        state = prepare_data(str(root), return_state=True)
        prepared = state.result()
        attempt.update(preparation_state=state.name, preparation=prepared)
        print('PREPARATION_STATE', state.name, prepared['preparation_key'])
        if fail_after_prepare:
            raise RuntimeError('Инъекция сбоя после подготовки')
        manifest = obtain_artifact(str(root), manifest_name)
        report = assess(str(root), manifest, report_name, synthetic)
        result = publish_or_preserve(str(root), manifest, report, fail_after_publish)
        attempt.update(result=result, status='complete')
        return result
    except Exception as exc:
        attempt.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        attempt.update(accepted_before=before, accepted_after=release.sha256((root / 'accepted.json').read_bytes()))
        directory = root / 'flow-attempts'
        release.write_json(directory / (attempt['attempt_id'] + '.json'), attempt)
        print('ATTEMPT', json.dumps(attempt, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=Path('.lab-real'))
    parser.add_argument('--synthetic', action='store_true')
    parser.add_argument('--manifest')
    parser.add_argument('--report', default='report-B.json')
    parser.add_argument('--fail-after-prepare', action='store_true')
    parser.add_argument('--fail-after-publish', action='store_true')
    args = parser.parse_args()
    result = release_flow(str(args.workspace), args.synthetic,
                          args.manifest or ('manifest-B.json' if args.synthetic else 'manifest-candidate.json'),
                          args.report, args.fail_after_prepare, args.fail_after_publish)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
