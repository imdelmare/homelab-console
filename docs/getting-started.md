# Getting Started

Homelab Console runs close to the infrastructure it manages and can use a small
model on the same host or private LAN for bounded reasoning. PostgreSQL stores
canonical state; provider adapters reach declared systems through narrow, typed
tools. The model receives neither provider credentials nor arbitrary network
access.

The supported Community Compose stack starts PostgreSQL, API, web, and MCP. It
stays healthy with every AI runtime disabled, so you can establish the control
plane and connect infrastructure before choosing a model. Ollama and the
inventory-bound OpenAI-compatible LAN adapter are the current local inference
paths; cloud inference is optional.

## See the workflow first

Start with the [Product Tour](./product-tour.md) to follow one synthetic incident
from the control room to task evidence, operator approval, and audit. It is the
fastest way to understand what the system does before reading implementation or
deployment details.

The first useful local-first path in a configured console is:

```text
Open Overview
  → inspect one provider observation
  → run a read-only summary tool
  → review the resulting task evidence
  → ask a bounded, read-oriented question through a local model
  → if needed, request an exact write through a governed tool client
  → let the operator approve or deny that invocation
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

## Choose the shortest path

1. Start with the [Product Tour](./product-tour.md) and the operator workflow.
2. Read [Conversation Service](./conversation.md) for the current Ollama and
   private OpenAI-compatible model contracts and their bounded tool scope.
3. Connect one integration using the normalized contracts in
   [Providers](./providers.md).
4. Review how exact writes stop at the human boundary in
   [Security](./security.md).
5. Use the [MCP adapter](./mcp.md) only when you want an external compatible
   agent to share the same governed tool surface.
6. Continue with [Architecture](./architecture.md) when you need the complete
   five-plane model and execution internals.

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
