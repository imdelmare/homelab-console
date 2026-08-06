# External Sentinel

External Sentinel is the VPS-side external watcher, separate from Homelab
Console. It covers the failure mode where the console, WireGuard path, or home
network is unreachable and the normal watcher stack cannot report its own
outage.

Treat it as a dead-man switch, not a second control plane. It observes from the
outside, deduplicates locally, and sends Telegram alerts directly from the VPS.
Its automatic Telegram alerts and recoveries use US English; historical or
user-provided content is not translated.

## Milestone 1

Implemented in `apps/sentinel`:

- fixed-config HTTP health checks;
- heartbeat receiver at `POST /heartbeat/{source_id}`;
- heartbeat timeout detection;
- local SQLite incident deduplication;
- configurable consecutive-failure confirmation (three HTTP failures by
  default; heartbeat timeout remains one already-debounced failure);
- two consecutive healthy observations before recovery;
- persistent cross-source aggregation and cooldown in local SQLite;
- one Telegram alert/recovery per availability group.

The Sentinel does not call Homelab Console APIs, execute shell commands, forward
raw HTTP requests, or remediate anything. URLs are read from local config at
startup; runtime callers cannot provide arbitrary targets.

## Configuration

Copy `config/sentinel.example.json` to a local, gitignored path such as
`config/sentinel.local.json`, then set:

```bash
export SENTINEL_CONFIG_PATH=config/sentinel.local.json
export SENTINEL_HEARTBEAT_TOKEN='long-random-token'
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_ALLOWED_CHAT_ID='...'
```

Environment variables override the token, bind address, port, state path and
Telegram credentials. Keep real values out of git.

Heartbeat clients call:

```http
POST /heartbeat/home
Authorization: Bearer <SENTINEL_HEARTBEAT_TOKEN>
Content-Type: application/json

{"status":"ok"}
```

`GET /health` only reports the Sentinel process liveness.

The default policy checks every 30 seconds, confirms HTTP failures after three
samples, waits 120 seconds to aggregate correlated HTTP and heartbeat evidence,
requires two healthy samples for recovery, and suppresses group reopenings for
30 minutes. Targets that represent the same availability boundary should share
the same `notification_group`.

## Deployment Shape

Use two deployments:

```text
VPS
  External Sentinel
  Telegram direct alerting
  SQLite sentinel state

Proxmox cluster / home side
  Homelab Console API/web/db
  internal watchers/providers/MCP
  outbound heartbeat sender to the VPS
```

Run Sentinel under systemd or Docker on the VPS. Bind the heartbeat listener to
the VPS WireGuard address when the home side can reach it privately. Do not
expose a public Sentinel hostname unless there is a specific reason to cross the
public internet. If exposed, keep the bearer token mandatory and move signed
heartbeats into Milestone 3 before relying on it across an untrusted path.

For a VPS-only Docker deployment with the compose plugin, use:

```bash
docker compose -f deploy/docker-compose.sentinel.yml --env-file .env.sentinel up -d --build
```

`deploy/env.sentinel.example` is the environment template. Keep the filled
`.env.sentinel` on the VPS only.

The current lab deployment is a direct Docker deployment on the VPS. Preserve
the mounted `config/` and `data/` directories when replacing the container.

On the cluster side, use the one-shot heartbeat sender rather than adding a
runtime dependency from Sentinel back into the console:

```bash
PYTHONPATH=apps/sentinel \
SENTINEL_HEARTBEAT_URL=http://192.0.2.10:8766/heartbeat/home \
SENTINEL_HEARTBEAT_TOKEN='long-random-token' \
python -m sentinel.heartbeat_client
```

The systemd templates in `deploy/systemd/` run the same sender every 60 seconds.
Install them on the Proxmox-side host or container that should represent
console/home availability, with `/opt/homelab-console` pointing at this checkout
and `/etc/homelab-console/sentinel-heartbeat.env` based on the example file.

Before relying on alerts, execute the failure drills in
operator Sentinel runbook.

## Later Milestones

Milestone 2 should add a narrow Homelab Console integration: an External
Sentinel page, typed import of Sentinel incidents, WireGuard status from the
existing VPS provider, and last-heartbeat display.

Milestone 3 should add signed heartbeats, richer Telegram retry/backoff,
retention, metrics and real failure drills.
