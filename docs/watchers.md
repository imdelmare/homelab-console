# Watchers

Watchers are the first automatic detection layer for Homelab Console.

They are intentionally narrow:

- they only call read-only tools through `execute_tool`;
- they do not run Claude or Codex;
- they do not execute fixes;
- they create tasks only through the Task Service;
- they deduplicate open incidents before creating another task.
- they keep task/incident history; watcher-created tasks are never deleted
  automatically.

## Current Watchers

`lab.alerts`

- runs `lab.alerts.recent`;
- owns only findings that do not have a dedicated watcher;
- excludes UPS, Uptime Kuma and Cloudflare Tunnel findings, plus OPNsense
  gateway/WireGuard findings, so one condition has one incident owner;
- reads warning/critical findings;
- creates one task per new incident;
- updates the existing open incident on repeated sightings.
- keeps an operator-handled incident quiet on repeated sightings.
- resolves open incidents only after their alert is missing for the configured
  grace period.

The dedupe key is derived from the first stable finding field available:
`dedupe_key`, `fingerprint`, `id`, or `code`.

If the finding has no stable key, the fallback key is derived from:

```text
watcher_id + provider_id + normalized message
```

The fallback normalizes numeric counters so aggregate findings such as
`193 unavailable entities` and `197 unavailable entities` update the same
incident instead of creating a fresh task every run.

`network.gateway`

- runs `opnsense.gateways.status`;
- maps only gateways declared in `providers.opnsense.gateway_observations`;
- applies typed topology availability groups: in an `any` dual-WAN group one
  failed uplink is warning, while loss of every uplink is one critical group
  incident;
- detects packet loss, high latency and high jitter as degraded gateway
  incidents;
- supports per-uplink thresholds and `performance_monitoring: false` for a
  standby cellular gateway whose idle-path latency is not actionable;
- does not emit a second performance incident for an offline gateway;
- uses stable dedupe keys per gateway and failure type;
- includes topology observation ids in incident payloads for exact UI
  attribution.

`network.wireguard`

- runs `opnsense.wireguard.status`;
- detects the case where no WireGuard peer is connected as critical;
- detects stale peers as warning incidents;
- uses stable dedupe keys per WireGuard failure type.

`network.zerotier`

- runs `zerotier.members.list` against declared Central Legacy networks;
- ignores stale laptops and phones unless they are explicitly declared in
  `required_online_member_ids`;
- creates a warning when only part of the required set is available and a
  critical incident when no required member is online.

`cloudflare.tunnel`

- runs the official control-plane `cloudflare.summary` tool;
- owns tunnel availability, connector availability and reconnect findings;
- emits at most one authoritative incident per run, ordered by impact;
- does not infer public application availability, which remains owned by
  Uptime Kuma.

`uptimekuma.monitors`

- runs `uptimekuma.monitors.status`;
- ignores monitors that are up or in maintenance;
- creates critical incidents for down monitors;
- creates warning incidents for other non-healthy monitor states.

`power.ups`

- runs `nutups.status`;
- creates critical incidents when the UPS is on battery or low battery;
- creates warning incidents for replace-battery/alarm state, low charge or
  runtime below 10 minutes;
- uses stable dedupe keys per UPS and failure type;
- resolves and auto-completes clean unclaimed watcher tasks when the UPS
  returns to online state.

`thermal.sensors`

- runs only read-only temperature tools through the execution core:
  `nutups.status`, `opnsense.system.temperature`, `mikrotik.system.health`,
  `fritzbox.primary.temperature`, `fritzbox.secondary.temperature`,
  `proxmox.disks.temperatures`, `hosts.temperatures` and `frigate.stats`;
- excludes room/ambient sensors and Home Assistant free-form discovery;
- ignores FritzBox devices that report `supported=false`;
- stores every canonical reading in the watcher run payload for later tuning;
- creates incidents only when a reading crosses its category threshold;
- uses stable dedupe keys per normalized sensor id.

Initial thresholds are intentionally conservative:

