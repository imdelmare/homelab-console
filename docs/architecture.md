# Architecture

## The five planes

Homelab Console is organized around five planes with distinct trust levels.

- **Control plane** — everything about the system's own state: inventory,
  users, sessions, login challenges, provider configuration, tool
  definitions, task state, findings/evidence, model selection, approvals, and
  the audit log. Owns the database.
- **Tool plane** — the only way anything (human or model) can act on
  homelab infrastructure. A fixed set of narrow, explicitly-named tools (e.g.
  `proxmox.nodes.list`), each with its own input/output schema and risk
  level. There is no generic shell, SSH, HTTP, or API forwarding tool — every
  capability is declared, typed, and reviewable.
- **Model plane** — configured model providers power the lightweight
  Conversation Service used by Telegram free text and authenticated REST
  conversation endpoints, plus the optional Task Router enrichment pass.
  Models receive compact, redacted context only and cannot reach providers
  directly. This path is for operator Q&A, task proposals, and task
  classification; it is not an MCP agent runner.
- **Watcher plane** — optional scheduled detection that calls only read-only
  summary tools through `execute_tool`, records incidents, and creates
  deduplicated tasks. It does not run models, execute fixes, or bypass the
  tool plane.
- **Operator plane** — how a human reaches the system: the framework-free
  TypeScript/Vite web console (apps/web), the Telegram bot (status, approvals, model
  switching), and MCP clients (apps/mcp) speaking the same tool surface over
  stdio or HTTP.

## Shared execution core

Both the REST API and the MCP adapter call into the same execution core —
there is exactly one code path that can run a tool, regardless of which
plane initiated the request:

```
execute_tool(tool_id, validated_input, actor, source, task_id, approval_id)
```

Pipeline, in order:

1. **Tool lookup** — resolve `tool_id` against the tool registry.
2. **Enabled check** — reject if the tool is disabled. Unapproved write tools
   are forced disabled by registry governance even if configuration attempts
   to enable them; see current milestone policy.
3. **Input validation** — Pydantic schema validation; extra/unknown fields
   are rejected, not silently dropped.
4. **Authentication** — resolve and verify the calling actor (session or MCP
   per-client token).
5. **Risk-policy check** — evaluate the tool's declared risk level against
   policy (see below).
6. **Approval verification** — for tools that require confirmation, verify a
   matching, unconsumed `Approval` record.
7. **Timeout enforcement** — bound provider calls by the tool's
   `timeout_seconds`.
8. **Provider invocation** — call the underlying provider adapter.
9. **Output normalization** — coerce the provider's response into the tool's
   declared output schema.
10. **Secret redaction** — strip anything matching sensitive-key patterns
    before it leaves this function (see [security.md](security.md)).
11. **Audit log** — write a structured `AuditEvent`.
12. **Task event** — append a `TaskEvent` if the call happened in the
    context of a task.
13. **Response** — return the normalized, redacted result to the caller.

Because REST and MCP both terminate in this same function, there is no
"MCP-only" or "REST-only" capability, and no way to bypass validation,
policy, approval, or redaction by picking one transport over the other.

## Tool definitions

Each tool declares:

| Field | Meaning |
|---|---|
| `id` | Stable dotted identifier, e.g. `proxmox.nodes.list` |
| `name`, `description` | Human-readable metadata |
| `provider_id` | Which provider adapter implements it |
| `category` | Grouping for UI/discovery |
| `mode` | `read` or `write` |
| `risk` | `low`, `medium`, `high`, or `critical` |
| `enabled` | Whether the tool can currently run |
| `timeout_seconds` | Hard timeout enforced by the execution core |
| `requires_confirmation` | Whether an `Approval` is required before running |
| input/output schemas | Pydantic models, extra fields rejected |

Risk policy this milestone:

- **low**-risk read tools run for any authenticated user.
- **medium**-risk tools require an explicit policy grant.
- **high**-risk tools require approval and are **disabled by default**.
- **critical**-risk tools are not implemented.
- Write tools require confirmation and remain disabled unless their exact id
  has operator approval in `app.tools.governance.APPROVED_WRITE_TOOLS` backed
  by an ADR. The current allowlist contains AdGuard protection pause/resume
  under ADR 0004, Proxmox LXC start/shutdown under ADR 0006, OPNsense WoL
  under ADR 0008, and OPNsense gateway failover/restore under ADR 0010.
  The egress switch attempted under ADR 0011 is not allowlisted.

## Entity model

Core persisted entities (owned by the control plane):

