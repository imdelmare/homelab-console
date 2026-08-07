# Notification Engine

Homelab Console owns operational notification policy. Producers (watchers,
REST, Telegram and MCP agents) record canonical findings and task transitions;
they never deliver Telegram messages directly.

```text
finding or task event -> policy -> persistent outbox -> Telegram delivery
```

## Policy

- Local critical findings (UPS, thermal, SMART and expired TLS) are available
  for immediate delivery. Infrastructure critical findings use a short
  debounce so transient outages can clear before Telegram delivery.
- Warning-level watcher incidents wait for the configured debounce period. If
  the incident clears or its task reaches a final state before delivery, the
  outbox row is cancelled.
- Successfully delivered fingerprints enter severity-specific cooldown.
  Repeated events remain auditable as `suppressed` rows.
- Connectivity incidents from different watchers are grouped by their shared
  dependency-graph root during a bounded aggregation window. Canonical
  incidents and tasks remain separate; only the Telegram presentation is
  aggregated.
- Aggregate `lab.alerts` findings join topology groups only for provider-health
  failures; application errors and intentionally stopped guests remain separate.
  If a critical incident joins a pending warning group, the delivery deadline
  is shortened to the critical debounce instead of retaining the warning delay.
- Before delivery the worker rechecks every grouped incident and omits resolved
  symptoms. When the complete group later clears, the original Telegram
  message is edited to show that it resolved automatically.
- Fingerprints are deterministic and versioned (`v1:...`). Models may classify
  semantic duplicates but never define canonical fingerprints.
- Every provider condition has one watcher owner. Dedicated UPS, Kuma,
  Cloudflare, gateway/WireGuard and ZeroTier watchers are excluded from the
  aggregate `lab.alerts` stream, preventing duplicate tasks and deliveries.
- Watcher incidents clear only after three consecutive successful misses by
  default; this grace period is independent from warning debounce and cooldown.

## Delivery guarantees

`notification_outbox` is canonical delivery state. Each row has a unique
idempotency key and moves through `pending`, `sending`, then `sent`, `failed`,
`suppressed` or `cancelled` (with transient edit states for recovery updates).
The worker claims one due row transactionally,
stores Telegram's `message_id`, and retries transient failures with bounded
exponential backoff. Stored text and reply markup pass through centralized
redaction.

The External Sentinel remains independent and sends directly to Telegram so it
can report failure of Homelab Console itself.

## Presentation policy

Automatic Telegram notifications use US English, including notifications about
tasks, approvals, Luna, and the External Sentinel. User-supplied content and
historical records embedded in a notification are not translated; normal
redaction, aggregation, and length limits still apply.

## MCP boundary

MCP agents produce findings and task state through the existing execution core
and task control plane. They cannot send, retry or suppress notifications.
Dynamic read-only tools expose `notifications.status` and
`notifications.outbox.list`; the latter omits message text and credentials.

## Configuration

```ini
NOTIFICATION_OUTBOX_ENABLED=true
NOTIFICATION_WORKER_INTERVAL_SECONDS=2
NOTIFICATION_WARNING_DEBOUNCE_SECONDS=900
NOTIFICATION_COOLDOWN_SECONDS=7200
NOTIFICATION_CRITICAL_DEBOUNCE_SECONDS=120
NOTIFICATION_CRITICAL_COOLDOWN_SECONDS=1800
NOTIFICATION_AGGREGATION_WINDOW_SECONDS=120
NOTIFICATION_MAX_ATTEMPTS=5
```

Keep warning debounce longer than one normal watcher interval. With the
default 600-second watcher cadence and 900-second debounce, a one-sample flap
is observed and audited but the next clean run cancels delivery before it can
reach Telegram.
