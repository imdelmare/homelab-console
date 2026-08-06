# MCP Adapter

`apps/mcp` exposes Homelab Console over [Model Context
Protocol](https://modelcontextprotocol.io). MCP clients can call the same
infrastructure tool plane used by REST, and can also operate persistent tasks
through narrow task tools. They do not get shell, SSH, arbitrary HTTP, raw API
forwarding or provider-specific shortcuts.

The automatically dispatched Fixer operating policy and OpenCode bootstrap
prompt are documented in isolated worker documentation.

## Same execution core, different transport

The MCP adapter is a thin transport layer. Every tool call it receives is
forwarded to the same `execute_tool(tool_id, validated_input, actor,
source="mcp", task_id=task_id, approval_id=approval_id)` function described in
[`architecture.md`](architecture.md#shared-execution-core) that backs
the REST API. There is no separate MCP-only implementation of tool logic,
policy, or redaction — MCP is another door into the same house, not a
different house.

Concretely, this means an MCP client gets exactly the same:

- tool registry (only tools marked `enabled`; write tools are enabled only
  through the operator-approved `APPROVED_WRITE_TOOLS` allowlist, ADR 0004),
- input schema validation (extra fields rejected),
- risk-policy enforcement (low/medium/high/critical),
- per-invocation, single-use, input-bound approval for every write or
  high-risk tool,
- timeouts,
- output normalization and secret redaction,
- audit logging (`AuditEvent`) and task-event recording.

For infrastructure tool calls, `task_id` is MCP metadata. The adapter removes it
from the provider input before validation and passes it separately to
`execute_tool`; provider tools never receive a caller-supplied `task_id` inside
their own payload.

## Transport

The adapter supports two MCP transports:

- **stdio**: launched as a local subprocess by the MCP client.
- **streamable HTTP**: an optional listener bound to loopback or a trusted LAN
  interface and exposed publicly only through the declared Cloudflare Tunnel.
  Every MCP request requires a registered bearer token. When the connector is
  not colocated, a firewall ACL must restrict origin access to its source.

The native `homelab-console-mcp.service` starts the optional HTTP listener for
local/LAN use. Stdio remains client-started. See `scripts/` for
live runtime helpers and `apps/mcp/requirements.txt` for dependencies.

## Authentication

Stdio clients authenticate as the configured agent identity **`MCP_AGENT_ID`**
(see `.env.example`). `MCP_AGENT_ID` must be `claude`,
`fixer`, `codex`, `cline`, or `opencode`; it is read from the process environment/configuration at
startup and is not accepted from individual tool calls.

HTTP clients authenticate with per-client bearer tokens. The token itself
determines the client identity (`claude`, `fixer`, `codex`, `cline`, or `opencode`), label,
fingerprint, dashboard row, and `last_seen_at`.

## HTTP Onboarding

The operator-managed HTTP onboarding flow is the same for Codex, Claude,
Cline and OpenCode:

1. In the **MCP Clients** window, the operator selects the agent and enters its
   label and host fingerprint.
2. The dashboard calls `POST /api/mcp/pairing/start`.
3. The backend sends an approve/deny prompt to the authorized Telegram chat.
4. The dashboard polls `POST /api/mcp/pairing/consume` after approval.
5. The dashboard shows the plaintext token exactly once.
6. The operator stores that token on the client, which connects to
   `https://mcp.example.com/mcp/` with
   `Authorization: Bearer <token>`.

Use these values per client:

| Client | `agent_id` | Example `client_label` | Example `host_fingerprint` |
|---|---|---|---|
| Codex | `codex` | `Codex operator-workstation` | `operator-workstation` |
| Claude | `claude` | `Claude operator-workstation` | `operator-workstation` |
| Fixer | `fixer` | `Fixer` | `opencode-fixer` |
| Cline | `cline` | `Cline operator-workstation` | `operator-workstation` |
| OpenCode | `opencode` | `OpenCode operator-workstation` | `operator-workstation` |

`client_label` is what the dashboard shows. `host_fingerprint` should be stable
for that machine; the hostname is enough for this homelab setup.

The **MCP Clients** window can start this flow directly: choose Codex, Claude,
Fixer, Cline or OpenCode, enter the label and host fingerprint, click **Start Telegram pairing**,
then approve the Telegram request. The UI checks approval immediately and then
every 2 seconds until the request is approved or expires; **Check approval**
can also be clicked manually. When approved, the UI shows the plaintext bearer
token once; paste it into that client's MCP HTTP configuration. The same panel
shows recent pairing requests with status, delivery result, expiry/decision
timestamps and deciding actor, but never exposes pairing secrets or hashes. All
pairing timestamps are displayed in `Europe/Rome`. A
request still stored as `pending` is displayed as **expired** once its
`expires_at` deadline has passed.

The pairing interface, including menus, accessibility labels, status, and error
messages, uses US English. This affects only the operational UI, not agent task
content or stored historical/user content.

### Prompt For New Clients

Give this instruction to a fresh Codex, Claude, Cline or OpenCode session after
starting pairing from the dashboard:

```text
Connect to Homelab Console MCP over its Cloudflare Tunnel HTTPS endpoint.

The operator performs Telegram pairing in the MCP Clients window and will
provide the per-client hmc_... token exactly once. Do not call the internal
/api/mcp/pairing endpoints yourself.

MCP URL: https://mcp.example.com/mcp/
Transport: streamable HTTP
Header: Authorization: Bearer <token>

Do not use a shared static token.
```

### Pairing boundary

Start and consume pairing through the authenticated dashboard. The internal
pairing endpoints require the normal web session and CSRF protections and are
not part of the public MCP transport. Never script them as unauthenticated
calls or expose their one-time pairing secret to an MCP client.

### Client Configuration

All clients use the same MCP HTTP endpoint:

```text
https://mcp.example.com/mcp/
```

The token must be sent as:

```text
Authorization: Bearer hmc_...
```

For Codex, configure the token as an environment variable and point the MCP
server at it:

```bash
export HOMELAB_MCP_TOKEN="hmc_..."
codex mcp add homelab-console \
  --url https://mcp.example.com/mcp/ \
  --bearer-token-env-var HOMELAB_MCP_TOKEN
codex
```

For OpenCode, keep the token outside the checked-in configuration:

```bash
export HOMELAB_MCP_TOKEN="hmc_..."
```

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "homelab-console": {
      "type": "remote",
      "url": "https://mcp.example.com/mcp/",
      "enabled": true,
      "oauth": false,
      "headers": {
        "Authorization": "Bearer {env:HOMELAB_MCP_TOKEN}"
      }
    }
  }
}
```

For Claude and Cline, use the same URL and bearer token in the client's MCP
HTTP configuration UI or JSON config. If a client cannot complete onboarding
itself, run the two manual pairing calls, copy the `hmc_...` token from the
approved `consume` response, and paste it into that client's MCP HTTP config.

### Dashboard And Revocation

The dashboard lists every registered MCP client separately. A client is
considered `online` only when its `last_seen_at` is within the UI freshness
window; otherwise it shows as `idle` even though the token is still valid.

Use the dashboard revoke action to disable one client without affecting the
others. Revoked clients immediately fail MCP HTTP authentication with `401`.
The **MCP Clients** window also contains copyable onboarding prompts for Codex,
Claude, Cline and OpenCode, all using the same Telegram pairing flow and HTTP endpoint.
The dashboard can start/check a pairing request, revoke a client, or rotate one
client's bearer token. Rotation updates the stored token hash in place,
invalidates the previous token immediately, and returns the new plaintext token
only in that one response. After revocation, **Forget** permanently removes the
client registration and token hash. The backend rejects Forget for active
clients; pairing history and audit events remain available.

### Troubleshooting

- `401 unauthorized` from `https://mcp.example.com/mcp/`: the client did not
  send the bearer token, used the wrong token, or the token was revoked.
