#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ -x "$repo_root/.venv/bin/python" ]]; then
  python_runner=("$repo_root/.venv/bin/python")
  runner=("$repo_root/.venv/bin/python" -m)
elif command -v uv >/dev/null 2>&1; then
  python_runner=(uv run --no-sync python)
  runner=(uv run --no-sync)
elif command -v python3.12 >/dev/null 2>&1; then
  python_runner=(python3.12)
  runner=(python3.12 -m)
elif command -v python >/dev/null 2>&1; then
  python_runner=(python)
  runner=(python -m)
else
  echo "Python 3.12 or uv is required. Install the declared dev dependencies first." >&2
  exit 127
fi

if ! "${python_runner[@]}" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  echo "Python 3.12 or newer is required." >&2
  exit 2
fi

"${runner[@]}" pytest
"${runner[@]}" ruff check .
"${runner[@]}" ruff format --check .
