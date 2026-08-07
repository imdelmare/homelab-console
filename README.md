# Homelab Console

Homelab Console is an AI-native, approval-driven control plane for private
homelab infrastructure. REST, Telegram, watchers, and authenticated MCP clients
share one typed execution core with validation, policy, redaction, and audit.

It deliberately provides **no arbitrary shell, SSH, URL, or raw provider API**.
Infrastructure writes are narrow capabilities and require a fresh, input-bound
operator approval.

## Components

- `apps/api` — FastAPI control plane, providers, tasks, approvals, and audit.
- `apps/web` — React operator desktop with a Windows 98-inspired interface.
- `apps/mcp` — authenticated stdio and streamable HTTP MCP adapter.
- `apps/sentinel` — independent external availability observer.
- `apps/docs` — VitePress field manual and landing-page assembly.

## Development validation

The backend requires Python 3.12+ and PostgreSQL; the frontend uses Node.js 22.

```bash
cp .env.example .env
cp config/secrets.local.example.yml config/secrets.local.yml

cd apps/api
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cd ../..

TEST_DATABASE_URL=postgresql+psycopg://example:example@localhost:5432/postgres   ./scripts/run-test-suite.sh all
```

Start from `config/homelab.example.yml` and add only targets you explicitly
control. Never commit a populated `.env`, credentials, or local inventory.

## Documentation

- [Field manual](https://imdelmare.github.io/homelab-mcp/docs/)
- [Architecture](docs/architecture.md)
- [Security model](docs/security.md)
- [MCP adapter](docs/mcp.md)
- [Provider contracts](docs/providers.md)

The public source is a sanitized export of a private operational repository.
Live inventory, activation records, deployment drills, and operator runbooks are
intentionally excluded.

## License

Licensed under the [Apache License 2.0](LICENSE). Third-party attribution and
license notes are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