- `202 pairing_not_approved` from `/api/mcp/pairing/consume`: approve the
  Telegram prompt, then retry.
- `409 pairing_consumed`: the one-time pairing was already exchanged for a
  token; start a new pairing if the token was lost.
- No Telegram message: check `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_ALLOWED_CHAT_ID`, and the API logs.
- Dashboard shows `idle`: make the client perform any MCP request; `last_seen_at`
  updates when a registered bearer token is accepted.

Stdio clients can also use **MCP client registration**:

1. First startup has no valid local client token.
2. The adapter creates a pairing request with agent id, client label and host
   fingerprint.
3. The backend sends an approve/deny prompt to the authorized Telegram chat.
4. After approval, the adapter consumes the pairing request and receives a
   token once.
5. The adapter stores the token locally with `0600` permissions.
6. Later startups validate the token and update `last_seen_at`.

The backend stores only a token hash and a short token hint, never the token
plaintext. Operators can list, revoke and forget registered clients through
`/api/mcp/clients`; forgetting is restricted to clients that are already
revoked.

Useful environment variables:

```bash
MCP_AGENT_ID=codex
MCP_CLIENT_TOKEN_PATH=~/.config/homelab-console/mcp-codex.token
MCP_CLIENT_LABEL="Codex workstation"
MCP_PAIRING_TIMEOUT_SECONDS=300
python apps/mcp/server.py
```

