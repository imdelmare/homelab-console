# Metrics

The **Metrics** section separates three different questions that
must not be collapsed into one score:

- **technical reliability**: successful Task Router responses divided by all
  recorded Task Router calls;
- **reviewed accuracy**: operator-accepted decisions divided by all reviewed
  decisions;
- **review coverage**: reviewed decisions divided by all router decisions in
  the selected period.

Task completion is not treated as proof that the initial routing was correct.
An operator records explicit `accepted`, `corrected`, or `rejected` feedback.
Corrections are schema-constrained, stored in `task_router_reviews`, mirrored as
`task.router_reviewed` events, and audited.

Reviews can correct action, category, priority, severity, suggested owner and
whether operator attention is required. The operator note is stored separately
from the model decision and remains part of the canonical review. Completing a
task is never treated as implicit approval of its routing decision.

## AI manager operations

Calls routed through the inventory-declared AI manager record only normalized
operational telemetry, never prompts or raw model output:

- effective provider and model;
- local/fallback outcome and sanitized failure category;
- queue wait and inference latency;
- prompt, schema and model versions;
- token usage already covered by the canonical usage record.

The API process permits one llama.cpp inference at a time. This matches the
single slot on `ai-host`; the recorded queue wait makes saturation visible.
The Metrics section reports local response rate, OpenAI fallback rate, schema
errors, timeouts, average and p95 latency, and effective model distribution.
Historical rows remain valid but have no invented latency or version metadata.

## AI delivery

The **AI Delivery** section presents conversation turns separately from
Task Router quality. One delivery row represents one completed or failed
conversation turn, not one provider request: a turn that uses a governed tool
can contain two model decisions, and its recorded model-path latency is the sum
of those measured decisions. The section reports successful and failed turns,
fallback rate, average/p95 path latency, effective provider/model distribution,
and the latest 20 normalized delivery records.

Delivery rows classify the route as **Free chat**, **Operations shortcut**, or
**Operations question**. Free chat has no tool contract and uses one model
decision. A fixed shortcut executes one declared summary tool and then one
model decision. Only an arbitrary Operations question can require the two-pass
model → tool → model path.
Historical rows without route schema metadata remain explicitly classified as
**Legacy / unclassified** rather than being attributed to a new route.

Provider failures are recorded with sanitized categories and fallback metadata.
Prompts, conversation text, raw provider responses, credentials and tool
payloads are never copied into `llm_usage_events` or the delivery ledger.

These reviews and measurements are operational application state. This
milestone does not export a dataset or perform online training.

## Usage and attributed cost

`llm_usage_events` stores normalized usage for:

- `task_router`;
- `incident_matcher`;
- `conversation`.

Each row records the model, status, input/cached/output/reasoning token counts,
the price snapshot used, attributed USD cost, source reference and optional
task. Historical events and conversation messages are backfilled
idempotently at API startup. Missing historical metering remains missing and
reduces the displayed coverage; it is never replaced with an invented value.

The built-in `gpt-5.6-luna` standard price snapshot is dated 2026-07-09:
$1.00/M input, $0.10/M cached input and $6.00/M output. See the
[official model page](https://developers.openai.com/api/docs/models/gpt-5.6-luna).

The number in this section is **attributed cost**, calculated from response
usage. It is not labelled as billed cost. Financial reconciliation requires a
separate, server-only integration with OpenAI's organization Costs endpoint
and an Admin API key; that integration is intentionally not enabled in this
milestone.

## API

```http
GET /api/luna/metrics?days=30
```

Returns the summary, per-component usage, AI delivery metrics, router review
metrics, auto-investigate policy outcomes and a recent unreviewed queue.

```http
POST /api/luna/tasks/{task_id}/review
{"verdict":"corrected","corrections":{"suggested_owner":"fixer"}}
```

All endpoints require an authenticated session; review writes also require the
normal CSRF token. No infrastructure action or tool execution is exposed.