- `User`, `Session`, `LoginChallenge` — identity and auth state.
- `ProviderConfiguration` — per-provider connection settings and status.
- `Task`, `TaskEvent` — units of operator/model work and their timeline.
- Task lifecycle — all state changes use the shared transition helper and
  append `task.status_changed`; claim/release and watcher events add context
  but do not replace that canonical event. Final tasks are immutable until
  explicitly reopened.
- `ToolInvocation` — one record per `execute_tool` call.
- `Finding` — structured evidence produced while working a task.
- `Conversation`, `ConversationMessage` — bounded operator conversation
  history, model usage, and estimated cost for Telegram/REST chat. This state
  is not a replacement for tasks.
- Task Router — optional asynchronous model pass backed by a durable one-job-per-task
  queue. It classifies newly created tasks and appends a `task.router_decision`
  event. It does not own, claim, execute, close or mutate tasks beyond that
  auditable timeline event.
- `WatcherRun`, `Incident` — automatic read-only detection history and
  deduplicated issue records linked to watcher-created tasks.
- Task Context Compiler — deterministic read model that compiles a task,
  linked incident, recent evidence, provider state, relevant read-only tools
  and budget into the minimum brief passed to MCP/agent surfaces.
- `Approval` — explicit confirmation records for tools that require them.
- `McpClient`, `McpPairingRequest` — persistent, revocable MCP client
  registrations. Pairing is approved over Telegram; only token hashes are stored
  server-side.
- `AuditEvent` — structured, redacted audit trail.
- `ModelProfile` — configured Conversation Service model providers and the
  currently selected one. MCP task ownership is tracked separately through
  `assigned_agent`.

Task state, findings, evidence, and tool history are all persisted in the
database and survive a model-provider switch; only the reasoning engine
changes.

## Conversation Service

Telegram free text and the authenticated `/api/conversations/*` endpoints share
one channel-neutral Conversation Service. A Chat section is not currently
registered in the web console. The service can call only a small allowlist of
summary and task tools, and summary-tool execution still goes through the
shared `execute_tool` core. It receives compact context only: the current user
message, bounded recent history, the allowed tool catalog, an optional current
task, and redacted summary results.

The Conversation Service uses strict structured output and the backend decides
what is actually executed. It may create a task immediately only when the
operator explicitly asks for one; otherwise it stores a pending task proposal
that requires an operator confirmation button. See
[`conversation.md`](conversation.md) for the concrete contract and limits.

When `CONVERSATION_PROVIDER=ai_manager`, the Conversation Service, Task Router,
and Incident Matcher send bounded redacted context to one private literal IP
resolved from the declared `ai-host` inventory host. The OpenAI-compatible
path is fixed, redirects are disabled, strict model output is validated before
execution, and a shared circuit breaker routes failures to Luna. The LAN model
has no MCP identity or direct tool access.

When `CONVERSATION_PROVIDER=ollama`, the backend sends bounded conversation
context to one operator-configured private Ollama origin. Connection failures,
timeouts, HTTP failures and invalid structured output open a short
process-local circuit breaker and route the turn to Luna. Tool execution begins
only after a valid decision, so fallback cannot duplicate an infrastructure
call. The endpoint and model are server configuration, never caller input.

Authorized Telegram photo and audio messages follow a separate bounded path:

```text
Telegram file_id
  -> fixed Telegram getFile/download origins
  -> byte and duration validation
  -> local Ollama image analysis or audio transcription
  -> bounded text
  -> shared Conversation Service
```

Original media bytes and base64 are not written to conversation history, audit
metadata or model-usage records and are never sent to Luna. Audio decoding is
in-process with fixed mono 16 kHz PCM output; no shell command, caller-selected
path, URL, codec or conversion option is accepted.

## Task Router

The Task Router is an optional asynchronous model pass over newly created tasks.
REST, provider entrypoints, watcher incidents and confirmed Conversation Service
task creation enqueue a durable job in the same transaction as the task; model
inference happens only after that transaction commits. Its output is intentionally
advisory: category, priority, severity, suggested owner, labels, possible
runbook, dedupe candidate, confidence, and first read-only steps.

The router never calls tools, never assigns `assigned_agent`, never changes
task status, and never resolves incidents. The only persisted side effects are
a redacted `task.router_decision` task event and a matching audit event. If the
model is disabled or fails, task creation continues normally.

With `TASK_ROUTER_PROVIDER=opencode_go`, inference uses the fixed OpenCode Go
HTTPS endpoint and reviewed `deepseek-v4-pro` model. The client has no MCP
identity or tool surface, rejects redirects and validates the result again.
Stale queue leases are recovered after a bounded interval; the unique task/job
constraint prevents duplicate routing. ADR 0024 defines the migration of
autonomous remediation to a separate vendor-neutral MCP worker role. The core
owns capability grants, assignment, durable leases, recovery and fencing while
external OpenCode, Codex or future adapters own only their isolated agent
runtime. The legacy OpenCode Fixer remains a rollback boundary until the first
external pull adapter passes conformance.