```text
ups: warning 38 C, critical 45 C
opnsense: warning 75 C, critical 85 C
mikrotik: warning 70 C, critical 80 C
fritzbox: warning 80 C, critical 90 C
disk: warning 55 C, critical 65 C
edge hosts (pizero/qdevice): warning 70 C, critical 80 C
compute hosts: warning 80 C, critical 90 C
frigate detectors: warning 75 C, critical 85 C
```

`backup.freshness`

- runs `proxmox.backups.list` and `pbs.backup.jobs.health`;
- tolerates one missing source (for example no PBS): the run only errors when
  no backup source is readable, and per-tool errors stay in the run payload;
- flags a guest whose latest vzdump backup is older than
  `providers.proxmox.backup_max_age_days` (default 3): warning above the
  threshold, critical above twice the threshold;
- flags a PBS backup group whose latest snapshot is older than
  `providers.pbs.backup_group_max_age_days` with the same warning/critical
  split;
- flags a guest listed in `required_backup_vmids` that has no visible backup;
- ignores guests in `backup_ignore_vmids` and groups in
  `backup_ignore_groups` (`type/id` or `store:type/id`);
- a guest that was never backed up and is not required stays silent: this
  watcher detects staleness, not backup policy;
- uses stable dedupe keys per guest/group.

PBS verification health is also included in `pbs.summary`, which feeds
`lab.alerts`. A PBS installation with no verify jobs now produces the explicit
`verify_jobs_missing` warning instead of appearing silently healthy. Verification
does not replace a restore test; follow `docs/BACKUP_RESTORE_DRILL.md` for the
operator-controlled restore drill.

This watcher exists because a backup that silently stops running produces no
failed task and therefore no `lab.alerts` finding. Staleness is the only
signal that catches "the job never started".

`security.certificates`

- runs `network.tls.certificates` against the TLS targets declared in
  `tls.certificate_targets` (inventory only — callers cannot supply hosts);
- warning when a certificate expires within `warning_days` (default 21),
  critical within `critical_days` (default 7) or already expired;
- unreachable targets do not create incidents: availability stays owned by
  Uptime Kuma, and the probe result remains visible in the run payload;
- uses stable dedupe keys per target id.

`storage.disks`

- runs `proxmox.disks.temperatures` (which also reports SMART health and
  wearout);
- creates a critical incident when a disk reports a SMART health value other
  than passed/ok/unknown;
- stores every disk's health and wearout reading in the watcher run payload
  so wearout trends can be reviewed before any wearout threshold exists;
- temperature thresholds remain owned by `thermal.sensors`;
- uses stable dedupe keys per node and device path.

`network.presence`

- runs `opnsense.devices.arp` and `opnsense.kea.leases`;
- treats the first run as a baseline and does not create incidents from it;
- detects new devices, duplicate hostnames and ARP-only devices on later runs.

Presence detection is intentionally noisy compared to gateway and monitor
health checks. Keep it manual unless the operator explicitly wants network
inventory drift to create tasks.

## OPNsense Direction

The current OPNsense watcher flow is outbound polling from Homelab Console to
OPNsense through narrowly scoped read-only tools. OPNsense does not call the
API directly for watcher events.

The API currently exposes only these webhook-style routes:

- Telegram webhook: `POST /api/telegram/webhook`;
- authenticated operator watcher routes under `/api/watchers/*`.

If OPNsense needs to push events to Homelab Console later, add a dedicated
typed endpoint with its own authentication and payload schema. Do not route it
through a generic HTTP forwarder.

## API

Manual run:

```http
POST /api/watchers/run
```

List open incidents:

```http
GET /api/watchers/incidents
```

List recent watcher runs:

```http
GET /api/watchers/runs
```

Read watcher scheduler/config state:

```http
GET /api/watchers/status
```

The response includes global scheduler state plus one row per watcher:
`enabled`, `interval_seconds`, `min_severity`, `investigation_mode`, `last_run`,
`next_run_at`, `last_error`, and `runbook_incident_type`.

Update the global scheduler state:

