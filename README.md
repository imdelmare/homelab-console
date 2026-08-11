# Homelab Console

Run your homelab with a small local AI — without giving the model your
credentials or a shell.

Homelab Console turns Proxmox, OPNsense, Home Assistant, Frigate, AdGuard and
other homelab systems into predictable typed tools. It keeps tasks, approvals,
provider access and audit history in one local control plane, while the
reasoning model remains replaceable.

[Explore the website](https://imdelmare.github.io/homelab-console/) ·
[Read the field manual](https://imdelmare.github.io/homelab-console/docs/)

## Why Homelab Console?

- **Local-first:** run the control plane in your own network. Cloud AI is optional.
- **Useful with smaller models:** compact summaries and strict schemas reduce the
  amount of context and reasoning a model needs.
- **No direct authority:** models never receive provider credentials, arbitrary
  shell access or a raw provider API.
- **Human-approved writes:** every infrastructure change is narrow, input-bound,
  single-use and explicitly approved by the operator.
- **One audit trail:** the web console, watchers, Telegram, REST and authenticated
  MCP clients all pass through the same execution core.

The core also works with every AI runtime disabled, so you can connect your
infrastructure first and add local reasoning later.

## Quick start with Docker Compose

### 1. Requirements

- Docker Engine with the Compose plugin
- Git
- A Telegram bot and your Telegram user/chat IDs for approvals and second factor

### 2. Prepare local configuration

```bash
git clone https://github.com/imdelmare/homelab-console.git
cd homelab-console

cp deploy/compose/env.example .env.compose
cp config/homelab.example.yml config/homelab.local.yml
cp config/secrets.local.example.yml config/secrets.local.yml
```

Edit the three copied files before starting. At minimum, replace the placeholder
database password, session secret, Telegram values and webhook secret in
`.env.compose`. Add only systems you own to `config/homelab.local.yml`, and keep
real provider credentials in `config/secrets.local.yml`.

For a temporary loopback-only HTTP evaluation, set `COOKIE_SECURE=false`. Keep
the default loopback bindings; use a controlled TLS proxy or tunnel before
exposing the console beyond the local machine.

### 3. Start the reviewed images

```bash
docker compose -f deploy/compose/compose.ghcr.yaml --env-file .env.compose config --quiet
docker compose -f deploy/compose/compose.ghcr.yaml --env-file .env.compose pull
docker compose -f deploy/compose/compose.ghcr.yaml --env-file .env.compose up -d
docker compose -f deploy/compose/compose.ghcr.yaml --env-file .env.compose ps
```

Wait until `db`, `api`, `mcp-http` and `web` are healthy.

### 4. Create the first account

```bash
docker compose -f deploy/compose/compose.ghcr.yaml --env-file .env.compose   run --rm api python -m app.cli create-admin
```

The command is interactive, hides the password while you type, and displays
recovery codes once. Then open <http://127.0.0.1:8080>.

For backups, upgrades, immutable image digests and reverse-proxy guidance, use
the complete [Community Compose guide](deploy/compose/README.md).

## How it stays safe

```text
operator / local model / MCP client
                 │
                 ▼
      typed input + policy checks
                 │
                 ▼
       shared execution core
        │ approval for writes
        │ redaction and audit
                 ▼
       declared provider target
```

There is deliberately no arbitrary shell, SSH, caller-selected URL, raw API
forwarding or Docker socket tool. Provider targets come from local inventory,
and approved write capabilities perform a post-action read-back where supported.

## Choose your path

| Goal | Start here |
|---|---|
| Install the Community Edition | [Community Compose](deploy/compose/README.md) |
| Learn the operator workflow | [Getting started](https://imdelmare.github.io/homelab-console/docs/getting-started) |
| Connect infrastructure | [Provider guide](docs/providers.md) |
| Connect Claude, Codex, OpenCode or Cline | [MCP guide](docs/mcp.md) |
| Understand the trust model | [Security model](docs/security.md) |
| Understand the internals | [Architecture](docs/architecture.md) |

## Repository map

- `apps/api` — FastAPI control plane, providers, tasks, approvals and audit.
- `apps/web` — framework-free TypeScript Quiet Operations console.
- `apps/mcp` — authenticated stdio and streamable HTTP MCP adapter.
- `apps/docs` — VitePress field manual.
- `deploy/compose` — source-build and reviewed-image deployment contracts.
- `config` — safe examples for inventory, credentials and runbooks.
- [External Sentinel](https://github.com/imdelmare/homelab-console-sentinel) — optional, independently released availability observer.

## Development

The backend supports Python 3.12 and 3.13 and requires PostgreSQL. The web
console uses Node.js 22. To build from source and run the complete validation
suite, follow the [development section of the field manual](https://imdelmare.github.io/homelab-console/docs/getting-started#local-validation).

Never commit a populated `.env`, credentials, local inventory or recovery codes.

## About this public repository

Public `main` is a reviewed, allowlisted Community Edition export. It contains
the exact source used for public API, MCP and web images, but intentionally
excludes live inventory, credentials, activation records, deployment drills and
operator-only runbooks.

## License

Licensed under the [Apache License 2.0](LICENSE). Third-party attribution and
license notes are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
