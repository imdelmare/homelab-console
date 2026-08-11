# Fixer dispatch contract v1

> **Legacy migration boundary.** ADR 0024 supersedes Fixer as a product role
> with the vendor-neutral `REMEDIATION_WORKER_V1` pull protocol. This contract
> remains valid only while the in-tree OpenCode runtime is migrated and must not
> dispatch the same task concurrently with a remediation worker lease.

## Scope and identities

Fixer is the only external OpenCode worker. Chat and Task Router use direct,
tool-free provider inference in the core API and are not part of this contract.

The automated worker owns the distinct MCP/task identity `agent:fixer`; an
interactive OpenCode client remains `agent:opencode`. Tokens and identities are
never shared. Infrastructure access remains exclusively through Homelab Console
MCP and `execute_tool`, including approval, policy, validation, redaction and
audit.

## HTTP supervisor API

The supervisor binds only to loopback, with default port `8767`.

### `GET /health`

Returns `200`:

```json
{"ok":true,"active":0}
```

`active` is the number of locally running task processes. Other paths return
`404 {"ok":false,"code":"not_found"}`.

### `POST /fixer`

Requires:

```http
X-Secret: <FIXER_DISPATCH_SECRET>
Content-Type: application/json
```

The body is limited to 4096 bytes and must contain exactly one UUID field:

```json
{"task_id":"00000000-0000-4000-8000-000000000000"}
```

No prompt, command, target, model or tool is caller-selectable.

| Status | Body |
|---|---|
| `202` | `{"ok":true,"code":"accepted"}` |
| `400` | `{"ok":false,"code":"invalid_input"}` or `invalid_json` |
| `403` | `{"ok":false,"code":"unauthorized"}` |
| `404` | `{"ok":false,"code":"not_found"}` |
| `409` | `{"ok":false,"code":"already_running"}` |
| `503` | `{"ok":false,"code":"spawn_failed"}` |

Authentication comparison is constant-time. Unauthorized bodies are bounded
and discarded without launching work.

## Core authorization contract

Before dispatch, the core must:

1. resolve the exact task;
2. assign an open unowned task to `agent:fixer`, or verify that ownership;
3. require a dispatchable status (`claimed`, `investigating`,
   `waiting_operator`, `blocked`);
4. persist and commit `task.fixer_dispatch_requested` bound to task, owner and
   authorizing actor;
5. re-read and validate that authorization before sending only `task_id`;
6. audit the normalized dispatch outcome.

The supervisor does not receive canonical task data. The launched agent must
retrieve the task through MCP and independently verify ownership and dispatch
history.

## Worker process boundary

One process per task is launched with an exact no-shell argument vector using
`opencode --pure run`, fixed agent `homelab-fixer`, JSON output and a fixed
project directory. Child stdin/stdout/stderr are not an application protocol.
The child environment is allowlisted and excludes application/provider
credentials. Timeout is bounded to 30–7200 seconds; timeout terminates then
kills the child if necessary.

## Compatibility, conformance and rollback

Compatible v1 client/supervisor pairs must test exact payload, body bounds,
constant-time secret auth, loopback binding, duplicate locking, spawn failure,
exact no-shell launch, environment filtering and core event/ownership binding.

Rollback disables `FIXER_DISPATCH_ENABLED`, stops the supervisor, revokes its
dedicated MCP token and releases tasks still owned by `agent:fixer`. Chat,
Router and interactive OpenCode remain unaffected. Breaking HTTP, identity or
authorization semantics require v2.
