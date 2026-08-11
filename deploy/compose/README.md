# Community Compose

This is the supported model-independent Homelab Console deployment. It starts
only PostgreSQL, API, web and MCP HTTP. It does not install or start Sentinel,
LAN models, Ollama, the legacy Fixer or an external remediation worker.
Conversation Service, Task Router inference and Telegram media analysis are
disabled in both Compose contracts; the core remains healthy without an AI
runtime.

## Prepare

Run from the repository root:

```bash
cp deploy/compose/env.example .env.compose
cp config/homelab.example.yml config/homelab.local.yml
cp config/secrets.local.example.yml config/secrets.local.yml
```

Edit `.env.compose`, `config/homelab.local.yml` and
`config/secrets.local.yml`. Never commit those files. Generate independent
random values for `POSTGRES_PASSWORD`, `SESSION_SECRET` and
`TELEGRAM_WEBHOOK_SECRET`.

The default host bindings are loopback-only. Keep them that way and terminate
TLS in a controlled reverse proxy or tunnel. If you temporarily evaluate the
console directly over HTTP, set `COOKIE_SECURE=false`; do not use that setting
for an Internet-facing deployment.

## Validate and start

For a source checkout, build the images locally:

```bash
docker compose -f deploy/compose/compose.yaml --env-file .env.compose config --quiet
docker compose -f deploy/compose/compose.yaml --env-file .env.compose up -d --build
docker compose -f deploy/compose/compose.yaml --env-file .env.compose ps
```

For the reviewed public images, use the standalone GHCR contract instead. It
contains no `build:` entries and therefore cannot silently build a different
checkout:

```bash
docker compose -f deploy/compose/compose.ghcr.yaml --env-file .env.compose config --quiet
docker compose -f deploy/compose/compose.ghcr.yaml --env-file .env.compose pull
docker compose -f deploy/compose/compose.ghcr.yaml --env-file .env.compose up -d
docker compose -f deploy/compose/compose.ghcr.yaml --env-file .env.compose ps
```

The image variables in `.env.compose` default to the public `main` tags. For a
repeatable deployment, replace each complete image reference with the reviewed
release digest, for example
`ghcr.io/imdelmare/homelab-mcp-api@sha256:<digest>`.
`VITE_API_BASE_URL` and `VITE_MCP_ENDPOINT` are source-build arguments and do
not reconfigure the immutable GHCR web image; the published image uses
same-origin `/api` routing and the default MCP endpoint unless a separately
reviewed image variant is built.

Wait until `db`, `api`, `mcp-http` and `web` are healthy. The API applies
pending Alembic migrations before it becomes ready.

## Create the first account

The live runtime rejects password bootstrap through environment variables. Use
the interactive, first-account-only command after the API has completed its
migrations:

```bash
docker compose -f deploy/compose/compose.yaml --env-file .env.compose \
  run --rm api python -m app.cli create-admin
```

Use the same command with `compose.ghcr.yaml` when running the published images.

The command requires an interactive terminal and reads the password without
terminal echo. Recovery codes are shown once, after the account and audit event
commit. The command refuses to create another account when one already exists.

## Operations

Set `COMPOSE_FILE` to the contract used for this installation:

```bash
COMPOSE_FILE=deploy/compose/compose.yaml
# For published images instead:
# COMPOSE_FILE=deploy/compose/compose.ghcr.yaml

# Health through the web proxy
# Replace 8080 if WEB_PORT has been changed in .env.compose.
curl --fail http://127.0.0.1:8080/health

# Logs
docker compose -f "$COMPOSE_FILE" --env-file .env.compose logs -f api

# Logical database backup
docker compose -f "$COMPOSE_FILE" --env-file .env.compose \
  exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' \
  > homelab-console.backup

# Stop without deleting state
docker compose -f "$COMPOSE_FILE" --env-file .env.compose down
```

Never add `-v` to `down` unless the PostgreSQL data is intentionally being
destroyed and a verified backup exists. Before an upgrade, create a logical
backup, pull/build the new images, then start the stack and verify all four
healthchecks. This deployment supports one API replica; do not run concurrent
Alembic migrations from multiple API replicas.
