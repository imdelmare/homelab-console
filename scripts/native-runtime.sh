#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="$ROOT_DIR/apps/api"
WEB_DIR="$ROOT_DIR/apps/web"
MCP_DIR="$ROOT_DIR/apps/mcp"

if [[ ! -f "$ROOT_DIR/.env" ]]; then
  echo "Missing live environment: $ROOT_DIR/.env" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1091
source "$ROOT_DIR/.env"
if [[ -f "$ROOT_DIR/.env.native" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env.native"
fi
set +a

# Compose uses the service hostname `db`; native services reach PostgreSQL on
# the host loopback address. Preserve credentials and all URL options.
native_database_host="${NATIVE_DATABASE_HOST:-127.0.0.1}"
DATABASE_URL="${DATABASE_URL//@db:/@${native_database_host}:}"
if [[ -n "${NATIVE_DATABASE_PORT:-}" ]]; then
  DATABASE_URL="${DATABASE_URL%%@*}@${native_database_host}:${NATIVE_DATABASE_PORT}/${DATABASE_URL#*@*/}"
fi
export DATABASE_URL

export HOMELAB_CONFIG_PATH="${HOMELAB_CONFIG_PATH:-$ROOT_DIR/config/homelab.local.yml}"
export SECRETS_PATH="${SECRETS_PATH:-$ROOT_DIR/config/secrets.local.yml}"

wait_for_database() {
  local attempt
  local max_attempts=120

  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    if "$API_DIR/.venv/bin/python" -c '
import os
import psycopg

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
with psycopg.connect(url, connect_timeout=2) as connection:
    connection.execute("SELECT 1")
' >/dev/null 2>&1; then
      echo "PostgreSQL is ready."
      return 0
    fi

    if ((attempt == 1 || attempt % 10 == 0)); then
      echo "Waiting for PostgreSQL (${attempt}/${max_attempts})..." >&2
    fi
    sleep 1
  done

  echo "PostgreSQL did not become ready within ${max_attempts} seconds." >&2
  return 1
}

case "${1:-}" in
  wait-db)
    wait_for_database
    ;;
  migrate)
    cd "$API_DIR"
    exec "$API_DIR/.venv/bin/alembic" upgrade head
    ;;
  api)
    cd "$API_DIR"
    exec "$API_DIR/.venv/bin/uvicorn" app.main:app \
      --host "${APP_HOST:-127.0.0.1}" \
      --port "${APP_PORT:-8000}"
    ;;
  mcp)
    cd "$ROOT_DIR"
    exec "$API_DIR/.venv/bin/python" "$MCP_DIR/server.py" \
      --transport streamable-http
    ;;
  build-web)
    cd "$WEB_DIR"
    # Keep content-hashed chunks from the previous release so browser tabs
    # opened before a deploy can still finish their dynamic imports.
    exec npm run build -- --emptyOutDir=false
    ;;
  *)
    echo "Usage: $0 {wait-db|migrate|api|mcp|build-web}" >&2
    exit 2
    ;;
esac
