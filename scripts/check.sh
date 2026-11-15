#!/usr/bin/env bash
# MiniHCI quality gate. Exits non-zero on any failure. Protected from autopilot edits.
#   bash scripts/check.sh              full gate (all tests, CRASH_ITERS default 20)
#   CHECK_FAST=1 bash scripts/check.sh fast gate for every commit (skips tests marked slow)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

if [ ! -f pyproject.toml ]; then
  echo "check.sh: no pyproject.toml yet (project not scaffolded); nothing to check"
  exit 0
fi

if uv run --quiet ruff --version >/dev/null 2>&1; then
  echo "== ruff"
  uv run ruff check src tests
  uv run ruff format --check src tests
else
  echo "== ruff not installed (add it: uv add --dev ruff); skipping lint"
fi

echo "== pytest"
export CRASH_ITERS="${CRASH_ITERS:-20}"
if [ "${CHECK_FAST:-0}" = "1" ]; then
  export CRASH_ITERS=5
  uv run pytest -q -m "not slow and not real_hosts" -x
else
  uv run pytest -q -m "not real_hosts"
fi

if [ -f ui/package.json ]; then
  echo "== ui"
  npm --prefix ui run --silent typecheck
  npm --prefix ui run --silent build
fi
echo "check.sh: OK"
