# Dodo IS Natural Reports Telegram Bot

Telegram-бот превращает русскоязычный запрос в безопасный, типизированный план отчёта,
получает данные из разрешённых GET-операций Dodo IS и рассчитывает метрики обычным
Python-кодом. Результат отправляется сообщением, CSV или XLSX. FastAPI API временно сохранён
как совместимый transport на период миграции.

Проект рассчитан на Python 3.12, aiogram 3, FastAPI, Pydantic v2, OpenAI Responses API,
`httpx.AsyncClient`, SQLite FTS5 и `openpyxl`.

## Архитектура

```text
Русский запрос
  → локальный поиск aliases + SQLite FTS5
  → top-K компактных endpoint candidates
  → один OpenAI Responses API вызов → ReportPlan
  → backend validation + unit resolution
  → allowlist executor + date/unit chunking + pagination
  → детерминированная агрегация (mode=metrics)
    либо плоская таблица по схеме документации (mode=raw)
  → table / JSON / CSV / XLSX
```

Документация целиком в OpenAI не передаётся. При старте ZIP читается локально,
нормализованные GET-операции и поля ответа записываются в SQLite FTS5. Модель получает
только до десяти компактных кандидатов, список metric IDs/aliases, текущую дату, timezone
и запрос пользователя. Dodo token, `Authorization`, URL от модели и сырые ответы Dodo
в prompt не попадают.

OpenAI используется только для понимания языка и построения `ReportPlan`. Поиск,
разрешение заведений, проверка плана, HTTP, повторы, chunking, pagination, дедупликация,
интервалы, суммы, отношения, взвешенные средние и экспорт выполняются локально.
`LLM_SUMMARY_ENABLED=false` по умолчанию гарантирует отсутствие второго LLM-вызова;
резюме строится детерминированно.

## Быстрый старт

Исходный архив должен находиться по пути
`Dodo_IS_API_Reference_Sorted.zip`. Если его нет или внутри отсутствует
`Dodo_IS_API_Reference_Sorted.json`, приложение завершится с понятной ошибкой.

### Telegram-бот через uv

Linux/macOS:

```bash
uv sync --extra dev
cp .env.example .env
uv run python -m app.cli build-index
uv run python main.py
```

Windows PowerShell:

```powershell
uv sync --extra dev
Copy-Item .env.example .env
uv run python -m app.cli build-index
uv run python main.py
```

### pip

Linux/macOS:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

После установки:

```bash
python -m app.cli build-index
python main.py
```

Получите токен у BotFather и задайте `TELEGRAM_BOT_TOKEN`. По умолчанию
`TELEGRAM_PUBLIC_ACCESS=true`, поэтому перечислять ID обычных пользователей не требуется.

Команды бота:

- `/start` — открыть кнопочное меню;
- `/reports` — создать отчёт кнопками;
- `/schedules` — настроить еженедельные отчёты;
- `/drive` — подключить Google Drive для выгрузки в Google Sheets;
- `/help` — инструкция;
- `/cancel` — отмена текущего диалога.

Основной сценарий работает через inline-кнопки: город → заведение → категория → тип отчёта →
период → формат → подтверждение. Свободный текст также поддерживается. Еженедельная
подписка присылает выбранный отчёт за предыдущую завершённую неделю (понедельник–
воскресенье) в выбранный день и целый час от `06:00` до `22:00`. Время
интерпретируется в `APP_TIMEZONE`.
Одинаковая подписка сохраняется один раз; по умолчанию доступно до 10 активных подписок
на пользователя (`TELEGRAM_MAX_WEEKLY_REPORTS_PER_USER`).
Для long polling должна работать одна реплика бота.

Совместимый web API при необходимости запускается отдельно:

```bash
COMPATIBILITY_API_KEY=replace-with-a-long-random-value \
  uv run uvicorn app.main:app --host 127.0.0.1 --reload
```

Без `COMPATIBILITY_API_KEY` все маршруты `/api/*` отключены. При работе с API
передавайте ключ в заголовке `X-API-Key`; `/health` и `/ready` остаются открытыми для
проверок процесса.

