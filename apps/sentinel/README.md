# Homelab Sentinel

Standalone external watcher for the VPS side. It is intentionally separate from
`apps/api`: if Homelab Console, WireGuard, or the home network fails, this
process can still emit Telegram alerts from outside the homelab.

Keep Sentinel small:

- fixed HTTP checks configured at startup;
- one heartbeat receiver at `POST /heartbeat/{source_id}`;
- local SQLite incident dedupe;
- Telegram alert/recovery notifications;
- no provider credentials and no remediation actions.

Run locally:

```bash
python -m venv .venv
source .venv/bin/activate
python -m sentinel
```

Set `SENTINEL_CONFIG_PATH=config/sentinel.local.json` and keep that file out of
git. See `config/sentinel.example.json` for the schema.

Send one heartbeat from the cluster side:

```bash
PYTHONPATH=apps/sentinel \
SENTINEL_HEARTBEAT_URL=http://192.0.2.1:8766/heartbeat/home \
SENTINEL_HEARTBEAT_TOKEN=... \
python -m sentinel.heartbeat_client
```
