# External Sentinel contract v1

## Scope

Sentinel is a standalone availability observer. It has no Homelab Console
database, MCP identity, provider credentials or remediation capability. Runtime
targets come only from its startup configuration. The optional home-side client
or API-lifecycle worker sends a heartbeat; the core API never calls Sentinel in
response to a user, tool, model or MCP request.

## HTTP API

### `GET /health`

Returns `200 application/json`:

```json
{"ok":true}
```

This is process liveness only. It does not guarantee Telegram delivery, target
health, SQLite writability or recent heartbeats. Other GET paths return `404`.

### `POST /heartbeat/{source_id}`

Authentication accepts either:

```http
Authorization: Bearer <token>
X-Sentinel-Token: <token>
```

The configured token must be non-empty and comparison is constant-time. Missing
or invalid authentication returns:

```http
401 {"ok":false,"error":"unauthorized"}
```

`source_id` is percent-decoded and trimmed; an empty value returns
`400 {"ok":false,"error":"missing_source_id"}`. The optional JSON object is
bounded to the first 4096 bytes and stored only as heartbeat evidence. For v1,
an empty, malformed or non-object body is treated as `{}`. Receipt updates the
source timestamp and returns `200 {"ok":true}`. Other POST paths return `404`.

The client payload currently contains informational `source`, `sent_at`, `host`
and `platform` fields. Server health decisions depend on authenticated receipt,
not on caller-provided field values.

## Configuration and monitoring semantics

The JSON configuration owns all HTTP targets, heartbeat source ids, expected
status ranges, timeouts, confirmation counts, notification groups, aggregation
and cooldown values. Requests cannot add targets or URLs. Secrets are supplied
only through runtime environment variables:

- `SENTINEL_HEARTBEAT_TOKEN`;
- `TELEGRAM_BOT_TOKEN`;
- `TELEGRAM_ALLOWED_CHAT_ID`.

Environment also selects the config path, SQLite state path and listen
host/port. Port `8766` is the v1 deployment default.

Incidents are deduplicated as `http:{target_id}` and
`heartbeat:{source_id}`. States are `pending`, `open` and `resolved`; recovery
requires the configured healthy observation streak. Notification delivery
failure must not stop monitoring.

## Persistence and deployment

SQLite state is local to Sentinel and must live on a persistent volume. Upgrade
and rollback preserve the database and dedupe keys. Schema changes within v1
must be additive. The container runs non-root, mounts configuration read-only
and uses `GET /health` as its Docker healthcheck.

## Compatibility and conformance

Compatible v1 implementations must pass tests for authentication, body bounds,
heartbeat persistence, restart persistence, failure/recovery confirmation,
group aggregation, notification failure and v1 client/server interoperability.
A breaking endpoint, authentication, dedupe or persistence change requires v2.
