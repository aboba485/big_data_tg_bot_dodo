# Repository instructions

## Project

This repository contains a Python 3.12 aiogram Telegram bot with a retained FastAPI compatibility
transport. It converts natural-language requests into validated Dodo IS report plans and
produces text, JSON, CSV, or XLSX output.
Read `README.md` and `docs/ARCHITECTURE.md` before changing code.

## Safety and scope

- Check `git status --short` before editing. Preserve unrelated and uncommitted user changes.
- Never read, print, edit, restore, or commit `.env` files, credentials, tokens, or secrets.
- Do not deploy, release, publish, push, merge, or install new dependencies unless the user
  explicitly authorizes the action after an explanation.
- Do not weaken tests, validation, allowlists, or security controls to make checks pass.
- Keep changes minimal and limited to the requested behavior. Do not refactor unrelated code.
- Avoid commands that call real OpenAI or Dodo IS services. `scripts/sync_units.py` is not a
  verification command because it requires credentials and network access.

## Team workflow

For feature implementation, bug fixes, and refactoring, use the repository skill at
`.codex/skills/coding-team/SKILL.md`. For each non-trivial task:

1. Read these instructions and relevant documentation.
2. Check Git status and identify user-owned changes.
3. Ask an Explorer subagent for read-only discovery of related files, dependencies, risks,
   and existing tests.
4. Write a short implementation plan.
5. Implement the smallest complete solution.
6. Ask a Tester subagent to run real checks and inspect edge cases and regressions.
7. Fix confirmed defects and run the project checks.
8. Ask a Reviewer subagent to independently inspect Git status, the final diff, and every
   task-related untracked file before editing.
9. Fix all `critical` and `high` findings, then rerun the checks.
10. Report changes, changed files, commands actually run, outcomes, Reviewer findings, and
    remaining limitations.

The primary Codex agent acts as Tech Lead and programmer. Specialized roles:

- **Explorer:** read-only; traces architecture, related files, dependencies, tests, and risks.
- **Tester:** validates behavior, edge cases, and regressions with real commands; may propose or
  add tests; never reports a pass without a successful command result.
- **Reviewer:** independently reviews Git status, the final diff, and task-related untracked files
  before making edits; reports findings as `critical`, `high`, `medium`, or `low`, including file
  and line evidence.

## Supported commands

Install declared development dependencies:

```bash
uv sync --extra dev
# or, in an activated Python 3.12 virtual environment:
python -m pip install -e ".[dev]"
```

Run the Telegram bot after building the local documentation index:

```bash
python -m app.cli build-index
python main.py
```

The retained web compatibility API can still be run with
`COMPATIBILITY_API_KEY=<secret> uvicorn app.main:app --host 127.0.0.1 --reload`.
Never expose `/api/*` without its `X-API-Key` protection.

Run the complete repository verification:

```bash
./scripts/verify.sh
```

Windows PowerShell:

```powershell
.\scripts\verify.ps1
```

The unified scripts run only checks configured by this repository:

```bash
pytest
ruff check .
ruff format --check .
```

There is currently no configured static type checker or standalone package-build verification.
Do not claim type-check or build success unless such a command is added and actually succeeds.

## Architecture constraints

- Keep `app/services.py` as the shared composition boundary, `app/main.py` as the compatibility
  HTTP transport, `app/bot/` as the Telegram transport, and `app/report_service.py` as use-case
  orchestration.
- Keep documentation loading/indexing in `app/documentation`, local retrieval in
  `app/retrieval`, planning/schema validation in `app/planner`, Dodo execution in `app/dodo`,
  deterministic calculations/export in `app/reports`, and persistence in `app/storage`.
- The model may create typed plans, but it must not choose executable URLs or define business
  formulas. URLs come from normalized documentation; formulas remain deterministic Python/YAML.
- Preserve backend validation of operations, metrics, fields, filters, units, periods, and
  pagination. Preserve the HTTPS host restrictions and operation allowlist behavior.
- Keep secrets and raw Dodo responses out of prompts, logs, SQLite audit data, and client errors.
- Keep Telegram handlers free of SQL, report formulas, and direct Dodo/OpenAI access. Enforce
  the configured public/stored-user policy, role, report-type, and unit access before report
  execution.
- Changes to metrics must update the relevant `config/*.yaml` files and tests. New executable
  endpoints require documentation presence, GET semantics, security review, limits/pagination
  configuration, and tests.
- Treat `Dodo_IS_API_Reference_Sorted.zip` as an input artifact; do not extract or rewrite it.
- Generated SQLite/index/export files belong under ignored paths in `data/`.

## Definition of done

- The requested behavior is complete without unrelated business-logic changes.
- Relevant tests are added or updated and real verification commands are run.
- `pytest`, `ruff check .`, and `ruff format --check .` pass, or each unavailable/failing check is
  reported exactly with its command and reason.
- The final diff contains no secrets, generated data, debug artifacts, or accidental user changes.
- Reviewer `critical` and `high` findings are resolved; lower-severity findings are reported.
- Never state that tests, lint, formatting, type checks, or builds passed unless the corresponding
  command completed successfully in the current work.
