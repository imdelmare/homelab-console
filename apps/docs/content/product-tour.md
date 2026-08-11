# Product Tour

Homelab Console turns infrastructure observations into a workflow a human can
inspect and control. This tour follows one synthetic DNS incident from detection
to evidence, approval, and audit. No live inventory or operator data appears in
these screenshots.

## Start in the control room

[![The Homelab Console overview showing provider health, incidents, tasks, MCP clients, watcher activity, and an operational runbook.](/product-tour/overview.webp)](/product-tour/overview.webp)

The Overview window brings together provider health, open incidents, the task
queue, connected MCP clients, watcher activity, and read-only runbook guidance.
It is an operational index rather than a second monitoring system: each card
leads to the canonical provider, task, or audit record behind it.

## Preserve evidence in a task

[![A task investigation showing ownership, lifecycle controls, a summarized finding, and synthetic evidence.](/product-tour/task-evidence.webp)](/product-tour/task-evidence.webp)

Tasks are the durable handoff boundary between operators and agents. A task can
carry ownership, findings, checks, invocation evidence, lifecycle history, and a
bounded root-cause context. Agents can hand work to one another without making a
chat transcript the source of truth.

## Keep the human in charge of writes

[![The Approvals window showing waiting, consumed, and denied write requests.](/product-tour/approvals.webp)](/product-tour/approvals.webp)

Infrastructure writes are not unlocked by connecting an agent. Every write
requires a fresh approval for one exact tool invocation and input. The operator
can approve or deny it in the console or through Telegram; approvals expire and
cannot be replayed.

## Inspect the complete audit trail

[![The Audit Log showing a successful tool invocation, an approval request, a task finding, and approval decisions.](/product-tour/audit.webp)](/product-tour/audit.webp)

REST, Telegram, watchers, and MCP clients converge on the same audit model. The
log attributes each event to an actor and source, links it to tools and tasks,
and stores only normalized, redacted metadata suitable for operational review.

## What the tour demonstrates

1. A watcher reports a bounded observation and opens an incident.
2. The incident is represented by a persistent task with explicit ownership.
3. Read-only tools collect normalized evidence through the execution core.
4. A write request stops at the approval boundary until the operator decides.
5. Task, approval, and execution events remain attributable in the audit log.

::: info SYNTHETIC DATA
The product-tour dataset is fictional and was rendered through the real web
interface. Hostnames, identifiers, timestamps, provider states, and operational
events were created exclusively for these screenshots.
:::

Continue with [Architecture](./architecture.md) for the five-plane model or
[Security](./security.md) for the trust boundaries behind this workflow.
