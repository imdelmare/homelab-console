import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Avatar, Button, GroupBox as Fieldset, TextInput as Input, TextInput as TextArea } from "react95";
import { useQueryClient } from "@tanstack/react-query";
import {
  addTaskNote,
  claimTask,
  claimTaskAsOperator,
  completeTaskAsOperator,
  createTask,
  dispatchTaskToFixer,
  fetchTaskContext,
  fetchTaskDetail,
  fetchTasks,
  fetchMcpClients,
  handoffTaskToClient,
  releaseTaskWithHandoff,
  setTaskStatus,
} from "../lib/api";
import { formatDateTime } from "../lib/format";
import {
  RELEASABLE_STATUSES,
  TASK_TRANSITIONS,
  describeError,
  isRecord,
  shortId,
  statusClass,
  taskAgent,
  taskInitialRouting,
  taskRouterLabel,
  taskStatusActionLabel,
  taskStatusLabel,
  text,
  isMcpClientOnline,
} from "../lib/ui";
import { usePanelQuery } from "../lib/usePanelQuery";
import { StatusLed } from "../components/StatusLed";
import { LoadingIndicator } from "../components/LoadingIndicator";
import { PanelLoadingScreen } from "../components/PanelLoadingScreen";
import { EmptyState } from "../components/EmptyState";
import { StatusBadge } from "../components/StatusBadge";
import { SelectControl } from "../components/SelectControl";

function taskTone(status: string): "success" | "warning" | "danger" | "neutral" {
  if (status === "completed" || status === "resolved") return "success";
  if (status === "blocked" || status === "cancelled") return "danger";
  if (status === "claimed" || status === "investigating" || status === "waiting_operator") return "warning";
  return "neutral";
}

function TaskAgentIdentity({ label }: { label: string }) {
  const normalized = label.replace(/^agent:/, "");
  const initials = normalized === "Unassigned" ? "—" : normalized.slice(0, 2).toUpperCase();
  const unassigned = normalized === "Unassigned";
  return (
    <span className={`task-agent-identity ${unassigned ? "task-agent-unassigned" : ""}`} title={`Owner: ${normalized}`}>
      <Avatar className="agent-avatar task-agent-avatar" size="34px" aria-hidden="true">{initials}</Avatar>
      <span className="task-agent-label">{normalized}</span>
    </span>
  );
}
import { ResultTable } from "./shared";
import { KeyValueGrid } from "./shared";
import type {
  Provider,
  Task,
  TaskCheck,
  TaskContext,
  TaskEvent,
  TaskFinding,
  TaskInvocation,
} from "../lib/types";

