# Getting Started

Homelab Console is a Python and React control plane intended to run close to the
infrastructure it manages. PostgreSQL stores canonical state; provider adapters
reach declared systems through narrow, typed tools.

## See the workflow first

Start with the [Product Tour](./product-tour.md) to follow one synthetic incident
from the control room to task evidence, operator approval, and audit. It is the
fastest way to understand what the system does before reading implementation or
deployment details.

The first useful path in a configured console is:

```text
Open Overview
  → inspect one provider observation
  → run a read-only summary tool
  → review the resulting task evidence
  → pair an MCP client
  → inspect the audited invocation
```

## Repository map

| Path | Purpose |
|---|---|
| `apps/api` | FastAPI control plane, execution core, providers, and workers |
| `apps/web` | React operator desktop |
| `apps/mcp` | MCP adapter over stdio and streamable HTTP |
| [External Sentinel](https://github.com/imdelmare/homelab-console-sentinel) | Independently released availability sentinel |
| `config` | Inventory examples and local-only provider configuration |
| `docs` | Canonical architecture, security, and operating documentation |
| `deploy` | systemd, Caddy, and fallback container definitions |

## Read the system before deploying it

1. Start with the [Product Tour](./product-tour.md) and the operator workflow.
2. Continue with [Architecture](./architecture.md) and the five-plane model.
3. Review the non-negotiable boundaries in [Security](./security.md).
4. Understand identities and sessions in [Authentication](./authentication.md).
5. Learn how external agents connect through the [MCP adapter](./mcp.md).
6. Review the normalized integration contracts in [Providers](./providers.md).

## Local validation

The API and MCP adapter support Python 3.12 and 3.13 and require PostgreSQL.
Python 3.14 is not supported by the current FastAPI/Starlette compatibility
window. The web application uses Node.js and npm.

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
