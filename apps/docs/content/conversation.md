# Conversation Service

The Conversation Service is the lightweight natural-language layer used by
Telegram chat, the Operations panel and authenticated REST endpoints. The desktop Chat app is
not currently registered in the web UI. It is intentionally separate from the
MCP agent workflow: it helps the operator ask questions, run a small set
of read-oriented summary tools, and create or update Homelab Console tasks, but
it never becomes an autonomous agent.

## Scope

The service can:

- understand short operator requests in natural language;
- choose from a fixed allowlist of summary/task tools;
- call summary tools through `execute_tool`;
- read tasks, create tasks, and update task summaries through the task service;
- propose task creation and wait for explicit operator confirmation;
- write conversation messages, model usage, and estimated cost to the database.

The service cannot:

- execute shell commands;
- use SSH;
- call arbitrary URLs;
- modify provider configuration;
- run infrastructure write tools;
- bypass `execute_tool`, validation, policy, redaction, audit, or task
  ownership.

## Channels

Current channels use the same backend service:

- **Authenticated REST**: `POST /api/conversations/message`, authenticated with
  the normal session + CSRF flow. No Chat app is currently registered in the
  desktop UI, but the endpoint is already available for authenticated clients.
- **Telegram free chat**: ordinary non-command text uses a one-decision,
  tool-free response contract. It receives no tool catalog or current task and
  cannot create or update tasks. When live data is needed, the reply directs
  the operator to the Operations panel.
- **Telegram Operations**: slash commands remain deterministic (`/menu`, `/status`,
  `/tasks`, `/incidents`, `/watchers`, `/mcp`, `/provider`, `/approve`,
  `/deny`). Inline callback buttons provide the operator dashboard. Non-command
  The Operations panel exposes fixed summary shortcuts and a one-shot
  `ForceReply` question. Fixed shortcuts execute their declared summary tool
  first and need only one model pass to explain the result. An arbitrary live
  question is bound to the authorized user and chat by a five-minute,
  single-use nonce; only that reply enters the governed tool-selection flow.
  Task proposals use a separate inline button and short-lived nonce.

This channel-neutral shape is deliberate so a future chat UI can reuse the same
service contract without changing the model/tool policy.

## Language policy

Assistant replies, task titles, goals, and summaries generated from a
conversation, and conversational analysis of Telegram images use Italian.
Audio transcription preserves the language actually spoken; it is not
translated before being stored or passed as
bounded context. Existing conversation history and operator-provided content
are never translated. This is separate from the US English web, Telegram
operations, notification, and MCP-pairing interfaces.

## Telegram photos and voice messages

The authorized Telegram operator can send a photo, voice message, or audio
attachment. The API obtains the exact `file_id` from Telegram, resolves it
through Telegram's fixed `getFile` API, and enforces configured byte and
duration limits before analysis.

Media analysis is local-only:

- JPEG, PNG, and WebP images are sent to the configured Ollama model;
- voice/audio is decoded in-process and normalized to mono PCM WAV at 16 kHz,
  then sent to Gemma through Ollama's multimodal input;
- only the bounded description or transcript enters the Conversation Service;
- original bytes and base64 are never stored in conversation history, audit
  metadata, or model usage records;
- media is never sent to the Luna fallback. If Ollama is unavailable, the bot
  returns a controlled attachment-analysis error.

PyAV performs audio decoding with fixed output parameters. No shell command,
caller-supplied path, URL, codec, or conversion option is executed. Defaults
are 8 MB per image, 10 MB per audio file, and 120 seconds per audio message,
controlled by `TELEGRAM_MEDIA_*` settings.

## Model Contract

The service supports the OpenAI Responses API and a fixed Ollama `/api/chat`
endpoint. Ollama can be selected as the primary model with Luna as the
automatic fallback. Both adapters enforce the same strict structured-output
schema. The model receives only a compact context:

- the current user message;
- a short bounded conversation history;
- the allowed tool catalog;
- the current task, when attached;
- compact redacted tool results from the previous step.

It does not receive raw provider credentials, raw provider config files, full
audit dumps, unbounded history, or arbitrary provider payloads.

Governed Operations questions must return this structured shape:

```json
{
  "assistant_reply": "...",
  "tool_calls": [],
  "create_task": false,
  "task_title": null,
  "task_goal": null,
  "update_task_id": null,
  "update_task_summary": null,
  "needs_clarification": false
}
```

