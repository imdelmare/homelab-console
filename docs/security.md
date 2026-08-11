# Security

## Threat model

Homelab Console currently runs on a **home application host inside the private home
network**, behind Caddy. A separate VPS runs External Sentinel and receives
outbound heartbeats over a narrowly routed WireGuard peer. That framing drives
most of the design:

- The API is internet-reachable (behind a reverse proxy), so it must assume
  hostile, unauthenticated traffic on every public endpoint at all times.
- A compromise of the home application host application host would occur inside the home
  network, so provider reach is constrained to typed adapters, credentials
  remain outside model surfaces, and the tool plane exposes no generic
  lateral-movement capability.
- Providers are reached directly over the trusted LAN through typed adapters.
  The WireGuard peer is reserved for the inventory-bound API-lifecycle Sentinel
  heartbeat and deployment
  traffic and does not route the home LAN. The live catalog is read-only; any
  future write capability must be individually approved and never becomes a
  general tunnel, SSH gateway, or arbitrary-URL proxy.
- The operator's Telegram account is a security-relevant identity (second
  factor by default, optional sole login factor, approvals and model
  switching), so the bot validates by numeric user ID and chat ID, never by
  username, and every callback carries a short-lived single-use nonce.
- AI/model surfaces are treated as semi-trusted: MCP clients such as Claude,
  Codex and Cline can request tool calls, and the Conversation Service can
  request a narrow summary/task allowlist. Both paths are assumed to
  potentially be manipulated (prompt
  injection, confused deputy). They never hold credentials and every
  infrastructure tool call still passes through authn, risk policy, approval,
  redaction, and audit.

## Trust boundaries

