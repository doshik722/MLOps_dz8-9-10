"""Отчёт и архив сдачи по сохранённым журналам; отсутствующие опыты не подменяются."""
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import release

ROOT = Path(__file__).resolve().parent
E = ROOT / 'evidence'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, encoding='utf-8').strip()


def main():
    green = (E / 'fast-green.log').read_text(encoding='utf-8')
    red = (E / 'fast-intentional-red.log').read_text(encoding='utf-8')
    original = (E / 'original-integration.log').read_text(encoding='utf-8')
    assert re.findall(r'Ran (\d+) tests', green) == ['13', '13'] and 'EXIT_CODE=0' in green
    assert 'FAILED' in original and 'Ran 8 tests' in original and 'EXIT_CODE=1' in original
    assert 'FAILED' in red and 'EXIT_CODE=1' in red
    digest = hashlib.sha256()
    for path in sorted((ROOT / 'tests').glob('*')):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    assert digest.hexdigest()[:12] == '09bd65e6b886'
    fixed = git('log', '-1', '--format=%H', '--', 'release.py', 'lab.py', 'service.py', 'flow.py', 'backend.py', 'bridge.py')
    initial = git('rev-list', '--max-parents=0', 'HEAD')
    assert fixed != initial, 'Сначала требуется отдельный commit исправлений'
    flows = read(E / 'flow-experiments.json')
    negatives = read(E / 'negative-releases.json')
    benchmark = read(E / 'benchmark.json')
    assert all(row['n'] == row['successes'] == 8 and row['errors'] == 0 for row in benchmark)
    smoke = read(E / 'real-smoke.json')
    restarted = read(E / 'real-restart.json')
    rollback = read(E / 'stub-rollback.json')
    workspace = ROOT / '.lab-real'
    accepted = read(workspace / 'accepted.json')
    candidate = read(workspace / 'manifest-candidate.json')
    decision = read(workspace / 'release-report.json')
    provenance = read(workspace / 'hw67/provenance.json')
    assert decision['passed'] is False and accepted['kind'] == 'base'
    assert smoke['matches_hw67_baseline_output'] and restarted['same_model_runtime_and_answer']
    assert all(row['n'] == 8 for row in benchmark)
    assert rollback['observed_models'] == ['fixture-A', 'fixture-A', 'fixture-B', 'fixture-A']
    fields = ['id', 'symptom', 'reproduce', 'expected', 'observed', 'root_cause', 'fix_commit',
              'regression_test', 'before_log', 'after_log']
    cases = [
        (1, 'После изменения данных или config используется старый результат',
         'test_subprocess_cache_recovery_and_changed_bytes; test_subprocess_preparation_config_invalidates',
         'Ключ зависит от содержимого и всех условий', 'Исходный прототип возвращал CACHE_HIT после изменения',
         'Ключ вычислялся только по строке пути', 'release.cache_key; flow.preparation_key'),
        (2, 'Неполный или чужой отчёт допускает выпуск', 'test_subprocess_rejects_incomplete_and_foreign_report',
         'Отказ, SHA accepted неизменен', 'running и строковое passed допускались',
         'Проверялось только truthiness passed без схемы, метрик и связей', 'release.validate_release; bridge.evaluate_release'),
        (3, 'Ошибка записи повреждает указатель', 'test_subprocess_atomic_replace_failure_preserves_pointer',
         'Старые байты сохранены, ошибка проброшена, tmp удалён', 'os.replace не использовался, указатель перезаписывался',
         'Неатомарный write_text в accepted.json', 'release.atomic_bytes; publish'),
        (4, 'Повтор выпуска создаёт дубликаты', 'test_subprocess_repeat_creates_one_publication',
         'Одна публикация, независимые попытки', 'Два события B при двух запусках',
         'Безусловный append и отсутствие durable intent', 'release.journaled_publish; recover_publication; lab.run'),
        (5, 'Сервис готов до прогрева', 'test_http_not_ready_before_warmup',
         'health=200, ready/generate=503 до прогрева', 'ready сразу True',
         'Готовность объявлялась до загрузки/прогрева', 'service.ServiceState; lifespan'),
        (6, 'ID ответа меняется раньше модели в памяти', 'test_http_loaded_version_and_rollback',
         'A до загрузки B, затем B, после отката A', 'Ответ A маркировался как B',
         'Manifest перечитывался на запрос вместо метаданных загруженного backend', 'service.ServiceState.generate'),
        (7, 'Неверный запрос запускает backend', 'test_http_invalid_request_never_calls_backend',
         '4xx, calls до/после равны', 'Некорректные запросы получали 200 и увеличивали calls',
         'Отсутствовала строгая проверка до генерации', 'service.GenerateRequest; ServiceState.generate; backend.generate')]
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for identity, symptom, test, expected, observed, cause, fix in cases:
        writer.writerow(dict(id=identity, symptom=symptom,
            reproduce='python -m unittest discover -s integration_tests -p test_incidents.py -v: ' + test,
            expected=expected, observed=observed, root_cause=cause + '; исправление: ' + fix,
            fix_commit=fixed, regression_test=test, before_log='evidence/original-integration.log',
            after_log='evidence/fast-green.log'))
    (ROOT / 'defects.csv').write_text(output.getvalue(), encoding='utf-8')
    lines = ['# ДЗ 8+9+10: процесс выпуска, проверки и API', '',
        '## 1. Условия, версии и расследование', '',
        f'Дата: 01.11.2026. Локальный репозиторий: {ROOT}. Исходный commit: `{initial}`. Commit исправления реализации: `{fixed}`. Удалённый репозиторий для этого задания не создавался.', '',
        f'Система: {platform.platform()}; Python {platform.python_version()}; логических CPU: {os.cpu_count()}. Генерация: CPU, 4 потока, float32. FastAPI {importlib.metadata.version("fastapi")}, Uvicorn {importlib.metadata.version("uvicorn")}, Prefect {importlib.metadata.version("prefect")}. Полные зависимости — pyproject.toml и uv.lock. Библиотеки генератора закреплены в runtime.', '',
        'Найдены семь независимых причин; подробная трассировка приведена в defects.csv. Восемь новых интеграционных тестов выполнены на исходных release.py/lab.py/service.py из первого коммита: падения — поведенческие AssertionError, не ошибки импорта. Затем те же тесты прошли на исправленном FastAPI и процессе выпуска. Три теста обращаются к настоящему HTTP; пять используют subprocess. Дополнительные пять тестов проверяют fsync, восстановление журнала, изменения артефакта, идентичность выпуска и отрицательные случаи.', '',
        'Красные логи: evidence/original-contract.log, original-integration.log. Зелёный итог: evidence/fast-green.log — 13 исходных + 13 собственных = 26 тестов. Выданные tests/ побайтно сохранены; отпечаток 09bd65e6b886. Исходное ДЗ 6+7 не изменялось.', '',
        '## 2. Переиспользование ДЗ 6+7', '',
        f'Training run кандидата: `{candidate["training_run_id"]}`; evaluation run: `{decision["evaluation_run_id"]}`. Источник: {provenance["source_directory"]}, commit `{provenance["source_commit"]}`. Веса адаптера сверены с SHA256 из original→server MLflow import. Полные отчёты, сырые ответы и код прежнего scorer скопированы в .lab-real/hw67; файл provenance.json фиксирует SHA256 каждого источника.', '',
        f'Кандидат: {candidate["model_id"]}; baseline: {accepted["model_id"]}. Исходная оценка: 22% accuracy у базы и 16% у кандидата. Повторно вызван прежний quality_gate; решение: `{decision["reason"]}`. Новый sweep, обучение и полная генерация eval не выполнялись.', '',
        'В accepted.json закреплён унаследованный baseline, а не фиктивно одобренный кандидат. Поле acceptance прямо указывает inherited-baseline-from-hw67. Для базы не создан искусственный report с passed=true. Новый release-report.json создаётся bridge.py только после проверки полных сохранённых отчётов и повторного вызова quality_gate.', '',
        '## 3. Граф задач, кеш и повтор', '',
        '```text', 'prepare-data → obtain-verified-artifact → evaluate-full-gate → publish-or-preserve',
        '                                                        ├─ отказ: прежний accepted.json',
        '                                                        └─ PASS: intent → atomic pointer → journal', '```', '',
        'flow.py содержит реальный Prefect-flow и четыре отдельные задачи. Передаются небольшие JSON-описания, URI, версии и хеши; веса/тензоры не становятся результатами задач. Получение артефакта читает существующий training run, а не обучает заново.', '',
        'prepare-data проверяет UTF-8/JSONL, уникальность ID, непустой input и допустимость expected. Ключ содержит SHA256 байтов входа, все поля config: точную tokenizer_revision, template_sha256, max_length, code_revision, а также фактический SHA256 flow.py. Имя пути и mtime ключом не являются. Prefect persist_result=True, JSON result serializer и файловое result_storage=.cache/prefect-results сохраняют результат между процессами; метаданные Prefect находятся в .prefect.', '',
        'Задачи проверки артефакта, gate и публикации не кешируются. Автоматические retries для отказов, неверной схемы и несовместимых весов отключены. Удалённое получение артефактов в опытах не выполнялось, сетевые retries не требовались. Расписание и исторические перезапуски не настраивались.', '',
        'attempt_id/flow_run_id идентифицируют попытку. release_id — SHA256 manifest и связанного решения допуска; в него входят model, runtime и evaluation, а не один model_id. Попытки сохраняются отдельно от событий. Для одного писателя durable intent восстанавливает журнал после сбоя между accepted.json и событием; каждый JSON/JSONL заменяется через временный файл, flush, fsync и os.replace.', '',
        '## 4. Реальные опыты процесса', '',
        '| Журнал | Exit | Состояние подготовки | Результат | Событий |', '|---|---:|---|---|---:|']
    for row in flows:
        attempt = row['attempt']
        lines.append(f'| {row["log"]} | {row["exit_code"]} | {attempt.get("preparation_state", "—")} | {attempt.get("result", {}).get("status", attempt["status"])} | {len(row["publications"])} |')
    lines += ['', 'Полные команды, flow_run_id, preparation_key и SHA accepted до/после сохранены в evidence/flow-experiments.json и соответствующих логах. Восстановление выполнялось в новых процессах. После изменения байтов входа по прежнему пути и max_length получено Completed, а не Cached. Повтор успешного B оставил одно событие, включая опыт со сбоем после указателя.', '',
              '## 5. Полный gate и безопасная публикация', '',
              f'Runtime принятой модели: `{accepted["runtime_sha256"]}`. В runtime закреплены exact base revision, хеши файлов токенизатора/шаблона, system-инструкция, greedy, seed 42, max_new_tokens=32, enable_thinking=false, CPU/float32 и точные версии библиотек. Проверочный runtime кандидата восстановлен из исходного evaluation.generate и его файлов, а не из обучающего generation-конфига с лимитом 16.', '',
              'bridge.py пересчитывает исходный protocol fingerprint, сверяет golden и baseline, SHA256 сырых ответов, ID и все результаты score, включая поведенческие. Затем вызывается прежний quality_gate. Перед выпуском и загрузкой отдельно сверяются реальные файлы адаптера и закреплённой базы. Изменение генерации/данных/модели требует новой полной оценки; редактирование сохранённых хешей не заменяет её.', '',
              '| Отрицательный случай | SHA accepted до | SHA accepted после | Причина |', '|---|---|---|---|']
    for row in negatives:
        lines.append(f'| {row["case"]} | {row["before"]} | {row["after"]} | {row["reason"]} |')
    actual = flows[-1]['attempt']
    lines += ['', f'Реальный отказ: SHA accepted до `{actual["accepted_before"]}`, после `{actual["accepted_after"]}`. Синтетический PASS из examples/.lab относится только к STUB ONLY и не используется для допуска Qwen.', '',
              '## 6. Быстрые проверки и три триггера', '',
              '`make check`: компиляция всех модулей, исходные контракты, HTTP/subprocess и проверки сохранности — без загрузки весов. `make check-red` намеренно меняет cache_key только в изолированной копии и завершается exit=1; рабочий код сохраняется. Затем снова проверена восстановленная исходная функция. Логи обоих исходов приложены. Это локальный эквивалент CI; GitHub Actions не запускался, защита merge не заявляется. ci.example.yml описывает fast-проход без extra inference.', '',
              '| Триггер | Действие и команды |', '|---|---|',
              '| Новый код | make check; при изменении генератора — make evaluate в исходном HW67, затем prepare_real.py в новом workspace и flow.py --workspace новый_каталог |',
              '| Новые данные | Проверка схемы/версий, новый cache_key; при изменении golden — полный eval обеих версий. Новые train-данные требуют отдельного решения о переобучении; простой смены пути недостаточно |',
              '| Новый адаптер | Проверка файлов/загрузки, полная оценка и quality_gate до обновления accepted.json; несовместимый отчёт закрывает выпуск |', '',
              '## 7. Реальный API и ограничения запросов', '',
              'FastAPI запускает загрузку в фоне из lifespan; health=200 подтверждает жизнь процесса, ready=503 до проверки весов, загрузки и реального прогрева. Наблюдён переход 503 → 200 при health=200. До готовности generate возвращает 503. StubBackend и TransformersBackend внедряются через create_app; реальный manifest нельзя обслужить StubBackend. Фактические model_id/runtime_sha256 берутся из backend в памяти, а не из изменяемого указателя.', '',
              f'Реальный smoke: HTTP {smoke["status"]}, ответ `{smoke["response"]["answer"]}`, выходных токенов {smoke["response"]["output_tokens"]}; request_id `{smoke["response"]["request_id"]}`. model_id `{smoke["response"]["model_id"]}`. Ответ и все 16 результатов замера совпали с сохранёнными исходными ответами baseline. evidence/real-smoke.json и real-restart.json содержат HTTP-готовность, длительности и повторную загрузку.', '',
              'Вход: непустая строка до 4096 символов; после шаблона не более 2048 токенов. Исходный текст не обрезается и не нормализуется. Для настоящего API допустим только max_new_tokens=32 из оценённого протокола. STUB-контракт допускает целые 1–32. Пустая строка, 0/100000, строка вместо числа, лимит 1 для реальной модели и чрезмерная длина дали HTTP 422 при неизменном calls. Backend генерирует последовательно под одним Lock; при concurrency=2 второй запрос ждёт очередь.', '',
              '## 8. Нагрузка: одинаковые 8+8 запросов', '',
              'После прогрева взяты одни и те же восемь сообщений трёх классов, одинаковый лимит 32, один процесс API и один backend. Ошибки/тайм-ауты сохраняются; в проведённом опыте их не было. Сырые записи всех 16 попыток — evidence/requests.jsonl, плоская таблица — requests.csv. Среднее и максимум ошибок при n_errors=0 не определены и показаны знаком —.', '',
              '| Concurrency | n | Успехи | Ошибки | Пакет, с | Успешных req/s | Средняя задержка, с | Максимум, с | Очередь средняя, с | Генерация средняя, с |',
              '|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in benchmark:
        lines.append(f'| {row["concurrency"]} | {row["n"]} | {row["successes"]} | {row["errors"]} | {row["batch_seconds"]:.3f} | {row["successful_req_per_sec"]:.3f} | {row["mean_client_seconds"]:.3f} | {row["max_client_seconds"]:.3f} | {row["mean_queue_seconds"]:.3f} | {row["mean_generation_seconds"]:.3f} |')
    lines += ['', 'Выходные токены в обоих режимах: ' + ', '.join(map(str, benchmark[0]['output_tokens'])) + '. Лимит — максимум, остановка по EOS наступила раньше. Пропускная способность почти одинакова; при concurrency=2 средняя клиентская задержка выросла за счёт ожидания одного последовательного backend. Различие req/s на такой выборке не доказывает ускорения.', '',
              'Клиентская задержка включает HTTP/JSON, ожидание и вычисление. queue_seconds измеряет ожидание блокировки внутри API; generation_seconds включает токенизацию, model.generate и декодирование. Prefill обрабатывает входной контекст, decode последовательно строит новые токены. KV-cache хранит K/V по слоям и растёт с длиной контекста и числом одновременно обслуживаемых последовательностей. Потоковой выдачи нет: измерена полная задержка JSON-ответа, TTFT/ITL и устойчивый p95 не измерялись. Этот последовательный Transformers backend не является бенчмарком SGLang/vLLM.', '',
              '## 9. Фактическая версия и откат', '',
              'Реальные HTTP-ответы STUB-опыта: A → A после публикации B без перезагрузки → B после перезапуска → A после публикации ранее принятого A и перезапуска. Ответы с явной надписью STUB ONLY, ID и runtime_sha256 сохранены в evidence/stub-rollback.json. Это проверка управления состоянием, не обучение и не eval модели.', '',
              'Для реальной модели доступен только принятый baseline. Отдельно повторно запущен API, проверены тот же model_id, runtime_sha256 и идентичный настоящий ответ. При реальном откате выбирается ранее одобренный manifest с его отчётом, сверяются файлы, атомарно обновляется указатель, затем управляемо перезапускается API и проверяются ready и фактический ID. Изменение accepted.json само по себе не меняет веса в памяти.', '',
              '## 10. Ограничения и доступ к артефактам', '',
              'Стенд рассчитан на одного писателя и один процесс генерации. Нет очереди с отдельным лимитом ожидания, распределённой блокировки, GPU, streaming, нагрузочного soak-теста и удалённого CI. Восемь запросов на режим — учебный замер, не гарантия производительности. Кандидат отвергнут честно; пороги и исходные отчёты не ослаблялись.', '',
              'Архив submission/hw8910-submission.zip содержит проект, журналы, manifest/release-report и исходные отчёты/данные оценки; веса, .venv, кеши и секреты исключены. База: https://huggingface.co/Qwen/Qwen3-0.6B, exact revision указана в manifest. Адаптер: существующий MLflow ДЗ 6+7, models:/my-qwen-adapter/1, исходные локальные файлы в provenance. Для проверки файлов адаптера на другой машине нужен доступ к этому MLflow или его хранилищу. Готовые отчёты доступны в архиве без сервера. Данные RuSentiment: https://github.com/strawberrypie/rusentiment, CC BY-NC-SA 4.0.', '',
              'Документация: Prefect https://reference.prefect.io/prefect/ ; FastAPI lifespan https://fastapi.tiangolo.com/advanced/events/ . Команды установки и повторного запуска находятся в README.md. Данные и результаты прошлых ДЗ не редактировались.', '',
              '## 11. Видео', '',
              'Видео 5–8 минут пока не записано; ссылка прикладывается к сдаче отдельно. В отчёте сохранены проверяемые журналы тяжёлых шагов.', '',
              '## Вывод', '',
              'Исправлены семь причин ошибок. Восемь новых регрессионных интеграционных тестов выявляют дефекты исходного прототипа; после исправлений все 26 тестов проходят. Prefect подтвердил восстановление между процессами, Cached/Completed и отсутствие повторных событий публикации. Полный gate сохранил прежний baseline при отклонении кандидата. Настоящий FastAPI подтвердил загрузку и прогрев, ограничения до генерации, фактическую версию и повторный запуск. Замер 8+8 показал очередь при последовательном backend; результаты ограничены малой выборкой и CPU-стендом.', '']
    (ROOT / 'docs').mkdir(exist_ok=True)
    text = '\n'.join(lines)
    (ROOT / 'docs/report.md').write_text(text, encoding='utf-8')
    (ROOT / 'REPORT.txt').write_text(text, encoding='utf-8')
    requests = [json.loads(line) for line in (E / 'requests.jsonl').read_text(encoding='utf-8').splitlines()]
    csv_fields = ['client_id', 'example_id', 'concurrency', 'status', 'client_seconds', 'request_id',
                  'model_id', 'runtime_sha256', 'output_tokens', 'queue_seconds', 'generation_seconds', 'answer', 'error']
    with (E / 'requests.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=csv_fields)
        writer.writeheader()
        for row in requests:
            response = row['response'] or {}
            writer.writerow({key: row.get(key, response.get(key)) for key in csv_fields})
    release.write_json(E / 'verification.json', {'original_commit': initial, 'implementation_commit': fixed,
        'tests_fingerprint': digest.hexdigest()[:12], 'original_tests': 13, 'new_tests': 13,
        'red_integration_tests': 8, 'full_fast_check_exit': 0, 'intentional_failure_exit': 1,
        'synthetic_rollback_observed': rollback['observed_models'], 'real_requests': len(requests),
        'real_errors': sum(row['status'] != 200 for row in requests), 'candidate_rejected': True,
        'accepted_model': accepted['model_id'], 'video_recorded': False})
    submission = ROOT / 'submission'
    submission.mkdir(exist_ok=True)
    subprocess.run(['git', 'bundle', 'create', str(submission / 'hw8910-history.bundle'), '--all'], cwd=ROOT, check=True)
    with ZipFile(submission / 'hw8910-submission.zip', 'w', compression=ZIP_DEFLATED) as archive:
        files = set(git('ls-files').splitlines())
        files.update(['REPORT.txt', 'defects.csv', 'docs/report.md'])
        for name in sorted(files):
            path = ROOT / name
            if path.is_file():
                archive.write(path, 'project/' + name)
        for path in sorted(E.glob('*')):
            if path.is_file():
                archive.write(path, 'evidence/' + path.name)
        for path in sorted((workspace / 'hw67').rglob('*')):
            if path.is_file() and path.suffix in ('.py', '.json', '.jsonl'):
                archive.write(path, 'artifacts/hw67/' + path.relative_to(workspace / 'hw67').as_posix())
        for name in ['accepted.json', 'manifest-baseline.json', 'manifest-candidate.json', 'release-report.json', 'config.json']:
            archive.write(workspace / name, 'artifacts/' + name)
        archive.write(submission / 'hw8910-history.bundle', 'repository.bundle')
    print('Обновлены docs/report.md, REPORT.txt, defects.csv и submission/hw8910-submission.zip.')


if __name__ == '__main__':
    main()