Free chat and fixed Operations shortcuts instead use a strict reply-only
schema containing `assistant_reply`. The backend ignores any model attempt to
introduce tools or task mutations outside the governed Operations question
contract.

The backend, not the model, decides what is actually executed. Invalid tools are
reported as `tool_not_allowed`; tool inputs are still validated by the execution
core or task service.

## Allowed Tools

The allowlist is intentionally narrow:

- `lab.summary`
- `lab.alerts.recent`
- `lab.network.summary`
- `lab.storage.summary`
- `lab.security.summary`
- `lab.automation.summary`
- `tasks.list`
- `tasks.get`
- `tasks.create`
- `tasks.update_summary`

`lab.storage.summary` includes NUT UPS state when the `nutups` provider is
configured, so Luna can include power/battery findings without broadening the
allowlist to arbitrary provider calls.

Summary tools go through `app.tools.execution.execute_tool` with
`source="conversation"`. Task tools are thin calls into
`app.services.tasks_service`, which owns validation, audit, redaction, and
task-state rules for Homelab Console's own database.

## Limits

The runtime limits are configurable through environment variables:

| Setting | Default | Meaning |
|---|---:|---|
| `CONVERSATION_ENABLED` | `true` | Master gate; when false, no conversation, media analysis, Task Router worker or model-assisted incident matching is invoked |
| `CONVERSATION_PROVIDER` | `ollama` | `ai_manager` for inventory-bound LAN inference with Luna fallback, `ollama` for local inference, `openai` for Luna only, or `opencode_go` for direct OpenCode Go inference |
| `CONVERSATION_MODEL` | `gpt-5.6-luna` | OpenAI model used for conversation routing |
| `OPENCODE_GO_API_KEY` | empty | Server-side OpenCode Go API key; never caller-visible or persisted |
| `OPENCODE_GO_CHAT_MODEL` | `deepseek-v4-flash` | Closed-enum lightweight Chatter model for the fixed Go endpoint |
| `OPENCODE_GO_ROUTER_MODEL` | `deepseek-v4-pro` | Closed-enum asynchronous Task Router model |
| `OPENCODE_GO_MAX_ATTEMPTS` | `3` | Bounded retries for transient transport/status failures |
| `CONVERSATION_REASONING_EFFORT` | `low` | Reasoning effort sent to the model |
| `CONVERSATION_MAX_TURNS` | `4` | Bounded history window |
| `CONVERSATION_MAX_TOOL_CALLS` | `3` | Maximum tool calls in one turn |
| `CONVERSATION_MAX_OUTPUT_TOKENS` | `600` | Maximum model output tokens |
| `CONVERSATION_TIMEOUT_SECONDS` | `60` | OpenAI request timeout |
| `AI_MANAGER_HOST_ID` | `ai-host` | Inventory host used by the LAN AI manager |
| `AI_MANAGER_PORT` | `8080` | Port that must also be declared in the host's `check_ports` |
| `AI_MANAGER_MODEL` | `Qwen3.5-4B-Q8_0` | llama.cpp model alias |
| `AI_MANAGER_CONNECT_TIMEOUT_SECONDS` | `2` | Fast LAN connection timeout |
| `AI_MANAGER_TIMEOUT_SECONDS` | `60` | LAN inference timeout |
| `AI_MANAGER_FAILURE_COOLDOWN_SECONDS` | `90` | Shared cooldown before retrying the LAN manager |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Fixed server-side Ollama endpoint; override locally for a dedicated model host |
| `OLLAMA_MODEL` | `gemma4-e4b-agentic:latest` | Local primary model |
| `OLLAMA_CONNECT_TIMEOUT_SECONDS` | `2` | Fast local connection timeout |
| `OLLAMA_TIMEOUT_SECONDS` | `90` | Local inference timeout |
| `OLLAMA_FAILURE_COOLDOWN_SECONDS` | `90` | Skip Ollama briefly after a failure |
| `OLLAMA_KEEP_ALIVE` | `10m` | Keep the local model loaded between requests |
| `TELEGRAM_MEDIA_ENABLED` | `true` | Allow authorized Telegram photo/audio analysis |
| `TELEGRAM_MEDIA_MAX_IMAGE_BYTES` | `8000000` | Maximum Telegram photo download size |
| `TELEGRAM_MEDIA_MAX_AUDIO_BYTES` | `10000000` | Maximum voice/audio download size |
| `TELEGRAM_MEDIA_MAX_AUDIO_SECONDS` | `120` | Maximum voice/audio duration |