export function TasksApp({ requestedTaskId, username }: { requestedTaskId?: string | null; username: string }) {
  const queryClient = useQueryClient();
  const tasksQuery = usePanelQuery(["tasks", "all"], () => fetchTasks());
  const clientsQuery = usePanelQuery(["mcp-clients"], fetchMcpClients);
  const tasks = tasksQuery.data ?? [];
  const [actionError, setActionError] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [creating, setCreating] = useState(false);
  // The prop seeds a newly opened window; the event subscription below also
  // handles requests while the Tasks window is already mounted.
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(requestedTaskId ?? null);
  const [taskAction, setTaskAction] = useState<string | null>(null);
  const [handoffSummary, setHandoffSummary] = useState("");
  const [operatorNote, setOperatorNote] = useState("");
  const [handoffClientId, setHandoffClientId] = useState("");
  const [taskStatusFilter, setTaskStatusFilter] = useState("all");

  const detailQuery = usePanelQuery(
    ["task-detail", selectedTaskId],
    async () => {
      const [detailPayload, contextPayload] = await Promise.all([
        fetchTaskDetail(selectedTaskId!),
        fetchTaskContext(selectedTaskId!),
      ]);
      return { detail: detailPayload, context: contextPayload };
    },
    { enabled: Boolean(selectedTaskId) },
  );
  const detail = detailQuery.data?.detail ?? null;
  const context = detailQuery.data?.context ?? null;
  const detailLoading = Boolean(selectedTaskId) && detailQuery.loadState === "loading";
  const onlineClients = (clientsQuery.data ?? []).filter(isMcpClientOnline);
  const effectiveHandoffClientId = onlineClients.some((client) => client.id === handoffClientId)
    ? handoffClientId
    : onlineClients[0]?.id ?? "";

  useEffect(() => {
    function handleOpenTask(event: Event) {
      const taskId = (event as CustomEvent<{ taskId?: string }>).detail?.taskId;
      if (taskId) {
        setOperatorNote("");
        setSelectedTaskId(taskId);
      }
    }

    window.addEventListener("homelab:open-task", handleOpenTask);
    return () => window.removeEventListener("homelab:open-task", handleOpenTask);
  }, []);

  function invalidateTasks(taskId: string | null = selectedTaskId) {
    void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    void queryClient.invalidateQueries({ queryKey: ["audit"] });
    if (taskId) {
      void queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] });
    }
  }

  async function submitCreate(event: FormEvent) {
    event.preventDefault();
    setCreating(true);
    setActionError(null);
    try {
      await createTask(title, goal);
      setTitle("");
      setGoal("");
      invalidateTasks(null);
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setCreating(false);
    }
  }

  async function runTaskAction(action: string, fn: () => Promise<Task>) {
    if (!detail) {
      return;
    }
    setTaskAction(action);
    setActionError(null);
    try {
      const task = await fn();
      if (action === "release") {
        setHandoffSummary("");
      }
      if (action === "operator-handoff" || action === "operator-complete") {
        setOperatorNote("");
      }
      setSelectedTaskId(task.id);
      invalidateTasks(task.id);
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setTaskAction(null);
    }
  }

  async function assignAndLaunchFixer() {
    if (!detail) return;
    await runTaskAction("dispatch-fixer", async () => {
      const result = await dispatchTaskToFixer(detail.id);
      if (!result.dispatch.ok) {
        setActionError(`Task assigned, Fixer launch failed: ${result.dispatch.message}`);
      }
      return result.task;
    });
  }

  async function saveOperatorNote() {
    if (!detail || !operatorNote.trim()) return;
    setTaskAction("save-note");
    setActionError(null);
    try {
      await addTaskNote(detail.id, operatorNote.trim());
      setOperatorNote("");
      invalidateTasks(detail.id);
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setTaskAction(null);
    }
  }

  const pendingChecks = detail?.checks.filter((check) => check.status === "pending") ?? [];
  const completedChecks = detail?.checks.filter((check) => check.status !== "pending") ?? [];
  const activeFindings = detail?.findings.filter((finding) => !finding.resolved_at) ?? [];
  const availableTransitions = detail
    ? (TASK_TRANSITIONS[detail.status] ?? []).filter((status) => status !== "claimed")
    : [];
  const operatorOwnsTask = detail?.assigned_agent === `user:${username}`;
  const shownError = actionError ?? tasksQuery.errorMessage ?? detailQuery.errorMessage;
  const taskFilterOptions: Array<[string, string, (task: Task) => boolean]> = [
    ["all", "All", () => true],
    ["open", "Open", (task) => task.status === "open"],
    ["mine", "Mine", (task) => task.assigned_agent === `user:${username}`],
    ["watcher", "Watcher", (task) => task.source === "watcher"],
    ["provider", "Provider", (task) => task.source === "provider"],
    ["claude", "Claude", (task) => task.assigned_agent === "agent:claude"],
    ["fixer", "Fixer", (task) => task.assigned_agent === "agent:fixer"],
    ["codex", "Codex", (task) => task.assigned_agent === "agent:codex"],
    ["cline", "Cline", (task) => task.assigned_agent === "agent:cline"],
    ["opencode", "OpenCode", (task) => task.assigned_agent === "agent:opencode"],
    ["blocked", "Blocked", (task) => task.status === "blocked"],
    ["completed", "Completed", (task) => task.status === "completed"],
  ];
  const primaryTaskFilters = new Set(["open", "mine", "blocked", "completed"]);
  const taskCounts = Object.fromEntries(
    taskFilterOptions.map(([key, , predicate]) => [key, tasks.filter(predicate).length]),
  );
  const visibleTasks = tasks.filter(taskFilterOptions.find(([key]) => key === taskStatusFilter)?.[2] ?? (() => true));

  if (tasksQuery.loadState === "loading") {
    return <PanelLoadingScreen label="Loading tasks…" />;
  }

  return (
    <div className="panel-app tasks-app">
      <form className="task-create-form" onSubmit={submitCreate}>
        <Fieldset label="Create task" className="task-create-fields">
        <div className="field-row-stacked">
          <label htmlFor="task-title">Title</label>
          <Input
            id="task-title"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            disabled={creating}
          />
        </div>
        <div className="field-row-stacked">
          <label htmlFor="task-goal">Goal</label>
          <Input
            id="task-goal"
            value={goal}
            onChange={(event) => setGoal(event.target.value)}
            disabled={creating}
          />
        </div>
        <div className="dialog-actions">
          <Button type="submit" disabled={creating || !title || !goal}>
            Create task
          </Button>
          <Button type="button" onClick={tasksQuery.refresh} disabled={tasksQuery.isFetching}>
            Refresh
          </Button>
        </div>
        </Fieldset>
      </form>
      <div className="task-filterbar">
        {taskFilterOptions.filter(([status]) => primaryTaskFilters.has(status)).map(([status, label]) => (
          <Button
            type="button"
            key={status}
            className={taskStatusFilter === status ? "pressed" : ""}
            onClick={() => setTaskStatusFilter(status)}
          >
            {label} <span>{taskCounts[status] ?? 0}</span>
          </Button>
        ))}
        <details className="task-extra-filters">
          <summary>More filters</summary>
          <div>
            {taskFilterOptions.filter(([status]) => !primaryTaskFilters.has(status)).map(([status, label]) => (
              <Button type="button" key={status} className={taskStatusFilter === status ? "pressed" : ""} onClick={() => setTaskStatusFilter(status)}>{label} <span>{taskCounts[status] ?? 0}</span></Button>
            ))}
          </div>
        </details>
      </div>
      {shownError && <p className="login-error">{shownError}</p>}
      <div className="task-layout">
        <div className="item-list task-list">
          {visibleTasks.map((task) => (
            <article
              className={`item-row item-row-stacked task-row task-row-${statusClass(task.status)} ${task.id === selectedTaskId ? "task-row-selected" : ""}`}
              key={task.id}
              onClick={() => { setOperatorNote(""); setSelectedTaskId(task.id); }}
            >
              <div className="task-row-heading">
                <StatusBadge label={taskStatusLabel(task.status)} tone={taskTone(task.status)} />
                {task.resolution_label === "human_handled" && <StatusBadge label="Handled by human" tone="success" />}
                <strong>{task.title}</strong>
              </div>
              <p className="task-row-goal clamp-two-lines">{task.goal}</p>
              <div className="task-row-metadata metadata-line">
                <TaskAgentIdentity label={taskAgent(task)} />
                <span>{task.source || "task"}</span>
                <span>Updated {formatDateTime(task.last_activity_at ?? task.updated_at)}</span>
              </div>
              <div className="task-row-footer">
                {taskRouterLabel(task) && (
                  <mark className={`task-router-badge task-router-${task.router_status}`}>{taskRouterLabel(task)}</mark>
                )}
                <Button type="button" onClick={() => { setOperatorNote(""); setSelectedTaskId(task.id); }}>Open task</Button>
              </div>
            </article>
          ))}
          {tasksQuery.loadState === "ready" && visibleTasks.length === 0 && (
            <EmptyState
              title={tasks.length === 0 ? "No tasks returned" : "No tasks in this filter"}
              description={tasks.length === 0
                ? "The task registry is empty. Refresh to synchronize this view with dashboard counters."
                : `${tasks.length} tasks exist, but none match the selected filter.`}
              actionLabel="Refresh task"
              onAction={tasksQuery.refresh}
            />
          )}
        </div>
        {selectedTaskId && (
          <div className="task-detail sunken-panel">
            {detailLoading && <LoadingIndicator label="Loading task details…" size={24} />}
            {!detailLoading && detail && (
              <>
                <div className="task-detail-head">
                  <div>
                    <h3>{detail.title}</h3>
                    <span>{detail.goal}</span>
                  </div>
                  <div className="task-row-badges">
                    <mark className={`task-status task-status-${statusClass(detail.status)}`}>{taskStatusLabel(detail.status)}</mark>
                    {detail.resolution_label === "human_handled" && <StatusBadge label="Handled by human" tone="success" />}
                  </div>
                </div>

                <KeyValueGrid
                  items={[
                    ["Owner", taskAgent(detail)],
                    ["Version", detail.version],
                    ["Updated", formatDateTime(detail.last_activity_at ?? detail.updated_at)],
                    ["Completed", detail.completed_at ? formatDateTime(detail.completed_at) : "—"],
                  ]}
                />

                <TaskRoutingPanel events={detail.events} />

                {context && <TaskRcaPanel context={context} />}

                <div className="task-control-groups">
                  <div className="task-action-group">
                        <strong>Manual management</strong>
                    <div className="task-actions">
                      {detail.status === "open" && (
                        <Button
                          disabled={Boolean(taskAction)}
                          onClick={() => runTaskAction("claim-self", () => claimTaskAsOperator(detail.id, detail.version))}
                        >
                          Claim
                        </Button>
                      )}
                      {RELEASABLE_STATUSES.has(detail.status) && (
                        <Button
                          disabled={Boolean(taskAction)}
                          onClick={() =>
                            runTaskAction("release", () =>
                              releaseTaskWithHandoff(
                                detail.id,
                                detail.version,
                                handoffSummary.trim() || "Released by the operator without additional notes.",
                              ),
                            )
                          }
                        >
                          Release
                        </Button>
                      )}
                    </div>
                  </div>
                  {(detail.status === "open" || detail.assigned_agent === "agent:fixer") && (
                    <details className="task-agent-assignment">
                      <summary>Assign to an agent</summary>
                      <div className="task-actions">
                        <Button disabled={Boolean(taskAction) || detail.status !== "open"} onClick={() => runTaskAction("assign-claude", () => claimTask(detail.id, "agent:claude"))}>Claude</Button>
                        <Button
                          disabled={Boolean(taskAction) || !(detail.status === "open" || (detail.assigned_agent === "agent:fixer" && RELEASABLE_STATUSES.has(detail.status)))}
                          onClick={assignAndLaunchFixer}
                          title="Assign the task to Fixer and start OpenCode"
                        >
                          {detail.assigned_agent === "agent:fixer" ? "Try again Fixer" : "Fixer"}
                        </Button>
                        <Button disabled={Boolean(taskAction) || detail.status !== "open"} onClick={() => runTaskAction("assign-codex", () => claimTask(detail.id, "agent:codex"))}>Codex</Button>
                        <Button disabled={Boolean(taskAction) || detail.status !== "open"} onClick={() => runTaskAction("assign-cline", () => claimTask(detail.id, "agent:cline"))}>Cline</Button>
                        <Button disabled={Boolean(taskAction) || detail.status !== "open"} onClick={() => runTaskAction("assign-opencode", () => claimTask(detail.id, "agent:opencode"))}>OpenCode</Button>
                      </div>
                    </details>
                  )}
                </div>

                {operatorOwnsTask && RELEASABLE_STATUSES.has(detail.status) && (
                  <section className="task-operator-diary">
                    <div className="task-operator-diary-head">
                      <div>
                        <strong>Operator diary</strong>
                        <span>Notes are append-only, redacted, and included in the context passed to clients.</span>
                      </div>
                      <StatusBadge label="Handled by human" tone="success" />
                    </div>
                    <label className="field-row-stacked">
                      <span>Operator note</span>
                      <TextArea
                        multiline
                        value={operatorNote}
                        onChange={(event) => setOperatorNote(event.target.value)}
                        placeholder="Checks performed, decisions made, or instructions for the client."
                        rows={4}
                      />
                    </label>
                    <div className="task-operator-actions">
                      <Button type="button" disabled={Boolean(taskAction) || !operatorNote.trim()} onClick={saveOperatorNote}>
                        Save note
                      </Button>
                      <SelectControl
                        aria-label="Target MCP client"
                        value={effectiveHandoffClientId}
                        disabled={Boolean(taskAction) || onlineClients.length === 0}
                        onChange={(event) => setHandoffClientId(event.target.value)}
                      >
                        {onlineClients.length === 0 && <option value="">No clients online</option>}
                        {onlineClients.map((client) => (
                          <option value={client.id} key={client.id}>
                            {client.agent_id} · {client.client_label || client.host_fingerprint || client.id.slice(0, 8)}
                          </option>
                        ))}
                      </SelectControl>
                      <Button
                        type="button"
                        disabled={Boolean(taskAction) || !operatorNote.trim() || !effectiveHandoffClientId}
                        onClick={() => runTaskAction("operator-handoff", () => handoffTaskToClient(detail.id, effectiveHandoffClientId, operatorNote.trim(), detail.version))}
                      >
                        Hand off task + note
                      </Button>
                      <Button
                        type="button"
                        disabled={Boolean(taskAction) || !operatorNote.trim()}
                        onClick={() => runTaskAction("operator-complete", () => completeTaskAsOperator(detail.id, operatorNote.trim(), detail.version))}
                      >
                        Mark as handled by me
                      </Button>
                    </div>
                    {clientsQuery.loadState === "error" && <p className="login-error">MCP clients unavailable: {clientsQuery.errorMessage}</p>}
                  </section>
                )}

                {RELEASABLE_STATUSES.has(detail.status) && (
                  <label className="field-row-stacked task-handoff-box">
                    <span>Handoff summary</span>
                    <TextArea
                      multiline
                      value={handoffSummary}
                      onChange={(event) => setHandoffSummary(event.target.value)}
                      placeholder="Checks performed, current evidence, and suggested next step."
                      rows={3}
                    />
                  </label>
                )}

                {availableTransitions.length > 0 && (
                  <div className="task-transition-panel">
                    <strong>Lifecycle</strong>
                    <div className="task-actions task-status-transition">
                      {availableTransitions.map((status) => (
                        <Button
                          type="button"
                          key={status}
                          disabled={Boolean(taskAction)}
                          onClick={() =>
                            runTaskAction(`set-${status}`, () => setTaskStatus(detail.id, status, detail.version))
                          }
                        >
                          {status === "investigating" && ["blocked", "waiting_operator"].includes(detail.status)
                            ? "Resume investigation"
                            : taskStatusActionLabel(status)}
                        </Button>
                      ))}
                    </div>
                    {detail.status === "claimed" && (
                      <p>
                        Start the investigation before completing, blocking, or putting the task on hold.
                      </p>
                    )}
                  </div>
                )}

                <section className="task-section">
                  <h4>Summary</h4>
                  <p>{detail.summary || "No summary available."}</p>
                </section>

                <h4>Findings</h4>
                <TaskFindingList findings={activeFindings} />

                <h4>Pending checks</h4>
                <TaskCheckList checks={pendingChecks} />

                <h4>Completed checks</h4>
                <TaskCheckList checks={completedChecks} />

                <h4>Invocations</h4>
                <TaskInvocationList invocations={detail.invocations} />

                <h4>Events</h4>
                <TaskEventList events={detail.events} />
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function TaskRoutingPanel({ events }: { events: TaskEvent[] }) {
  const routing = taskInitialRouting(events);
  if (!routing) return null;

  const statusLabel = routing.status === "routed" ? "Assignment completed" : "Automatic assignment failed";
  const confidence = routing.confidence === null ? "—" : `${Math.round(routing.confidence * 100)}%`;
  return (
    <section className={`task-section task-routing-panel task-routing-${routing.status}`}>
      <div className="task-routing-head">
        <div>
          <h4>Initial assignment</h4>
          <span>{formatDateTime(routing.createdAt)}</span>
        </div>
        <mark className={`task-router-badge task-router-${routing.status}`}>{statusLabel}</mark>
      </div>
      {routing.status === "routed" ? (
        <>
          <KeyValueGrid
            items={[
              ["Category", routing.category],
              ["Priority", routing.priority],
              ["Severity", routing.severity],
              ["Suggested owner", routing.suggestedOwner],
              ["Action", routing.action],
              ["Confidence", confidence],
              ["Runbook", routing.runbook || "—"],
              ["Model", routing.model || "—"],
            ]}
          />
          {routing.summary && <p>{routing.summary}</p>}
          {routing.labels.length > 0 && (
            <div className="task-routing-labels">
              {routing.labels.map((label) => (
                <span key={label}>{label}</span>
              ))}
            </div>
          )}
        </>
      ) : (
        <p>
          {routing.failureMessage}
          {routing.failureReason ? ` Reason: ${routing.failureReason.replace(/_/g, " ")}.` : ""}
        </p>
      )}
    </section>
  );
}

function TaskRcaPanel({ context }: { context: TaskContext }) {
  const incident = context.incident;
  const incidentLabel = incident?.type ?? "task_handoff";
  const providerIds = context.provider_ids ?? [];
  const providerStates = context.provider_states ?? [];
  const recommendedTools = context.recommended_tools ?? [];
  const budget = context.budget ?? { max_tool_calls: 0, max_minutes: 0 };
  const providerText = providerIds.length > 0 ? providerIds.join(" → ") : "No provider identified";
  return (
    <section className="task-section rca-panel">
      <div className="rca-panel-head">
        <div>
          <h4>Root cause analysis</h4>
          <span>{incidentLabel}</span>
        </div>
        <mark className="summary-badge summary-badge-info">{context.recommended_next_step || "Verify context"}</mark>
      </div>
      <p>{context.brief}</p>
      <div className="rca-chip-row">
        <span>Provider: {providerText}</span>
        <span>
          Budget: {budget.max_tool_calls} calls / {budget.max_minutes} min
        </span>
      </div>
      {providerStates.length > 0 && (
        <div className="rca-provider-grid">
          {providerStates.map((provider) => (
            <span key={provider.provider_id}>
              <StatusLed status={provider.status as Provider["status"]} />
              {provider.display_name || provider.provider_id}
            </span>
          ))}
        </div>
      )}
      {recommendedTools.length > 0 && (
        <div className="rca-tool-list">
          {recommendedTools.map((tool, index) => (
            <article key={tool.tool_id}>
              <strong>
                {index + 1}. {tool.name || tool.tool_id}
              </strong>
              <span>{tool.reason}</span>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function TaskFindingList({ findings }: { findings: TaskFinding[] }) {
  if (findings.length === 0) {
    return <p>No active findings.</p>;
  }
  return (
    <div className="task-card-list">
      {findings.map((finding) => (
        <article className="task-card" key={finding.id}>
          <div className="task-card-head">
            <strong>{finding.title}</strong>
            <mark className={`finding-severity finding-${finding.severity}`}>{finding.severity}</mark>
          </div>
          <p>{finding.description}</p>
          <small>
            {finding.created_by} · {formatDateTime(finding.created_at)}
            {finding.tool_invocation_id ? ` · ${shortId(finding.tool_invocation_id)}` : ""}
          </small>
        </article>
      ))}
    </div>
  );
}

function TaskCheckList({ checks }: { checks: TaskCheck[] }) {
  if (checks.length === 0) {
    return <p>None.</p>;
  }
  return (
    <div className="task-card-list">
      {checks.map((check) => (
        <article className="task-card task-check-card" key={check.id}>
          <div>
            <strong>{check.description}</strong>
            {check.skip_reason && <span>{check.skip_reason}</span>}
          </div>
          <mark className={`check-status check-${check.status}`}>{check.status}</mark>
        </article>
      ))}
    </div>
  );
}

function TaskInvocationList({ invocations }: { invocations: TaskInvocation[] }) {
  if (invocations.length === 0) {
    return <p>No linked invocations.</p>;
  }
  return (
    <ResultTable
      columns={[
        { key: "tool_id", label: "Tool" },
        { key: "status", label: "Status" },
        { key: "error_code", label: "Error" },
        { key: "duration_ms", label: "ms" },
        { key: "started_at", label: "Started", render: (row) => formatDateTime(text(row.started_at, "")) },
      ]}
      rows={invocations}
    />
  );
}

function TaskEventList({ events }: { events: TaskEvent[] }) {
  if (events.length === 0) {
    return <p>No events.</p>;
  }
  return (
    <div className="task-timeline">
      {events.map((event) => (
        <article key={event.id}>
          <strong>{event.kind}</strong>
          <span>{formatDateTime(event.created_at)}</span>
          <p>{taskEventSummary(event)}</p>
          {Object.keys(event.payload).length > 0 && <pre>{JSON.stringify(event.payload, null, 2)}</pre>}
        </article>
      ))}
    </div>
  );
}

function taskEventSummary(event: TaskEvent): string {
  const payload = event.payload;
  if (event.kind === "task.released") {
    return `Released by ${text(payload.from_agent)} from ${text(payload.from_status)}. Handoff: ${text(payload.handoff_summary, "none")}`;
  }
  if (event.kind === "task.status_changed") {
    return `Status changed from ${text(payload.from)} to ${text(payload.to)}.`;
  }
  if (event.kind === "task.claimed") {
    return `Claimed by ${text(payload.assigned_agent)}.`;
  }
  if (event.kind === "task.provider_context") {
    return `Provider context captured for ${text(payload.provider_id)}: ${text(payload.status)}.`;
  }
  if (event.kind === "task.router_decision") {
    const decision = isRecord(payload.decision) ? payload.decision : {};
    return `Router: ${text(decision.category, "unknown")} · ${text(decision.priority, "medium")} · owner ${text(decision.suggested_owner, "operator")}.`;
  }
  if (event.kind === "task.router_failed") {
    const error = isRecord(payload.error) ? payload.error : {};
    return `Router failed: ${text(error.message, "no decision stored")}.`;
  }
  if (event.kind === "watcher.auto_investigate") {
    return `Auto-investigate ${text(payload.outcome, "evaluated")}: ${text(payload.reason, "policy decision recorded")}.`;
  }
  if (event.kind === "watcher.task.auto_completed") {
    return "The watcher automatically completed the task after the alert was resolved, without assigning an agent.";
  }
  if (event.kind === "watcher.incident.resolve_handled") {
    return "Operator marked the linked watcher incident as already handled.";
  }
  if (event.kind === "task.note_added") {
    return text(payload.note, "Note added.");
  }
  if (event.kind === "task.operator_handoff") {
    return `Human handoff to ${text(payload.to_agent)} through ${text(payload.client_label, "MCP client")}. Note: ${text(payload.note)}`;
  }
  if (event.kind === "task.operator_completed") {
    return `Handled by human (${text(payload.handled_by)}). Final note: ${text(payload.note)}`;
  }
  return "Event recorded in the task audit timeline.";
}