```http
POST /api/watchers/automation
{"enabled": false}
```

The global value is persisted in `watcher_automation_state`, is shared by all
API workers and survives restarts. `WATCHERS_ENABLED` is only the bootstrap
default until the first persisted toggle is written.

Update one watcher at runtime:

```http
PATCH /api/watchers/config/{watcher_id}
{"enabled": true, "interval_seconds": 300, "min_severity": "warning", "investigation_mode": "manual"}
```

This state is persisted in the `watcher_configs` table. Missing rows fall back
to the defaults from this document, so existing installs keep working after the
migration.

Resolve an open incident that the operator confirms was already handled:

```http
POST /api/watchers/incidents/{incident_id}/resolve-handled
```

This marks the incident `resolved` with
`resolution_reason="operator_already_handled"`, appends a watcher-specific
event/summary to the linked task, skips pending checks and completes the
watcher-created task if it is not already final. If the same finding is seen
again, the watcher updates the handled incident without creating another task.
It preserves the audit trail instead of deleting stale watcher output.

All endpoints require operator auth. The run endpoint also requires CSRF.

## Scheduler

Automatic scheduling is enabled by default and runs every 5 minutes.

```ini
WATCHERS_ENABLED=true
WATCHERS_INTERVAL_SECONDS=300
WATCHERS_MIN_SEVERITY=warning
WATCHERS_IGNORE_PATTERNS=
WATCHERS_RESOLVE_AFTER_MISSING_RUNS=3
```

When enabled, the API process starts a background scheduler after startup. The
first run is delayed briefly, then repeats every `WATCHERS_INTERVAL_SECONDS`
seconds, with a minimum interval of 60 seconds.

Watcher tasks are always created and audited. Local urgent critical incidents
enter the notification outbox immediately; infrastructure critical incidents
use a short debounce and cross-watcher aggregation based on the dependency
graph. Warning incidents retain the longer debounce. Pending notifications are
cancelled when their incidents clear, and cooldown and delivery history remain
persistent. See [notifications.md](notifications.md).

## Already-handled matching

Before opening a new watcher task, the API compares the incident with recently
resolved incidents. Exact dedupe-key matches are handled deterministically.
Plausible semantic matches are narrowed to at most five candidates and sent to
the configured task-router model for structured classification. High-confidence
warning matches are linked to the previous task, marked
`operator_already_handled`, flagged with `watcher.incident.auto_matched`, and do
not create a new task or Telegram notification. Critical incidents are never
auto-handled: they are flagged as `watcher.incident.possible_match` for operator
review. Model calls are capped per hour and every decision records model, token
usage, confidence, reason, and the matched incident in the audit trail.

```ini
INCIDENT_MATCHER_ENABLED=true
INCIDENT_MATCHER_MAX_CANDIDATES=5
INCIDENT_MATCHER_MAX_CALLS_PER_HOUR=10
INCIDENT_MATCHER_AUTO_HANDLE_CONFIDENCE=0.9
```

The default enabled watcher set is currently:

- `cloudflare.tunnel`;
- `lab.alerts`;
- `network.gateway`;
- `network.wireguard`;
- `network.zerotier`;
- `power.ups`;
- `uptimekuma.monitors`.

Available but not scheduled by default:

- `backup.freshness`;
- `network.presence`;
- `security.certificates`;
- `storage.disks`;
- `thermal.sensors`.

Keep `network.presence` manual unless inventory drift should create tasks
automatically. Keep `thermal.sensors` manual until the operator has reviewed a
day or two of real readings and confirmed the canonical sensor list.
Thermal provider failures are recorded in the run payload and never clear that
provider's existing incidents; a run with no readable thermal provider fails
closed.
Enable `backup.freshness` after aligning `backup_max_age_days` /
`backup_group_max_age_days` with the lab's real backup schedule, so the first
scheduled run does not flag guests that are simply on a weekly cadence.
Enable `security.certificates` after declaring `tls.certificate_targets`
(with no targets the watcher is a no-op). `storage.disks` has an unambiguous
signal (SMART failure) and can be enabled as soon as one manual run confirms
the disk list looks right.
Provider-specific noise is configured structurally in inventory: known stopped
Proxmox VMIDs, Home Assistant count thresholds, required ZeroTier members and
per-uplink gateway thresholds remain visible as metrics while only actionable
conditions become findings.

