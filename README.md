# ДЗ 8+9+10: процесс выпуска и локальный API

Продолжение ДЗ 6+7 на RuSentiment и Qwen3-0.6B. Исправлены семь причин ошибок прототипа. Исходный комплект сохранён коммитом `b338ec1`; оригинальные `tests/` не изменены. Их отпечаток — `09bd65e6b886`.

`README.txt`, `INCIDENTS.txt` и `CONTRACTS.txt` сохраняют исходные условия задания. Итоговый отчёт находится в `docs/report.md` и `REPORT.txt`, причины и тесты — в `defects.csv`, проверяемые результаты — в `evidence/`.

## Окружение и быстрые проверки

Python 3.12, uv 0.12.10. Зависимости закреплены в `pyproject.toml` и `uv.lock`. Команды выполняются из корня проекта. FastAPI/Prefect входят в основное окружение, генератор вынесен в optional extra `inference`.

```bash
uv sync --frozen
make check
```

Проверяются компиляция, 13 исходных и 13 новых тестов. Генератор и веса не загружаются. Для намеренного красного прогона используется изолированная копия кода:

```bash
make check-red
```

Ненулевой exit code здесь ожидается. Рабочий `release.py` при этом не изменяется. Облачный CI не запускался; локальный эквивалент сохранён в `evidence/fast-green.log` и `evidence/fast-intentional-red.log`. `ci.example.yml` содержит пример тех же команд.

## Стенд STUB ONLY

Для каждого нового независимого опыта выбирается новый workspace. `init` отказывается перезаписывать существующий каталог.

```bash
uv run --frozen --no-sync python lab.py --workspace .lab-demo init
uv run --frozen --no-sync python flow.py --workspace .lab-demo --synthetic --fail-after-prepare
uv run --frozen --no-sync python flow.py --workspace .lab-demo --synthetic
uv run --frozen --no-sync python flow.py --workspace .lab-demo --synthetic
```

Первый flow намеренно падает. После восстановления подготовка имеет состояние Cached. Повтор выпуска не добавляет событие публикации. Полный автоматизированный опыт с журналами:

Для коротких задач на Windows добавлен интервал 25 мс: он разделяет Pending/Running/Completed в SQLite Prefect при низкой точности системных часов. Интервал не входит в измеряемую генерацию API.

```bash
uv run --frozen --no-sync python experiments.py --prefix .lab-new-experiment
```

Полный experiments.py предполагает подготовленный `.lab-real`: последний опыт проверяет реальный сохранённый отказ. Синтетические ответы всегда содержат `STUB ONLY`. Они используются для инженерных тестов, не для оценки качества модели.

## Реальная модель и API

```bash
uv sync --frozen --extra inference
make real-prepare
make real-flow
make serve
```

`real-prepare` выполняется один раз и использует существующий `../mlops26-hw6-7/hw67-broken`, его отчёты, выбранный адаптер и точную ревизию базы в HF-кеше. Для другого исходного каталога используется `python prepare_real.py --source /путь/к/hw67-broken --workspace .lab-real-new`.

На этом компьютере `.lab-real` уже подготовлен. Для обычного повторного запуска достаточно `make real-flow` и `make serve`. Полный gate повторно проверяет исходные отчёты, сырые ответы, версии и хеши. Новый sweep и новая генерация eval не требуются при неизменном протоколе.

Кандидат отклонён: accuracy 16% против 22% у базы. Указатель остаётся на унаследованном baseline. Для него не создаётся вымышленный report с `passed=true`. API использует условия первоначальной оценки: CPU/float32, greedy, JSON, `enable_thinking=false`, лимит 32 новых токена.

Адреса: `http://127.0.0.1:8091/health`, `/ready`, `/docs`. Готовность появляется после проверки файлов, загрузки и реального прогрева. Команда `make serve` включает локальный `/debug` для опыта; при обычном запуске `service.py --backend real` он выключен без флага `--debug`.

Пример запроса в PowerShell:

```powershell
$body = @{ prompt = 'Сегодня отличный день!'; max_new_tokens = 32 } | ConvertTo-Json
Invoke-RestMethod -Uri 'http://127.0.0.1:8091/generate' -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

Допустимы непустые строки до 4096 символов и до 2048 токенов с шаблоном. Реальный API принимает только `max_new_tokens=32`, сохраняя оценённый протокол. Параллельные вычисления ограничены одним backend, запросы ожидают блокировку; длительности ожидания и генерации возвращаются раздельно.

## Измерения и артефакты

`make measure` запускает реальный сервер в отдельном процессе, проводит smoke, 8+8 запросов при concurrency 1 и 2 и повторную загрузку. Уже полученные результаты находятся в `evidence/benchmark.json`, `requests.jsonl`, `real-smoke.json`, `real-restart.json`. Повтор для обычной проверки проекта не требуется.

`make evidence` обновляет отчёт, `defects.csv` и архив `submission/hw8910-submission.zip` без весов, окружений и кешей. В архив входят исходные отчёты и данные проверки ДЗ 6+7, а также `repository.bundle` с Git-историей. После распаковки репозиторий восстанавливается командой `git clone repository.bundle hw8910`. Корпус — RuSentiment, источник https://github.com/strawberrypie/rusentiment, лицензия CC BY-NC-SA 4.0.

На другой машине базовая модель доступна на Hugging Face по закреплённой ревизии. Адаптер доступен в существующем локальном MLflow ДЗ 6+7 как `models:/my-qwen-adapter/1`; для проверки артефакта нужен доступ к этому серверу или его хранилищу. В архив сдачи веса не включаются. HTTP API, журнал и tests работают локально; удалённая защита merge не заявляется.