## Выгрузка в Google Sheets

Каждый пользователь подключает свой Google-аккаунт сам: бот создаёт таблицу на его
Google Диске и не получает доступа к остальным файлам. После подключения в выборе
формата появляется вариант «Google Sheets» — и для обычных, и для еженедельных отчётов.

Подготовка проекта Google (выполняется один раз администратором):

1. Создайте проект в [Google Cloud Console](https://console.cloud.google.com/).
2. Включите **Google Drive API** и **Google Sheets API**.
3. Настройте OAuth consent screen (тип External) и добавьте scope
   `https://www.googleapis.com/auth/drive.file`. Пока приложение в статусе Testing,
   добавьте пользователей в Test users.
4. Создайте OAuth 2.0 Client ID типа **Web application**.
5. В Authorized redirect URIs укажите ваш `GOOGLE_REDIRECT_URI`, например
   `https://reports.example.com/google/oauth/callback`.
6. Сгенерируйте ключ шифрования токенов: `python -m app.cli google-key`.

```env
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=https://reports.example.com/google/oauth/callback
GOOGLE_TOKEN_ENCRYPTION_KEY=<вывод python -m app.cli google-key>
```

`/google/oauth/callback` обслуживает FastAPI transport, поэтому для подключения аккаунтов
он должен быть запущен и доступен по HTTPS на адресе из `GOOGLE_REDIRECT_URI`. Маршрут
намеренно находится вне `/api/*`: браузер Google не передаёт `X-API-Key`, а подлинность
ответа подтверждает одноразовый параметр `state` со временем жизни
`GOOGLE_OAUTH_STATE_TTL_SECONDS` (по умолчанию 600 секунд). Сам бот работает по long
polling и во время формирования отчётов обращается к Google напрямую.

Refresh-токены хранятся в SQLite в зашифрованном виде (Fernet) и никогда не попадают в
логи, промпты или сообщения. Смена `GOOGLE_TOKEN_ENCRYPTION_KEY` делает сохранённые
подключения нечитаемыми — пользователям придётся подключиться заново через `/drive`.
Кнопка «Отключить Google Drive» удаляет сохранённый доступ; полностью отозвать
разрешение можно в настройках Google-аккаунта.

Отчёты крупнее `GOOGLE_SHEETS_MAX_ROWS` (по умолчанию 20000 строк) в Google Sheets не
выгружаются — для них используйте CSV или XLSX.

## Конфигурация

Скопируйте `.env.example` в `.env`. Секреты в репозиторий не добавляйте.

Основные режимы:

```env
# Полностью локальная демонстрация
DODO_MOCK_MODE=true
PLANNER_MOCK_MODE=true
TELEGRAM_BOT_TOKEN=...
TELEGRAM_PUBLIC_ACCESS=true
TELEGRAM_PUBLIC_UNIT_IDS=000d3a240c719a8711e68aba13f7f862

# Реальный OpenAI, mock Dodo
DODO_MOCK_MODE=true
PLANNER_MOCK_MODE=false
OPENAI_API_KEY=...

# Реальные OpenAI и Dodo IS
DODO_MOCK_MODE=false
PLANNER_MOCK_MODE=false
OPENAI_API_KEY=...
DODO_ACCESS_TOKEN=...
DODO_COUNTRY_ID=ru
```

Для `get-all-units` также задайте `DODO_BUSINESS_ID`. Если заведения не указаны в
запросе, используются `DEFAULT_UNIT_IDS`; при пустом значении сервис просит уточнение.

## Доступ Telegram

При `TELEGRAM_PUBLIC_ACCESS=true` любой пользователь личного чата получает временную роль
`viewer` без записи Telegram ID в SQLite. Ему доступны зарегистрированные метрики и только
подразделения из `TELEGRAM_PUBLIC_UNIT_IDS`; если список пуст, используются
`DEFAULT_UNIT_IDS`, а если и он пуст — конкретные UUID из локальных `data/units.json` и
`data/unit_aliases.json`. Поэтому при заполненном локальном каталоге отдельный список UUID
настраивать не требуется. Значение `*` явно открывает все подразделения:

```env
TELEGRAM_PUBLIC_ACCESS=true
TELEGRAM_PUBLIC_UNIT_IDS=*
```

Публичные пользователи не получают raw/dynamic-доступ. IDs из `ADMIN_TELEGRAM_IDS` получают
роль `admin`. Существующая запись пользователя в SQLite переопределяет публичные права, а
неактивная запись блокирует пользователя. Для точной индивидуальной настройки:

```bash
python -m app.bot.admin 123456789 --role manager \
  --reports sales,orders_count \
  --units 000d3a240c719a8711e68aba13f7f862
```

При `TELEGRAM_PUBLIC_ACCESS=false` действует закрытый режим: доступ имеют только IDs из
`ALLOWED_TELEGRAM_IDS`, `ADMIN_TELEGRAM_IDS` или SQLite.
Для raw/dynamic-отчётов правом типа отчёта считается `operation_id` выбранной GET-операции.
Администраторы и пользователи с `--reports '*'` могут выполнять такие отчёты; для остальных
нужно явно добавить разрешённый `operation_id`.

При запуске бот сразу проверяет обязательные параметры выбранного режима:
`TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY` при `PLANNER_MOCK_MODE=false` и
`DODO_ACCESS_TOKEN` при `DODO_MOCK_MODE=false`.

## Секреты и важные идентификаторы

Секретные значения храните только в локальном `.env`, переменных Railway/Docker и менеджере
паролей. Не отправляйте их в чат и не добавляйте в Git:

- `TELEGRAM_BOT_TOKEN`;
- `OPENAI_API_KEY`;
- `DODO_ACCESS_TOKEN`;
- `COMPATIBILITY_API_KEY` — только если используется совместимый HTTP API;
- `GOOGLE_CLIENT_SECRET` и `GOOGLE_TOKEN_ENCRYPTION_KEY` — только если включена выгрузка
  в Google Sheets.

Важные настройки, которые обычно не являются секретами: `DODO_COUNTRY_ID`,
`DODO_BUSINESS_ID`, `DEFAULT_UNIT_IDS`, `TELEGRAM_PUBLIC_UNIT_IDS`.

## Локальная демонстрация

В `.env`:

```env
DODO_MOCK_MODE=true
PLANNER_MOCK_MODE=true
DEFAULT_UNIT_IDS=000d3a240c719a8711e68aba13f7f862
```

Затем:

```bash
python -m app.cli build-index
python main.py
```

Mock Dodo проходит через тот же validator, chunker, paginator, aggregator и exporter,
что и реальный режим. Локальный planner предназначен для демонстрационных запросов и не
заменяет OpenAI при произвольных формулировках.

Пример API-запроса:

```bash
curl -X POST http://localhost:8000/api/reports \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $COMPATIBILITY_API_KEY" \
  -d '{"query":"Покажи выручку по дням с 1 по 10 мая 2026 по юниту 000d3a240c719a8711e68aba13f7f862","output_format":"table"}'
```

PowerShell:

```powershell
$body = @{
  query = "Покажи выручку по дням с 1 по 10 мая 2026 по юниту 000d3a240c719a8711e68aba13f7f862"
  output_format = "table"
} | ConvertTo-Json
Invoke-RestMethod http://localhost:8000/api/reports -Method Post `
  -ContentType application/json -Headers @{"X-API-Key"=$env:COMPATIBILITY_API_KEY} -Body $body
```

Для CSV/XLSX установите `output_format` в `csv` или `xlsx`, затем откройте URL из
поля `download`.

## Индекс и каталог заведений

```bash
python -m app.cli build-index
python -m app.cli inspect-docs
python -m app.cli list-operations
python scripts/sync_units.py
```

PowerShell-команды те же после активации окружения. При старте hash ZIP сравнивается с
`documentation_meta`; изменившаяся документация автоматически перестраивает индекс.
Исходный ZIP не извлекается и не изменяется.

`sync_units.py` вызывает только allowlisted `get-all-units`, обрабатывает pagination и
сохраняет каталог в `data/units.json`. Ручные псевдонимы хранятся в
`data/unit_aliases.json`; пример — `data/unit_aliases.example.json`. Неоднозначное
частичное имя возвращает варианты, а не случайный UUID.

## Совместимые HTTP endpoints

Все маршруты `/api/*` требуют `X-API-Key`, совпадающий с `COMPATIBILITY_API_KEY`.
Если ключ не настроен, совместимый API закрыт.

- `GET /` — уведомление о переходе в Telegram; старый browser UI отключён.
- `GET /health` — процесс жив.
- `GET /ready` — SQLite и индекс готовы.
- `GET /google/oauth/callback` — OAuth redirect Google; вне `/api/*`, защищён одноразовым
  `state`.
- `GET /api/documentation/stats` — безопасная статистика индекса.
- `GET /api/documentation/search?q=...` — локальный поиск.
- `GET /api/metrics` — реестр метрик.
- `POST /api/reports` — отчёт.
- `GET /api/reports/{report_id}/download` — сохранённый CSV/XLSX.

Download принимает только случайный report ID из базы. Пользовательский путь не
используется, path traversal отклоняется.

## Поддерживаемые метрики

Продажи:

- `sales`, `orders_count`, `average_check`, `sales_by_channel`.

Доставка:

- `delivery_sales`, `delivery_orders_count`;
- `average_delivery_time_seconds`, `average_cooking_time_seconds`;
- `average_heated_shelf_time_seconds`, `average_trip_time_seconds`;
- `late_orders_count`, `courier_app_usage_percent`;
- `orders_per_courier_hour`, `courier_utilization_percent`.

Клиенты и персонал:

- `new_clients_count`, `dine_in_new_clients_count`;
- `delivery_takeaway_new_clients_count`, `old_clients_count`;
- `labor_hours`, `productivity_sales`, `sales_per_labor_hour`;
- `products_per_labor_hour`, `productivity_average_heated_shelf_time`.

Операционные:

- `vouchers_count`;
- `ingredient_stops_count`, `ingredient_stop_duration_hours`;
- `channel_stops_count`, `channel_stop_duration_hours`;
- `workload_orders_count`, `workload_products_count`.

`average_check` считается как `sum(sales) / sum(ordersCount)`, а не среднее дневных
значений. Проценты считаются из сумм компонентов. Средние времена объединяются
взвешенно. При нулевом знаменателе возвращается `null`. Интервалы стопов обрезаются
периодом, дедуплицируются и объединяются; открытый стоп ограничивается текущим временем
или концом bucket.

## Allowlist операций

Executor разрешает только:

```text
get-finances-sales-daily-units
get-finances-sales-period-units
get-delivery-statistics
get-orders-client-statistics
get-staff-productivity
get-delivery-vouchers
get-production-stop-sales-saleschannels
get-production-stop-sales-statistics-ingredients
get-production-unit-workload-by-orders
get-production-unit-workload-by-products
get-all-units
```

Список хранится в `config/allowed_operations.yaml`. По умолчанию `INDEX_ALL_GET_OPERATIONS=true`
добавляет остальные GET в поиск, но executor их не выполнит. URL выбирается только из
нормализованной документации, схема должна быть HTTPS, разрешены только `api.dodois.io`
и `api.dodois.com`.

Лимиты периода, заведений и pagination находятся в
`config/endpoint_overrides.yaml`. Unit-группы ограничены 30 элементами. Периоды
разбиваются без пропусков и пересечений; календарная неделя начинается в понедельник.

## Режим raw: все проиндексированные GET-операции

Операции из реестра метрик имеют проверенные формулы, поэтому для них считаются точные
показатели. Остальные проиндексированные GET-операции доступны в режиме `raw`: backend
заранее выводит контракт запроса и форму ответа из документации, а строки возвращаются
таблицей без вычислений.

```env
ALLOW_ALL_GET_OPERATIONS=true
```

Все актуальные GET-операции индексируются и разрешены по умолчанию. Чтобы ограничить
выполнение только проверенным вручную списком из `config/allowed_operations.yaml`, задайте
`ALLOW_ALL_GET_OPERATIONS=false`.

Что выводится автоматически из индекса (`app/dodo/profile.py`):

- пара параметров периода (`from`/`to`, `fromDate`/`toDate` либо единственная пара
  `*From`/`*To` строкового типа) и режим даты;
- параметр заведений (`units`, `unitIds`, `unitUuids`, `unit`, `unitId`);
- pagination — только когда есть одновременно `skip`, `take` и `isEndOfListReached`;
- коллекция строк ответа — единственный массив верхнего уровня;
- параметры пути (`{manufactureId}` и подобные) и обязательные параметры, которые
  planner передаёт только тогда, когда значение явно есть в запросе пользователя.

`countryId` и `businessId` подставляются из `DODO_COUNTRY_ID` и `DODO_BUSINESS_ID`.

Приоритет остаётся у метрик. Для остальных endpoint backend принудительно использует
`mode=raw`, даже если модель предложила собственное поле или формулу. Неизвестные,
дублирующиеся и неверно расположенные параметры нормализуются по контракту endpoint;
отсутствующие обязательные значения возвращаются как уточняющий вопрос, а не ошибка 422.
Записи из `config/endpoint_overrides.yaml` всегда
перекрывают выведенные значения, потому что содержат неописуемые в схеме факты —
например, лимит в 10 дней для `get-finances-sales-daily-units` и округление до часов
для `get-staff-productivity`.

Ограничения вывода:

- `max_period_days` из схемы не выводится, для новых операций берётся
  `RAW_DEFAULT_MAX_PERIOD_DAYS`; неверное значение вернётся как ошибка 400 от Dodo IS.
- Операции с обязательным идентификатором (например, `{manufactureId}`) работают
  только если значение указано в запросе пользователя.
- Если ответ содержит несколько массивов, planner обязан указать `raw_collection`.
- Число строк ограничено `RAW_MAX_ROWS`.

Запросы к операциям, на которые у токена нет прав, ожидаемо завершатся ошибкой 403.
В `details.required_scopes` возвращается список scopes, которых не хватает токену.

Mock-режим покрывает только одиннадцать курируемых операций. Для остальных
`DODO_MOCK_MODE=true` вернёт понятную ошибку: их нужно вызывать с реальным
`DODO_ACCESS_TOKEN`.

## Разработка и проверки

```bash
pytest
ruff check .
ruff format --check .
```

Через uv:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Тесты не обращаются к реальным OpenAI/Dodo API. Используются mock planner,
`httpx.MockTransport`, настоящий SQLite FTS5 и mock Dodo pipeline.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

PowerShell:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Каталог `data/` монтируется с хоста, поэтому индекс и сформированные файлы сохраняются
между перезапусками. Контейнер работает как long-polling worker, открытый порт не требуется.
Запускайте только одну polling-реплику, чтобы один Telegram token не обрабатывался несколькими
consumer одновременно.

### Railway

Разворачивайте репозиторий как worker из `Dockerfile`. Добавьте переменные из `.env.example`
в Railway Variables, подключите persistent volume к `/app/data` и используйте одну реплику.
Публичный домен и HTTP health check для polling worker не нужны. Если инфраструктура требует
HTTP health check, запускайте сохранённый FastAPI transport отдельным service-процессом:

```bash
COMPATIBILITY_API_KEY=replace-with-a-long-random-value \
  uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
```

## Добавление метрики

1. Добавьте определение в `config/metrics.yaml`: operation, collection, aggregation,
   исходные/весовые поля, допустимые granularities и groups.
2. Добавьте русские/английские aliases в `config/metric_aliases.yaml`.
3. Убедитесь, что все поля существуют в `/api/documentation/search`.
4. Если нужен новый тип вычисления, добавьте отдельную функцию в
   `app/reports/aggregations.py` и вызов в `ReportAggregator`.
5. Добавьте unit и integration tests. Формулу в prompt добавлять нельзя.

## Добавление endpoint

1. Endpoint должен существовать в ZIP и быть GET.
2. Добавьте ID в `config/allowed_operations.yaml` только после проверки безопасности.
3. Опишите лимиты и pagination в `config/endpoint_overrides.yaml`.
4. Привяжите к нему метрику и проверенные поля ответа.
5. Перестройте индекс и добавьте HTTP/chunk/pagination tests.

Модель никогда не передаёт URL executor-у.

## Безопасность и аудит

- Секреты читаются только из окружения и не логируются.
- Dodo token не попадает в OpenAI context, SQLite или ответы.
- Пользовательский текст не вставляется через `innerHTML`.
- SQL FTS выражение строится из очищенных токенов и передаётся параметром.
- Все операции, метрики, поля, группы, фильтры и UUID проверяются backend-ом.
- Retry ограничены; `429 Retry-After` и временные `5xx` обрабатываются.
- Клиент не получает traceback.
- В `report_runs` хранятся status, operation/metric IDs, counters, duration и usage.
- `STORE_USER_QUERIES=false` оставляет только SHA-256 пользовательского запроса.

## Расход OpenAI-токенов

При подтверждённом отчёте выполняется один planner-вызов. Свободный текст может потребовать
дополнительный preflight-вызов перед выбором формата или уточняющим вопросом. Контекст содержит максимум
`RETRIEVAL_TOP_K` компактных кандидатов, по 25 релевантных полей. Статический system
prompt отделён от динамического input, чтобы провайдер мог кэшировать его. Usage из SDK
возвращается в `execution.openai_input_tokens` и `openai_output_tokens`. CSV/XLSX,
агрегация и резюме не расходуют LLM-токены.

Для planner используется `reasoning.effort=minimal`. Минимальный лимит ответа — 3000
токенов: Responses API учитывает в нём как видимый JSON, так и reasoning-токены.

## Демонстрационные запросы

```text
Покажи выручку и количество заказов по дням с 1 по 10 мая 2026 по юнитам 000d3a240c719a8711e68aba13f7f862 и 000d3a240c719a8711e68aba13f7fc8a
Посчитай средний чек за май 2026 по юниту 000d3a240c719a8711e68aba13f7f862
Покажи среднее время доставки по дням за прошлую неделю по юниту 000d3a240c719a8711e68aba13f7f862
Сколько было новых клиентов в июне 2026 по юниту 000d3a240c719a8711e68aba13f7f862
Сколько часов ингредиенты находились в стопе за май 2026 с группировкой по ингредиентам
Сформируй XLSX с производительностью по заведениям за июнь 2026
```

## Ограничения MVP и расхождения документации

- FSM хранится в памяти процесса: после перезапуска незавершённый диалог начинается заново.
- Используется long polling и асинхронная обработка без Redis/очереди; архитектура допускает
  замену MemoryStorage и per-process guards на Redis перед горизонтальным масштабированием.
- Защищённый Web API временно сохранён для совместимости. Старый browser UI отключён,
  а его исходные assets пока оставлены в репозитории для безопасного отката миграции.
- Нет графиков и BI-конструктора.
- Локальный planner распознаёт ограниченный набор демонстрационных формулировок и
  не строит raw-планы.
- Текстовые названия заведений требуют предварительно синхронизированного каталога.
- Автоматически вычисляются только перечисленные метрики и группировки. Остальные
  операции доступны в режиме `raw` как таблица без агрегации.
- `LLM_SUMMARY_ENABLED` зарезервирован для будущего summary provider; второй вызов сейчас
  не выполняется даже при включении.
- Исходно в рабочей папке был только распакованный
  `Dodo_IS_API_Reference_Sorted.json`; ожидаемый ZIP был создан без изменения JSON.
- Реальная документация использует поля `deliverySales`,
  `avgDeliveryOrderFulfillmentTime`, `avgCookingTime`, `avgHeatedShelfTime` и
  `avgOrderTripTime` вместо предположительных названий из задания. Реестр использует
  реальные поля.
- Реальный ZIP содержит 119 операций, из них 103 GET; при текущих фильтрах индексируется
  97 не-deprecated GET-операций.