`WATCHERS_MIN_SEVERITY=critical` creates new incidents only for critical
findings. An already-open warning that is still observed remains open and is
marked `policy_state=filtered`; it is not falsely resolved as
`alert_cleared`. `WATCHERS_IGNORE_PATTERNS` is a comma-separated
case-insensitive substring list matched against provider id, severity and
finding text, with the same observed-versus-filtered behavior.

Each watcher can override `enabled`, `interval_seconds`, `min_severity`, and
`investigation_mode` through the UI/API. The default mode is `manual`.
`auto_investigate` is a read-only dispatch gate, not remediation: it accepts
only new deterministic warning incidents with a known runbook and a Task Router
decision for the OpenCode-powered `fixer` agent with
confidence of at least 0.80 and no operator dependency. Only one worker task may
be active; rejected or unreachable delivery releases the task back to the
manual queue through the canonical state machine.
Manual runs with explicit watcher ids still run that watcher on demand;
scheduled/background runs use the enabled set and each watcher's own interval.

## Test Flow

1. For deterministic manual testing, disable automation through
   `POST /api/watchers/automation` (or set `WATCHERS_ENABLED=false` before the
   first persisted toggle exists).
2. Start API and web normally.
3. Log in to the web UI.
4. Trigger `POST /api/watchers/run`.
5. Check `GET /api/watchers/incidents`.
6. Check `/api/tasks` for any created watcher tasks.
7. In the web Watchers app, switch between Open/Resolved/All and use the task
   button on an incident to open the linked task.

The created tasks have `source="watcher"` and `created_by="service:watcher"`.
When `TASK_ROUTER_ENABLED=true`, a new watcher task queues asynchronous routing
and is later enriched with a redacted `task.router_decision` event. That event may suggest category,
priority, owner, labels, dedupe/runbook hints and first read-only steps, but it
does not claim the task or execute remediation.
Incidents whose alert disappears are not resolved immediately. Each successful
watcher run that does not see the alert increments `missing_runs` and sets
`last_missing_at`; the incident is marked `resolved` with
`resolution_reason="alert_cleared"` after
`WATCHERS_RESOLVE_AFTER_MISSING_RUNS` missed runs. The default is `3`, so one
clean sample does not close an incident/task during a brief flap. If the alert
returns before that threshold, the existing incident/task is reused and the
clearing counter is reset.

When an alert clears, the watcher may also auto-complete the linked task, but
only when it is still a clean watcher task:

- `source="watcher"`;
- `created_by="service:watcher"`;
- status is still `open`;
- no assigned agent and no claim timestamp;
- no other open incident is linked to the same task.

Pending task checks are skipped with a watcher-specific reason, the summary is
updated with the auto-resolution note, a `watcher.task.auto_completed` event is
appended, and a `TaskResolution` snapshot is kept. The task remains technically
unassigned, but task API responses expose `resolution_label="auto_closed"` and
`auto_closed=true` so the UI can label it as `Auto closed` instead of ordinary
`Unassigned`. Claimed or manually worked tasks are left open for the assigned
agent/operator.

Incident rows expose `dedupe_note`, `dedupe_basis`, `auto_close_note`, and
`runbook_incident_type` so the UI can show whether a run created a new task,
updated an existing task, linked to a root incident, or is waiting through the
auto-close grace period.

Open incidents are protected by a partial unique index on `dedupe_key` where
`status='open'`. The service also uses a PostgreSQL advisory transaction lock per
watcher id to avoid duplicate scheduler work across workers; the unique index is
the final data-level guard.

## Out Of Scope

- no automatic remediation;
- no shell/SSH/HTTP generic execution;
- no Agent Runner;
- no incident escalation engine;
- no multi-agent dispatch.