The **Metrics** section adds immutable per-component usage rows and explicit operator
reviews. Technical success, reviewed accuracy and review coverage remain
separate metrics. Token-based cost is stored with the price snapshot used at
the time and is labelled attributed cost, not invoice-reconciled spend. See
[`metrics.md`](metrics.md).

The inventory-bound AI manager permits one in-process inference at a time and
records queue wait, inference latency, effective provider/model, fallback,
sanitized error category, and prompt/schema/model versions on the same canonical
usage row. No prompt or raw output is added to metrics telemetry.

## Watchers

Watchers are an opt-in detection layer. The first watcher, `lab.alerts`, runs
`lab.alerts.recent`, extracts warning/critical findings, and creates one task
per new open incident. Repeated sightings update the same incident instead of
creating duplicate tasks.

Watchers use `execute_tool` for every infrastructure observation and
`tasks_service.create_task` for task creation. They do not call MCP agents,
the Conversation Service, shell, SSH, arbitrary HTTP, or any write-mode
provider tool. See [`watchers.md`](watchers.md).

## External Sentinel

Watchers detect problems from the console's own home application host/home-network vantage
point, so they are structurally blind when that host, site or outbound path is
unreachable.
External Sentinel is a small, independently released VPS-side service that
covers exactly that gap: fixed-config HTTP health checks plus a heartbeat
receiver, local SQLite incident dedup, and direct Telegram alerting. Its source,
tests and deployment assets live in
[`imdelmare/homelab-console-sentinel`](https://github.com/imdelmare/homelab-console-sentinel).

It is deliberately outside the five-plane model above rather than an
extension of it: it never calls `execute_tool` or any Homelab Console API,
holds no infrastructure credentials, and cannot reach providers, agents,
models, or the tool plane. Its own attack surface is limited to the heartbeat
receiver, which requires a bearer/token match (`SENTINEL_HEARTBEAT_TOKEN`) and
fails closed — a missing or wrong token is always rejected, never treated as
"no auth configured, allow it". See [`sentinel.md`](sentinel.md).

## Trust boundaries

```
                         Internet (untrusted)
                                │
                                ▼
                    ┌───────────────────────┐
                    │  Reverse proxy (HTTPS) │   TLS termination,
                    │        Caddy           │   security headers
                    └───────────┬───────────┘
                                │  loopback / private net only
                                ▼
   ┌────────────────────────────────────────────────────┐
   │       home application host: Homelab Console API + database    │
   │        (home LAN — session/CSRF/authn boundary)      │
   │                                                        │
   │   Operator plane ── Control plane ── Tool plane        │
   │        │                 │               │            │
   │        │                 │        (execute_tool core)  │
   │        │                 │               │            │
   │   Model/agent surfaces — no credentials, no direct     │
   │   provider access, only typed tool requests            │
   └───────────────────────────┬─────────────────────────┘
                                │  trusted LAN
                                ▼
                    ┌───────────────────────┐
                    │     Home network       │
                    │  Proxmox / OPNsense /  │
                    │  MikroTik / HA /       │
                    │  Frigate (provider     │
                    │  APIs, read-only for   │
                    │  the real ones today)  │
                    └───────────────────────┘

   ┌────────────────────────────────────────────────────┐
   │           External Sentinel (separate VPS)            │
   │   independent process, own SQLite, no console API      │
   │   access, no infra credentials — Telegram alerting      │
   │   only. Receives outbound heartbeat over the dedicated  │
   │   WireGuard peer and detects console/site outages.      │
   └────────────────────────────────────────────────────┘
```

Key boundaries:

- **Internet → reverse proxy**: untrusted, TLS-terminated, only exposes the
  public endpoints listed in [security.md](security.md).
- **Reverse proxy → API**: loopback only, never directly internet-reachable.
- **External Sentinel**: deployed independently of the Homelab Console API
  (see [`sentinel.md`](sentinel.md)); it does not sit inside the request
  path above and has no access to the control/tool/model planes.
- **Model plane → tool plane**: models can only request named tool calls
  through the execution core; they hold no credentials and cannot reach
  providers directly.
- **API → providers**: direct trusted-LAN access only through provider
  adapters — never a general-purpose tunnel exposed to the app.
- **home application host → External Sentinel**: outbound heartbeat over the separately
  managed WireGuard peer; Sentinel is not in the provider request path.

See [`security.md`](security.md) for the threat model this boundary
diagram supports, [`authentication.md`](authentication.md) for how the
operator plane's identity boundary works, and
operator deployment guide for the home application host/VPS split deployment.
