# Architecture

## Purpose

Dodo IS Natural Reports is a Python 3.12 aiogram Telegram bot with a retained FastAPI
compatibility API. It turns a Russian-language request into a validated report plan, fetches
allowed Dodo IS data, calculates deterministic metrics, and returns text, JSON, CSV, or XLSX.

OpenAI is limited to interpreting language and producing a typed plan. Documentation search,
validation, HTTP execution, pagination, chunking, calculations, export, and audit persistence run
locally.

## Request flow

```text
Telegram message or inline callback
  -> Telegram ID, role, report and unit access checks
Retained HTTP request
  -> compatibility API key check
Both transports
  -> transport validation
  -> local aliases and SQLite FTS5 retrieval
  -> typed planner result (OpenAI or local mock)
  -> backend plan and unit validation
  -> documentation-derived, allowlisted Dodo executor
  -> deterministic aggregation or raw schema flattening
  -> response plus optional CSV/XLSX export and audit record
```

`app/services.py` is the shared composition root. `app/bot/` provides long polling, middleware,
button and natural-language FSM flows, Telegram presentation, file delivery, and a single-process
weekly scheduler. Button selections create a validated plan directly, without an LLM planning
call. Unit selection is grouped by city after applying the current user's unit permissions, so
the hierarchy cannot widen access. A successfully validated natural-language plan is cached
in memory for the FSM lifetime and consumed once after button confirmation, avoiding a second
model call; report and unit permissions are rechecked before execution. `app/main.py` retains the FastAPI routes during
the migration. `app/report_service.py` coordinates the report use case without transport logic
or metric formulas.

## Components

| Path | Responsibility |
| --- | --- |
| `main.py` | Telegram long-polling entrypoint |
| `app/services.py` | Shared infrastructure and domain-service composition |
| `app/bot/` | aiogram routers, FSM, access/rate middleware, presentation, and delivery |
| `app/users/`, `app/storage/users.py` | Telegram roles, report/unit permissions, and SQLite repository |
| `app/storage/weekly_reports.py` | Weekly subscriptions, delivery claims, and duplicate-delivery journal |
| `app/google_drive/`, `app/storage/google_drive.py` | Per-user Google OAuth, encrypted token storage, and Google Sheets creation |
| `app/main.py` | Retained FastAPI compatibility routes and lifecycle |
| `app/report_service.py` | End-to-end report orchestration and export selection |
| `app/documentation/` | Load the bundled API ZIP, normalize operations, and maintain the SQLite FTS5 index |
| `app/retrieval/` | Normalize user queries and retrieve compact endpoint candidates locally |
| `app/planner/` | Typed plan schemas, OpenAI/mock planning, intent normalization, and plan validation |
| `app/dodo/` | Unit resolution, endpoint profiles, HTTPS client, chunking, pagination, mocks, and execution |
| `app/reports/` | Metric registry, deterministic aggregations, presentation, interval logic, and CSV/XLSX export |
| `app/storage/` | SQLite initialization, report-run audit data, and generated-file metadata |
| `config/` | Operation allowlist, endpoint overrides, metric definitions, and aliases |
| `app/templates/`, `app/static/` | Inactive legacy browser assets retained for migration rollback |
| `tests/` | Unit and API/integration-style tests using local mocks and temporary SQLite data |

## Dependency direction

- Telegram and FastAPI transports depend on the same composed report services; core modules do
  not depend on Telegram updates or HTTP request objects.
- Retained `/api/*` routes are disabled unless `COMPATIBILITY_API_KEY` is configured and require
  the matching `X-API-Key` header.
- `GET /google/oauth/callback` is deliberately outside `/api/*` because a browser redirect from
  Google cannot carry `X-API-Key`. It is authenticated by a single-use, TTL-bound OAuth `state`
  row and never returns report data.
- The orchestrator depends on retrieval, planning, validation, execution, reporting, and storage
  services assembled in `app/services.py`.
- Retrieval depends on normalized documentation and the metric registry, not on remote APIs.
- Planning produces schemas consumed by backend validation and execution. It does not select an
  arbitrary URL or execute requests.
- Allowed `salesChannel`, `orderSource`, and `paymentMethod` filters are applied to fetched
  records before deterministic aggregation; user text is never converted into SQL.
- Telegram permissions use metric IDs for registered reports and operation IDs for validated
  raw/dynamic reports.
- In public mode unknown private-chat users receive an ephemeral viewer identity. Public viewers
  can use registered metrics only and are limited to configured public unit IDs; stored users,
  inactive blocks, and administrators override the public defaults.
- Dodo execution obtains endpoint contracts from the documentation repository and applies
  profiles, allowlist rules, date/unit chunking, and pagination.
- Reporting consumes validated plans and fetched records; metric formulas remain deterministic
  Python/configuration rather than model-generated expressions.
- Storage is an adapter used by composition/orchestration and does not drive planning or domain
  calculations.
- Weekly subscriptions store no tokens. The scheduler rechecks current report and unit access,
  uses the previous completed Monday–Sunday interval, and claims each subscription/period before
  sending. A crash after Telegram accepts a message but before the local `sent` update can still
  cause a retry; strict exactly-once delivery is not available through the Telegram API.

## Data and external boundaries

- `Dodo_IS_API_Reference_Sorted.zip` is the source API contract. The application hashes and reads
  it locally and stores normalized index data in SQLite.
- OpenAI receives only compact endpoint candidates and planning context. Tokens, executable URLs,
  and raw Dodo responses must not enter prompts.
- Dodo requests must use documentation-derived HTTPS URLs on the permitted hosts and validated
  operations/parameters.
- SQLite/index databases and generated report files live under ignored `data/` paths.
- `.env` contains local configuration and secrets and must not be read, logged, or committed.
- Telegram IDs and permissions are stored in SQLite. Dodo IS, OpenAI, Telegram, and compatibility
  API secrets remain environment-only.
- Per-user Google OAuth refresh/access tokens are the one exception: they are stored in SQLite
  encrypted with Fernet under `GOOGLE_TOKEN_ENCRYPTION_KEY`, which itself stays in the
  environment. Decrypted tokens exist only in memory for the duration of one Google call and are
  excluded from application logs, prompts, audit rows, and client errors. The short-lived
  authorization code still appears in the redirect query string, so front-proxy access logs for
  `/google/oauth/callback` should be treated as sensitive. Google receives report columns,
  rows, and totals only when the user selects the `sheets` output format; the `drive.file` scope
  limits the grant to files the application creates.

## Change constraints

- Adding a metric requires synchronized changes to `config/metrics.yaml`, aliases when needed,
  deterministic aggregation support, and tests using documented response fields.
- Adding an executable endpoint requires a documented GET operation, security review, allowlist
  decision, limits/pagination overrides where necessary, and execution tests.
- Preserve backend validation of operations, fields, filters, UUIDs, periods, groups, pagination,
  and download paths.
- Tests must remain offline: use the mock planner, mock Dodo transport, and temporary SQLite
  storage. `scripts/sync_units.py` is an operational network command, not a test or verification
  step.

## Known project-level limitations

The repository currently configures pytest and Ruff only. No static type-checker command and no
standalone package-build verification command are defined. Docker Compose provides a documented
runtime build path but is not part of the unified local verification script. Telegram FSM,
rate counters, and in-flight report guards are process-local; production horizontal scaling
requires Redis-backed state/locks or a task queue. The polling deployment must use one consumer
replica per bot token.
