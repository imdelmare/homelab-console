# Getting Started

Homelab Console is a Python and React control plane intended to run close to the
infrastructure it manages. PostgreSQL stores canonical state; provider adapters
reach declared systems through narrow, typed tools.

## Repository map

| Path | Purpose |
|---|---|
| `apps/api` | FastAPI control plane, execution core, providers, and workers |
| `apps/web` | React operator desktop |
| `apps/mcp` | MCP adapter over stdio and streamable HTTP |
| `apps/sentinel` | Independent external availability sentinel |
| `config` | Inventory examples and local-only provider configuration |
| `docs` | Canonical architecture, security, and operating documentation |
| `deploy` | systemd, Caddy, and fallback container definitions |

## Read the system before deploying it

1. Start with [Architecture](./architecture.md) and the five-plane model.
2. Review the non-negotiable boundaries in [Security](./security.md).
3. Understand identities and sessions in [Authentication](./authentication.md).
4. Learn how external agents connect through the [MCP adapter](./mcp.md).
5. Review the normalized integration contracts in [Providers](./providers.md).

## Local validation

The API requires Python 3.12 or newer and PostgreSQL. The web application uses
Node.js and npm.

```bash
cd apps/api
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Run the repository validation suites from the project root:

```bash
./scripts/run-test-suite.sh smoke
./scripts/run-test-suite.sh ui
./scripts/run-test-suite.sh all
```

For frontend-only development:

```bash
cd apps/web
npm ci
npm run dev
```

::: warning DO NOT COPY A LIVE INVENTORY
Start from the example configuration files. Never commit provider credentials,
real tokens, private inventory, or an operator's local environment files.
:::