When the AI manager is selected, conversation, task routing, and ambiguous
incident matching use the same inventory-declared private host and shared
circuit breaker. The endpoint path is fixed to `/v1/chat/completions`, redirects
are disabled, and OpenAI is used after transport, HTTP, malformed response, or
schema-validation failures. The OpenAI API key is never sent to the LAN host.

When Ollama is selected, connection failures, timeouts, HTTP errors, malformed
responses, and schema-validation failures open a process-local circuit breaker
and route the current request to Luna. No tool is executed until a model
decision has passed schema validation, so fallback cannot duplicate tool
execution. The endpoint is operator configuration, never caller input, and
must only be exposed over the trusted private network.

When OpenCode Go is selected, the API calls the documented fixed HTTPS Chat
Completions endpoint directly with the reviewed `deepseek-v4-flash` model. Callers cannot
choose the endpoint or an arbitrary model; redirects are disabled, retries are
bounded to transient failures, and the result is validated again before any
tool execution. OpenCode Go failures fall back to the inventory-bound AI
manager, which retains its existing Luna fallback. Each fallback happens before
a validated decision can request a governed tool, so it cannot replay tool
execution. Failed turns store normalized telemetry without prompts, raw output
or the API key. OpenCode documents 30-day retention for Grok 4.5; choose the
closed-enum DeepSeek rollback only if its latency/quality is acceptable and
zero-retention is required. See ADR 0020.

Telegram free text uses Telegram's native typing indicator while inference is
running, then sends the final reply with the existing navigation callbacks.
The home screen exposes an **Operations** panel instead of coupling the feature
name to a model provider. Summary buttons map to fixed `lab.*.summary` tools;
**Ask live question** sends a new `ForceReply` prompt and automatically returns
to tool-free chat after its single response.
The webhook keeps a bounded, process-local cache of completed Telegram
`update_id` values so ordinary retries do not duplicate a turn or progress
message. A savepoint rolls back task-tool database effects if the follow-up
model decision fails. The cache is not a cross-process or restart-persistent
idempotency guarantee: an update replayed after an API restart can be processed
again, so persistent webhook idempotency remains a separate hardening item.

Token usage and estimated cost are stored on assistant messages. Cost estimates
come from `CONVERSATION_INPUT_COST_PER_MILLION` and
`CONVERSATION_OUTPUT_COST_PER_MILLION`; leave them at `0` if unknown.
The REST response includes the model, input/output token counts, estimated
cost, and the summary tools used for each assistant response so an operator or
future UI can validate whether the answer came from live control-panel data.

Transient OpenAI failures (`408`, `409`, `429`, and `5xx`) are retried briefly.
Non-transient API errors such as invalid schema/model/input failures are not
retried, so task creation and confirmations are not duplicated.

## Task Creation Flow

If the operator explicitly asks to create a task ("apri una task", "crea
ticket", etc.), the backend creates it immediately using the task service.
When `TASK_ROUTER_ENABLED=true`, the created task and a durable routing job are
committed together. The asynchronous Task Router later appends an advisory
`task.router_decision` timeline event; conversation latency no longer includes
router inference. The router does not confirm, claim, assign, or execute the task.
`TASK_ROUTER_PROVIDER` can override the conversation provider; when empty it
inherits `CONVERSATION_PROVIDER`.

If the model only thinks a task would be useful, the backend stores a pending
task proposal instead:

1. the assistant replies with the proposal;
2. the web UI shows a confirmation action, or Telegram shows an inline button;
3. the operator confirms via `POST /api/conversations/task-proposals/{nonce}/confirm`
   or the Telegram callback;
4. only then is the task created.

Pending proposals are nonce-protected, single-use, and expire after 15 minutes.
Confirmed proposals follow the same asynchronous Task Router enrichment path as
explicit task creation.

## Migration

The conversation tables are part of the Alembic-managed schema:

- `conversations`
- `conversation_messages`

For a fresh live database, `alembic upgrade head` creates both tables
alongside the rest of the control-plane schema. PostgreSQL is the only supported
database for the API in development, tests and live operation.