Claude, Codex, Cline and OpenCode are **external MCP clients** — they connect to this
adapter over stdio or HTTP, exactly like any other MCP client. The control
panel never starts, supervises, or calls out to an LLM API on
their behalf; nothing in the task lifecycle makes an Anthropic/OpenAI/any
model API call. Telegram free-text replies and authenticated REST
conversation endpoints use a separate, deliberately narrow conversation service — see
[`architecture.md`](architecture.md) — which is unrelated to this
adapter and to task ownership.)

## Tool discovery and execution

- **Discovery**: the adapter lists the currently enabled tools from the
  tool registry, with their id, description, and input schema, so an MCP
  client can present them to a model or a human without hardcoding the catalog.
  It also exposes typed `tasks.*` tools for task state and `approvals.*`
  tools for the write-approval flow. Enabled write tools (necessarily
  ADR-approved) are listed with an extra optional `approval_id` input.
- **Write approvals**: an agent requests approval for one exact invocation
  with `approvals.request(tool_id, input, task_id?)`; the operator decides on
  Telegram (inline buttons) or in the console Approvals window. The agent
  polls `approvals.get` and, once approved, calls the tool with that
  `approval_id` and the same input. Approvals are single-use, input-bound
  (SHA-256 of the validated input) and expire after `APPROVAL_TTL_SECONDS`
  (default 600).
- **Execution**: a call names a `tool_id` and input payload; the adapter
  validates the shape at the MCP layer and then hands off to
  `execute_tool`, which re-validates against the Pydantic schema and runs
  the full pipeline described in [`architecture.md`](architecture.md).
- Results returned to the MCP client are the same normalized, redacted
  output the REST API would return for the same tool call.

## Persistent task tools

Task tools modify only Homelab Console application state, so they never
require an infrastructure write approval.

MCP clients should load the `homelab_task_workflow` prompt at session start and
treat it as operating instructions for task work. It states the required
lifecycle explicitly, including the common completion path:

```text
tasks.claim
tasks.set_status(status="investigating")
...
tasks.update_summary
tasks.complete
```

`tasks.complete` is valid only from `investigating`; a direct
`claimed -> completed` attempt is rejected by the backend. If a task is still
`claimed`, first call `tasks.set_status(status="investigating")` with the latest
`expected_version`, then call `tasks.complete`.

Read tools:

- `tasks.list`
- `tasks.get`
- `tasks.context`
- `tasks.events.list`
- `tasks.findings.list`
- `tasks.checks.list`

Write tools:

- `tasks.create`
- `tasks.claim`
- `tasks.release`
- `tasks.set_status`
- `tasks.update_summary`
- `tasks.add_note`
- `tasks.add_finding`
- `tasks.resolve_finding`
- `tasks.add_check`
- `tasks.complete_check`
- `tasks.skip_check`
- `tasks.complete`
- `tasks.reopen`

`tasks.context` returns the backend-compiled agent brief: task identity, linked
incident, provider ids/status, bounded recent findings/checks/events/tool
invocations, recent watcher runs, recommended read-only tool allowlist, budget,
stop conditions and deterministic `recommended_next_step` (a fixed string
chosen from task status/checks/findings — no LLM call involved). The MCP
adapter only exposes this read model; it does not compile context itself.

