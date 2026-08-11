# External Sentinel

External Sentinel is the VPS-side external watcher, separate from Homelab
Console. It covers the failure mode where the console, WireGuard path, or home
network is unreachable and the normal watcher stack cannot report its own
outage.

The independently releasable HTTP, persistence and compatibility boundary is
frozen in [`sentinel-contract-v1.md`](sentinel-contract-v1.md).

Treat it as a dead-man switch, not a second control plane. It observes from the
outside, deduplicates locally, and sends Telegram alerts directly from the VPS.
Its automatic Telegram alerts and recoveries use US English; historical or
user-provided content is not translated.

## Released runtime

The implementation, standalone tests, deployment assets and operator runbook
live in
[`imdelmare/homelab-console-sentinel`](https://github.com/imdelmare/homelab-console-sentinel).
The first independently verified release is
[`v1.0.0`](https://github.com/imdelmare/homelab-console-sentinel/releases/tag/v1.0.0).
It provides:

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

Use the configuration and environment templates shipped with the matching
external release. Environment variables override the token, bind address, port,
state path and Telegram credentials. Keep real values out of both repositories.

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

For a VPS-only Docker deployment, check out an exact release and follow its
installation guide:

```bash
git clone --branch v1.0.0 https://github.com/imdelmare/homelab-console-sentinel.git
cd homelab-console-sentinel
docker compose -f deploy/compose.yaml --env-file .env.sentinel up -d --build
```

Keep the filled `.env.sentinel` on the VPS only. Preserve the mounted `config/`
and `data/` directories when replacing or rolling back the container.

The current lab deployment is a direct Docker deployment on the VPS. Preserve
the mounted `config/` and `data/` directories when replacing the container.

On the home side, ADR 0023 allows the FastAPI lifecycle to send the heartbeat
every 60 seconds. The origin and source id come only from
`providers.vps.sentinel_heartbeat`; the token comes from the untracked
`SENTINEL_HEARTBEAT_TOKEN` environment value. Enable it with
`SENTINEL_HEARTBEAT_ENABLED=true`. It is a fixed background integration, not a
tool or request-driven HTTP proxy.

The external repository's runbook still owns Sentinel health, failure,
persistence and rollback drills. This core repository retains the versioned
contract and the narrow heartbeat client only.

## Later Milestones

Milestone 2 should add a narrow Homelab Console integration: an External
Sentinel page, typed import of Sentinel incidents, WireGuard status from the
existing VPS provider, and last-heartbeat display.

Milestone 3 should add signed heartbeats, richer Telegram retry/backoff,
retention, metrics and real failure drills.
