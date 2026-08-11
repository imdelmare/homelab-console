# Product Tour

Homelab Console turns infrastructure observations into a workflow a human can
inspect and control. The operator console is a framework-free TypeScript/Vite
application with a minimal **Quiet Operations** interface. It is organized as a
ledger, not a card-heavy monitoring dashboard.

## Start in Inbox

**Inbox** is the default section. Its status rail summarizes healthy systems,
critical incidents, tasks needing attention and pending approvals. The list
contains only open operational work and links directly to canonical records.

The console polls bounded read endpoints while visible, pauses background work
when the browser tab is hidden and resumes on focus. Sidebar badges expose
attention counts without turning every section into a dashboard.

## Inspect the affected system

**Systems** lists configured providers and their normalized health states. A
provider detail contains:

- summary status, timestamps, driver, transport and watchers;
- governed read/write capabilities from the tool registry;
- normalized capability observations;
- a direct investigation-task action for unhealthy providers.

**Topology** complements Systems with declared physical relationships and a
failure-impact view. Neither section scans arbitrary hosts or exposes raw
provider responses.

## Work an incident

**Incidents** is separate from watcher configuration. It shows severity,
provider, watcher, recurrence, first/last observation and the linked task. An
operator can mark an incident already handled only with an explicit note.

**Watchers** owns schedules, thresholds, investigation mode, manual execution
and the recent run ledger. This separation keeps detection configuration apart
from incident response.

## Preserve evidence in a task

**Tasks** is the durable handoff boundary between operators, interactive MCP
clients and remediation workers. A task carries ownership, findings, checks,
invocation evidence, lifecycle history and bounded root-cause context.

Task mutations use backend-validated transitions and generation/version checks.
The interface never treats local UI state as proof that a mutation succeeded.

## Run a governed tool

**Tools** exposes the declared catalog with provider, mode, risk and availability
filters. Inputs are generated from each tool's JSON Schema and converted through
the same typed frontend helpers used by tests.

Read tools execute through the shared core. Write or high-risk tools stop at the
single-use approval boundary; the browser requests approval, polls its state and
executes only after receiving an approved id bound to the exact input.

## Keep the human in charge

**Approvals** shows waiting and historical requests with live expiry countdowns.
Approve requires an explicit confirmation; deny is immediate. The same backend
decision path is used by Telegram inline buttons.

**MCP Clients** manages Telegram pairing, per-client identity, token rotation and
revocation. Newly issued or rotated bearer tokens are shown once. Conversion to
the privileged `task-worker.v1` capability requires a dedicated confirmation.

## Inspect delivery and attribution

**Metrics** separates technical reliability, reviewed routing accuracy, review
coverage, metering and attributed cost. **AI Delivery** presents normalized
conversation-route outcomes independently from Task Router quality.

**Activity** is the append-only, redacted audit trail. REST, Telegram, watchers
and MCP clients converge on the same actor/source model and link events back to
tools and tasks.

## What this workflow demonstrates

1. A watcher records a bounded observation and opens an incident.
2. Inbox surfaces the incident without becoming a second monitoring system.
3. The incident links to persistent task evidence and explicit ownership.
4. Tools collect normalized evidence through the shared execution core.
5. An infrastructure write waits for one exact operator approval.
6. Task, approval and execution events remain attributable in Activity.

Continue with [Architecture](./architecture.md) for the five-plane model or
[Security](./security.md) for the trust boundaries behind this workflow.