## Task ownership

Once a task is `assigned_agent`-claimed, only that agent may mutate it.
`tasks.release`, `tasks.set_status`, `tasks.update_summary`, `tasks.add_note`,
`tasks.add_finding`, `tasks.resolve_finding`, `tasks.add_check`,
`tasks.complete_check`, `tasks.skip_check` and `tasks.complete` all check
that the calling agent (`agent:claude`, `agent:fixer`, `agent:codex`, `agent:cline`, or `agent:opencode`,
from `MCP_AGENT_ID`)
matches `assigned_agent`; a mismatch — including an agent trying to act on a
task nobody has claimed yet — fails with `not_task_owner`. A human operator
acting through the authenticated web UI (REST, `kind="user"`) or Telegram
always bypasses this check (administrative override); `tasks.claim` itself
stays atomic and idempotent for the same agent re-claiming its own task.

`tasks.release` works from any active assigned status — `claimed`,
`investigating`, `waiting_operator`, `blocked` — and always returns the task
to `open` with `assigned_agent` and `claimed_at` cleared, recording
both the canonical `task.status_changed` transition and `task.released` with
the handoff details. Claims likewise record `task.status_changed` plus
`task.claimed`; every task status change passes through the same transition
helper.

A `completed`/`cancelled` task is immutable until explicitly reopened:
summary/note/finding/check mutations and infrastructure tool calls made with
its `task_id` fail with `task_not_active`, including operator/service callers.
A successful task-linked tool call on an active task bumps its `version` (in
addition to `last_activity_at`).

To avoid an extra `tasks.get` round trip after every infrastructure tool
call, the response to any call made with `task_id` includes a `task_version`
field — the task's current version after that call, whether it succeeded or
was rejected (e.g. `not_task_owner`, `task_not_active`). Use it directly as
`expected_version` on the next `tasks.*` mutation for that task.

## Agent handoff

The intended workflow, first agent:

```text
tasks.list
tasks.claim
tasks.context
tasks.set_status(investigating)
lab.summary(task_id=...)
tasks.add_finding
tasks.add_check
tasks.update_summary
tasks.release(handoff_summary="...")
```

Second agent:

```text
tasks.claim
tasks.context
tasks.complete_check
tasks.update_summary
tasks.complete
```

Concretely, a typical Claude → Codex handoff:

1. Claude claims the task with `tasks.claim`, sets `tasks.set_status` to
   `investigating`, and runs infrastructure tools (e.g. `lab.summary`) with
   `task_id` set so invocations are linked.
2. Claude records findings/checks/summary with `tasks.add_finding`,
   `tasks.add_check`, `tasks.update_summary`.
3. Claude releases the task with `tasks.release` and a non-empty
   `handoff_summary` (or leaves it in `waiting_operator` for a human, via
   `tasks.set_status`).
4. Codex claims the same task with `tasks.claim` — it's `open` again after
   release, so `tasks.set_status(investigating)` first if it needs to run
   more tools.
5. Codex reads `tasks.context` (summary, pending checks, open findings,
   `recommended_next_step`) and continues the work: `tasks.complete_check`,
   `tasks.update_summary`, then `tasks.complete`.

## What it cannot bypass

Because MCP terminates in the same `execute_tool` core:

- it cannot call a disabled tool,
- it cannot run a write tool outside the operator-approved
  `APPROVED_WRITE_TOOLS` allowlist, and even an approved write tool needs a
  fresh operator decision for every single invocation — agents can request
  approvals, never grant them,
- `tasks.*` writes only affect task state and still go through typed schemas,
- it cannot skip approval for a `requires_confirmation` tool,
- it cannot receive unredacted secrets — the same centralized redaction
  applies,
- it cannot invent a tool outside the registry — only declared,
  schema-validated tools exist,
- it cannot reach a provider directly — it only ever reaches providers
  through a tool, never a raw connection,
- it cannot mutate a task owned by a different agent, or run infrastructure
  tools against a task it doesn't own once that task has been claimed.

In short, the MCP adapter changes *how* a caller reaches the tool plane, not
*what* the tool plane will do.
