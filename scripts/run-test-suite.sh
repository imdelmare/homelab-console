#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="$ROOT_DIR/apps/api"
WEB_DIR="$ROOT_DIR/apps/web"
PYTHON_BIN="$API_DIR/.venv/bin/python"

# API tests create and drop randomly named PostgreSQL databases. Reuse the
# configured server connection without ever modifying the canonical database.
if [[ -z "${TEST_DATABASE_URL:-}" && -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
  test_database_host="${NATIVE_DATABASE_HOST:-127.0.0.1}"
  TEST_DATABASE_URL="${DATABASE_URL//@db:/@${test_database_host}:}"
  export TEST_DATABASE_URL
fi

# Keep API tests deterministic even when local development config files exist.
export RUNBOOKS_CONFIG_PATH="config/runbooks.test-missing.yml"
export WATCHERS_ENABLED="true"
export WATCHERS_INTERVAL_SECONDS="300"
export WATCHERS_MIN_SEVERITY="warning"
export WATCHERS_IGNORE_PATTERNS=""
export WATCHERS_RESOLVE_AFTER_MISSING_RUNS="3"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing API virtualenv python at $PYTHON_BIN" >&2
  echo "Create the apps/api virtualenv before running test suites." >&2
  exit 2
fi

run_api_pytest() {
  (cd "$API_DIR" && "$PYTHON_BIN" -m pytest "$@")
}

run_web_build() {
  (cd "$WEB_DIR" && npm run build)
}

usage() {
  cat <<'EOF'
Usage: scripts/run-test-suite.sh <suite>

Suites:
  smoke     Suite 1: health/auth/core list endpoints
  auth      Suite 2: auth service and API flow
  tasks     Suite 3: task lifecycle service/API coverage
  watchers  Suite 4: watcher incident flow and granular watcher tests
  tools     Suite 5: execution/tool-router safety coverage
  mcp       Suite 6: MCP adapter/client registration coverage
  ui        Suite 7: UI regression gate (TypeScript + Vite build)
  sentinel  External Sentinel standalone tests
  all       API tests + web build + Sentinel tests
EOF
}

suite="${1:-}"
case "$suite" in
  smoke)
    run_api_pytest tests/test_smoke_suite.py
    ;;
  auth)
    run_api_pytest tests/test_auth.py tests/test_telegram.py
    ;;
  tasks)
    run_api_pytest tests/test_tasks_service.py tests/test_tasks_api.py tests/test_e2e_handoff.py
    ;;
  watchers)
    run_api_pytest tests/test_watcher_flow_suite.py tests/test_watchers.py tests/test_failure_simulator.py
    ;;
  tools)
    run_api_pytest tests/test_execution.py tests/test_tool_governance.py tests/test_wave1_tools.py tests/test_network_safety.py
    ;;
  mcp)
    run_api_pytest tests/test_mcp_clients.py tests/test_mcp_adapter.py
    ;;
  ui)
    run_web_build
    ;;
  sentinel)
    "$PYTHON_BIN" -m pytest "$ROOT_DIR/apps/sentinel/tests"
    ;;
  all)
    run_api_pytest tests
    run_web_build
    "$PYTHON_BIN" -m pytest "$ROOT_DIR/apps/sentinel/tests"
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    echo "Unknown suite: $suite" >&2
    usage >&2
    exit 2
    ;;
esac