See the ASCII diagram in [`architecture.md`](architecture.md#trust-boundaries).
In short: Internet → Cloudflare Tunnel/reverse proxy → loopback API → typed
provider adapters over the trusted LAN. A separate outbound WireGuard path
carries the fixed ADR 0023 heartbeat from the API lifecycle to the VPS. The API,
database, MCP origin and
provider APIs are never directly exposed. Under ADR 0017, the dedicated MCP
hostname reaches the LAN MCP origin only through Cloudflare Tunnel and still
requires a per-client bearer token on every request.

## Public endpoints

Only these endpoints are reachable without an authenticated session:

- `GET /health`
- `GET /api/auth/config`
- `POST /api/auth/login`
- `GET /api/auth/challenge/{id}`
- `POST /api/auth/complete`
- `POST /api/auth/verify-otp`
- `POST /api/auth/recovery` (when recovery is enabled)
- `POST /api/telegram/webhook` (protected by `TELEGRAM_WEBHOOK_SECRET`)
- `POST /api/mcp/pairing/start` (rate-limited; starts Telegram approval)
- `POST /api/mcp/pairing/consume` (requires the one-time pairing secret)

Every other API endpoint requires a valid server-side session cookie. MCP HTTP
itself is exposed on the configured MCP endpoint and requires a per-client
bearer token issued through Telegram pairing.

`AUTH_LOGIN_MODE=password` remains the default. The operator-approved
`telegram_only` mode treats the submitted username as account identification,
not a secret, and makes Telegram approval/OTP the sole factor. It preserves
per-IP, per-username and per-account challenge limits, rejects password fields,
and disables password recovery codes. This mode increases the impact of a
Telegram account, bot-token, webhook-secret or allowed-ID compromise and must
be rolled back to `password` if that identity boundary is in doubt.

MCP HTTP access is client-scoped: each approved client gets its own token,
agent id (`claude`, `fixer`, `codex`, `cline`, `opencode`, or `worker`), label,
fingerprint, dashboard row,
revocation state, and `last_seen_at`. Individual MCP tool calls cannot choose
or spoof the agent identity; it is derived from the bearer token record.
The public MCP hostname does not use Cloudflare Access: possession of an
operator-approved MCP bearer token is the application authentication boundary.
Pairing remains Telegram-approved, tokens are stored only as hashes server-side,
and revocation takes effect on the next request.

## Secret handling and redaction

- Provider credentials (e.g. Proxmox API token) live only in
  `SECRETS_PATH` (default `config/secrets.local.yml`), which is gitignored
  and never committed. The repo only ships an example file documenting the
  expected shape, with empty/placeholder values.
- Provider endpoints, hostnames, ports, timeouts and TLS policy live in
  `HOMELAB_CONFIG_PATH` (use a gitignored `config/homelab.local.yml` for
  real local values). Do not put `base_url`, `host`, `port`, or `verify_tls`
  in the secrets file.
- TLS verification remains enabled by default. The explicit
  `ALLOW_INSECURE_LOCAL_TLS=true` exception permits `verify_tls: false` only
  for private-IP provider endpoints on a trusted LAN/VPN, logs a warning, and
  never permits the exception for a public endpoint.
- Secrets are read by provider adapters; they are not stored in the
  database, not put in task/finding/artifact data, and not passed to model
  providers.
- Task titles, goals, summaries, notes, findings and checks are application
  state. They are length-limited, schema-validated and redacted before event or
  audit persistence; they are not a place for credentials.
- The execution core applies **centralized redaction** to every tool
  response and every audit event before it is persisted or returned. Keys
  matching (case-insensitively) `password`, `secret`, `token`, `api_key`,
  `authorization`, `cookie`, `session`, `otp`, `csrf`, `credential` are
  redacted, plus provider-specific rules for known credential shapes.
- Redaction happens before the audit log write and before the response
  leaves `execute_tool`, so neither the DB, the optional JSONL audit sink,
  nor any caller (REST, MCP, or a model) ever sees an unredacted secret
  through the tool plane.

## What AI models never see

- Provider credentials, API tokens, or the contents of `SECRETS_PATH`.
- Raw local provider endpoint configuration from `HOMELAB_CONFIG_PATH`.
- Session cookies, CSRF tokens, or other auth material.
- Raw, unredacted tool output — models see the same redacted output the
  execution core returns to any other caller.
- Anything outside the declared tool schemas: models cannot request
  arbitrary shell commands, arbitrary HTTP calls, or arbitrary SSH sessions,
  because no such tool exists.
- Conversation history is bounded and channel-neutral. The Conversation Service
  receives only compact recent messages, allowed-tool metadata, an optional
  current task, and redacted summary results; it does not receive full audit
  logs, provider config files, or unbounded transcripts.
- The optional Task Router receives only compact redacted task context and
  stores an advisory `task.router_decision` event. Its durable queue contains
  only that redacted context. The direct OpenCode Go client has a fixed HTTPS
  origin, fixed reviewed model mapping, no redirects and no MCP identity. It
  cannot call tools, claim tasks, set `assigned_agent`, change task status, or
  resolve incidents. Its API key stays in the API runtime environment and is
  never stored in task events, usage rows or audit metadata.
- The LAN AI manager receives only bounded redacted decision context. Its
  address comes from a private literal-IP inventory host, its port must be
  declared for that host, its path is fixed, and redirects are disabled. It has
  no MCP credentials or direct infrastructure capability; output remains
  untrusted until backend schema validation succeeds.

## SSRF policy

There is no tool or code path that performs a health check or request
against an arbitrary, caller-supplied URL. All provider/host reachability
checks resolve their target from an **inventory ID** (a known host/service
already present in `HOMELAB_CONFIG_PATH`), not from free-form input, and are
bounded to expected ports and short timeouts. This closes the classic
"paste a URL, have the server fetch it" SSRF vector by construction — the
input space for a target is a fixed, pre-configured ID list, not a string.
The AI manager follows the same rule and additionally rejects public IPs,
hostnames, and ports absent from the selected inventory host.

## Risk levels and tool policy

| Risk | Policy this milestone |
|---|---|
| `low` (read) | Any authenticated user |
| `medium` | Requires an explicit policy grant |
| `high` | Requires approval; **disabled by default** |
| `critical` | Not implemented |
| infrastructure `write` tool | Requires exact operator allowlisting, an ADR and confirmation |
| task-state write | Allowed only through typed task service operations, subject to [task ownership](#task-ownership) |

The system now has a deliberately small infrastructure write surface:
only exact tools present in `APPROVED_WRITE_TOOLS` can be enabled, and every
invocation consumes a single-use, input-bound operator approval. Task-state writes
(`tasks.claim`, `tasks.add_finding`, `tasks.complete`, etc.) modify only Homelab
Console's own database and create audit/task events. Enabling an infrastructure
write tool is a deliberate, reviewed step backed by an exact machine-readable
allowlist entry and ADR — not a configuration toggle an operator or model can
flip at runtime. The current machine-readable allowlist is authoritative; new
Proxmox LXC, OPNsense WoL and OPNsense gateway-transition tools were activated
under ADRs 0006, 0008 and 0010 after their scoped live approval drills. The
unsuccessful egress-switch drill recorded by ADR 0011 ended with rollback and
removal of that tool from governance.

High-risk approvals are single-use at the database boundary: the execution
core consumes an `approved` row with one conditional update, and
`tool_invocations.approval_id` is unique. Concurrent calls using the same
approval therefore cannot both reach the provider. Infrastructure write tools
remain disabled unless separately approved despite this defence.

## Task ownership

Once a task is claimed, `app.services.tasks_service` enforces that only the
claiming agent (`agent:claude`, `agent:fixer`, `agent:codex`, `agent:cline`,
`agent:opencode`, or a per-client `agent:worker:<client-id>` principal) can
mutate it further
(`not_task_owner` otherwise); a human operator acting through the
authenticated web UI or Telegram always bypasses this (administrative
override), and an agent may not touch a task that hasn't been claimed
yet either.

This is a coordination and audit-hygiene control backed by the MCP client
registry: for HTTP clients, the agent identity comes from the approved
per-client token record. Revoke a client token from the MCP dashboard when a
machine, tool, or local config should no longer access the control plane.

The optional legacy OpenCode Fixer uses the separate `agent:fixer` identity. Before
the API contacts its loopback supervisor, it records and verifies an exact
`task.fixer_dispatch_requested` event bound to the task, actor, owner and
dispatchable state. The static dispatch secret authenticates process launch;
the dedicated MCP token, task ownership checks and execution core independently
control what that process can read or change. Legacy `claude-fixer` clients are
revoked during deployment rather than accepted through a compatibility alias.
ADR 0024 replaces this with per-client remediation worker principals,
operator-granted capability and durable lease fencing. Core enforcement binds
task mutation, approval request/consumption and task-bound infrastructure calls
to the authenticated client, worker job and current lease generation. A
core-owned recovery loop fences expired workers and releases exhausted jobs.
The legacy boundary remains available only for rollback until an external pull
adapter passes conformance, and must never process the same task concurrently.
Granting `task-worker.v1` permanently converts that MCP registration to a unique
worker principal. Removing the capability does not downgrade its bearer token
back to the shared interactive agent family. Revocation closes active jobs and
releases non-final tasks; registrations with worker history remain audit
tombstones and cannot be forgotten.

All status changes, including claim/release and guarded watcher
auto-completion, pass through one transition helper and append the canonical
`task.status_changed` event. Final tasks (`completed`/`cancelled`) reject
summary, note, finding, check and task-linked tool mutations with
`task_not_active`; reopening is the only supported way to make them mutable
again. The operator override bypasses ownership, not final-state immutability.

## Non-goals (explicit)

- **Not a general remote-access tool.** No arbitrary shell, SSH, or HTTP
  proxying into the homelab, now or planned.
- **Not multi-tenant.** Designed for a single operator/household, not a
  shared or public service.
- **SMS is explicitly deferred** as a second factor; Telegram
  approval/OTP is the only supported second factor today, with recovery
  codes as the offline fallback.
- **Not a secrets manager.** `SECRETS_PATH` is a local file read by the
  app; there is no vault integration in this milestone.
- **Not defended against a fully compromised VPS host.** WireGuard, disk
  encryption, and OS hardening at the host level are out of scope for the
  application itself and assumed to be handled operationally (see
  operator deployment guide).
- **The `/models` endpoints are operator-scoped only, decoupled from
  tasks.** `assigned_agent` (set exclusively by `tasks.claim`) is the only
  field that determines who owns a task; the "active model" concept plays no
  role in task ownership or the task lifecycle.
- **Conversation is not an agent runner.** It does not start MCP clients,
  does not run autonomous loops, and cannot expand its own tool catalog. It can
  answer operator questions, call narrow summary/task tools, and propose or
  create tasks under the rules in [`conversation.md`](conversation.md).
- **Task Router is not an owner or dispatcher.** It can classify newly created
  tasks and suggest an owner/runbook in a timeline event, but only
  `tasks.claim` changes `assigned_agent`, and only typed task-service
  operations mutate task state.
