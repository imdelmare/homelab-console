import {
  addTaskNote,
  assignWorkerTask,
  claimTask,
  claimTaskAsOperator,
  completeTaskAsOperator,
  createTask,
  dispatchTaskToFixer,
  fetchMcpClients,
  fetchTaskContext,
  fetchTaskDetail,
  fetchTasks,
  handoffTaskToClient,
  releaseTaskWithHandoff,
  setTaskStatus,
} from "../src/lib/api";
import { formatDateTime } from "../src/lib/format";
import { isMcpClientOnline, RELEASABLE_STATUSES, TASK_TRANSITIONS, taskStatusActionLabel } from "../src/lib/ui";
import type { McpClient, Task, TaskContext, TaskDetail } from "../src/lib/types";
import { button, element, replaceChildren } from "./dom";

export function mountTasks(target: HTMLElement, searchInput: HTMLInputElement, username = ""): () => void {
  let tasks: Task[] = [];
  let clients: McpClient[] = [];
  let detailTask: TaskDetail | null = null;
  let taskContext: TaskContext | null = null;
  let selectedId: string | null = null;
  let query = "";
  let filter = "open";
  let loading = true;
  let detailLoading = false;
  let actionBusy = "";
  let actionError = "";
  let errorMessage = "";
  let active = true;
  const refreshButton = button("Refresh", "quiet-button");
  const filters = element("div", { className: "filter-bar" });
  const summary = element("p", { className: "result-summary" });
  const list = element("div", { className: "record-list" });
  const detail = element("aside", { className: "record-detail", "aria-label": "Selected task" });

  async function runAction(name: string, taskId: string, action: () => Promise<unknown>): Promise<void> {
    actionBusy = name; actionError = ""; renderDetail();
    try {
      await action();
      await load(false);
      await selectTask(taskId);
    } catch (error) {
      actionError = error instanceof Error ? error.message : "Task action failed.";
    } finally {
      if (active) { actionBusy = ""; renderDetail(); }
    }
  }

  function renderTaskControls(task: TaskDetail): HTMLElement {
    const controls = element("section", { className: "task-controls" }, element("h3", {}, "Task controls"));
    const quick = element("div", { className: "task-quick-actions" });
    const actionButton = (label: string, name: string, action: () => Promise<unknown>) => {
      const control = button(actionBusy === name ? "Working…" : label, "quiet-button");
      control.disabled = Boolean(actionBusy);
      control.addEventListener("click", () => void runAction(name, task.id, action));
      return control;
    };
    if (task.status === "open") quick.append(actionButton("Claim as operator", "claim-self", () => claimTaskAsOperator(task.id, task.version)));
    if (task.status === "open") {
      for (const [label, agent] of [["Claude", "agent:claude"], ["Codex", "agent:codex"], ["Cline", "agent:cline"], ["OpenCode", "agent:opencode"]] as const) quick.append(actionButton(`Assign ${label}`, `assign-${label}`, () => claimTask(task.id, agent)));
    }
    if (task.status === "open" || task.assigned_agent === "agent:fixer") quick.append(actionButton(task.assigned_agent === "agent:fixer" ? "Retry Fixer" : "Assign Fixer", "fixer", async () => {
      const result = await dispatchTaskToFixer(task.id);
      if (!result.dispatch.ok) throw new Error(`Task assigned, Fixer launch failed: ${result.dispatch.message}`);
      return result.task;
    }));
    const workers = clients.filter((client) => isMcpClientOnline(client) && client.capabilities.includes("task-worker.v1") && client.principal_id.startsWith("agent:worker:"));
    if (task.status === "open" && workers.length) {
      const workerSelect = element("select", { className: "control-input", "aria-label": "Remediation worker" }, ...workers.map((client) => element("option", { value: client.id }, client.client_label || client.agent_id)));
      quick.append(workerSelect, actionButton("Assign worker", "assign-worker", () => assignWorkerTask(task.id, workerSelect.value, task.version)));
    }
    controls.append(quick);

    if (RELEASABLE_STATUSES.has(task.status)) {
      const handoff = element("textarea", { className: "control-input", rows: 3, placeholder: "Checks performed, evidence, and suggested next step." });
      const release = actionButton("Release with handoff", "release", () => releaseTaskWithHandoff(task.id, task.version, handoff.value.trim() || "Released by the operator without additional notes."));
      controls.append(element("label", { className: "control-field" }, element("span", {}, "Handoff summary"), handoff), release);
    }

    if (task.assigned_agent === `user:${username}` && RELEASABLE_STATUSES.has(task.status)) {
      const note = element("textarea", { className: "control-input", rows: 4, placeholder: "Checks performed, decisions, or client instructions." });
      const onlineClients = clients.filter((client) => isMcpClientOnline(client) && client.agent_id !== "worker" && !client.principal_id.startsWith("agent:worker:"));
      const targetClient = element("select", { className: "control-input", "aria-label": "Target MCP client" }, ...onlineClients.map((client) => element("option", { value: client.id }, `${client.agent_id} · ${client.client_label || client.id.slice(0, 8)}`)));
      const saveNote = actionButton("Save note", "save-note", async () => {
        if (!note.value.trim()) throw new Error("Write an operator note first.");
        return addTaskNote(task.id, note.value.trim());
      });
      const handoffClient = actionButton("Hand off to client", "handoff-client", async () => {
        if (!note.value.trim() || !targetClient.value) throw new Error("A note and target client are required.");
        return handoffTaskToClient(task.id, targetClient.value, note.value.trim(), task.version);
      });
      const complete = actionButton("Mark handled by me", "operator-complete", async () => {
        if (!note.value.trim()) throw new Error("Write an operator note first.");
        return completeTaskAsOperator(task.id, note.value.trim(), task.version);
      });
      controls.append(element("label", { className: "control-field" }, element("span", {}, "Operator diary"), note), element("div", { className: "task-quick-actions" }, saveNote, targetClient, handoffClient, complete));
    }

    const transitions = (TASK_TRANSITIONS[task.status] ?? []).filter((status) => status !== "claimed");
    if (transitions.length) controls.append(element("div", { className: "task-quick-actions lifecycle-actions" }, ...transitions.map((status) => actionButton(taskStatusActionLabel(status), `status-${status}`, () => setTaskStatus(task.id, status, task.version)))));
    if (actionError) controls.append(element("p", { className: "error-banner", role: "alert" }, actionError));
    return controls;
  }

  function isOpen(task: Task): boolean { return !["completed", "cancelled"].includes(task.status); }

  function renderDetail(): void {
    if (detailLoading) { replaceChildren(detail, element("div", { className: "loading-state" }, "Loading task context")); return; }
    if (!detailTask || detailTask.id !== selectedId) {
      replaceChildren(detail, element("p", { className: "detail-index" }, "TASK CONTEXT"), element("h2", {}, "Select a task"), element("p", { className: "detail-empty" }, "Open a task to inspect its goal, findings, checklist, and recent activity."));
      return;
    }
    const task = detailTask;
    const facts = element("dl", { className: "detail-facts" });
    for (const [label, value] of [["Status", task.status], ["Owner", task.assigned_agent || "Unassigned"], ["Updated", formatDateTime(task.last_activity_at)], ["Findings", String(task.findings.length)], ["Checks", String(task.checks.length)]]) facts.append(element("div", {}, element("dt", {}, label), element("dd", {}, value)));
    const findings = task.findings.filter((finding) => !finding.resolved_at).slice(0, 3);
    replaceChildren(detail,
      element("button", { className: "detail-close", type: "button", "aria-label": "Close details" }, "×"),
      element("p", { className: "item-kind" }, task.status), element("h2", {}, task.title),
      element("p", { className: "detail-description" }, task.summary || task.goal), facts,
      taskContext ? element("div", { className: "task-context" }, element("h3", {}, "Investigation context"), element("p", {}, taskContext.brief || taskContext.recommended_next_step), element("small", {}, `${taskContext.recommended_tools.length} recommended tools · budget ${taskContext.budget.max_tool_calls} calls / ${taskContext.budget.max_minutes} min`)) : null,
      findings.length ? element("div", { className: "detail-notes" }, element("h3", {}, "Open findings"), ...findings.map((finding) => element("p", {}, element("strong", {}, finding.title), finding.description))) : null,
      renderTaskControls(task),
    );
    detail.querySelector<HTMLButtonElement>(".detail-close")?.addEventListener("click", () => { selectedId = null; detailTask = null; render(); });
  }

  function renderFilters(): void {
    replaceChildren(filters, ...(["open", "completed", "all"] as const).map((name) => {
      const control = button(name[0].toUpperCase() + name.slice(1), `filter-button${filter === name ? " filter-button--active" : ""}`);
      control.addEventListener("click", () => { filter = name; render(); }); return control;
    }));
  }

  function render(): void {
    renderFilters();
    if (loading) { summary.textContent = "Reading task ledger…"; replaceChildren(list, element("div", { className: "loading-state" }, "Loading tasks")); renderDetail(); return; }
    if (errorMessage) { summary.textContent = "Task ledger unavailable"; replaceChildren(list, element("p", { className: "error-banner", role: "alert" }, errorMessage)); renderDetail(); return; }
    const normalized = query.trim().toLowerCase();
    const visible = tasks.filter((task) => (filter === "all" || (filter === "open" ? isOpen(task) : task.status === "completed")) && (!normalized || [task.title, task.goal, task.status, task.assigned_agent, task.id].join(" ").toLowerCase().includes(normalized)));
    summary.textContent = `${tasks.filter(isOpen).length} open · ${tasks.filter((task) => task.status === "completed").length} completed in the latest ${tasks.length}`;
    replaceChildren(list, ...(visible.length ? visible.map((task, index) => {
      const row = button(task.title, `record-row${selectedId === task.id ? " record-row--selected" : ""}`);
      replaceChildren(row, element("span", { className: `state-dot state-dot--${task.status === "blocked" ? "critical" : isOpen(task) ? "warning" : "healthy"}` }), element("span", { className: "row-index" }, String(index + 1).padStart(2, "0")), element("span", { className: "record-copy" }, element("strong", {}, task.title), element("small", {}, task.goal)), element("span", { className: "state-label" }, task.status), element("span", { className: "record-meta" }, task.assigned_agent || "Unassigned"), element("span", { className: "row-arrow" }, "↗"));
      row.addEventListener("click", () => {
        window.history.replaceState(null, "", `#tasks/${encodeURIComponent(task.id)}`);
        void selectTask(task.id);
      }); return row;
    }) : [element("p", { className: "empty-state" }, "No tasks match this view.")]));
    renderDetail();
  }

  async function selectTask(taskId: string): Promise<void> {
    selectedId = taskId; detailTask = null; taskContext = null; detailLoading = true; render();
    try {
      const [detailResult, contextResult] = await Promise.allSettled([fetchTaskDetail(taskId), fetchTaskContext(taskId)]);
      if (active && selectedId === taskId) {
        if (detailResult.status === "fulfilled") detailTask = detailResult.value;
        if (contextResult.status === "fulfilled") taskContext = contextResult.value;
      }
    }
    catch { /* Keep the list usable when a task is removed concurrently. */ }
    finally { if (active && selectedId === taskId) { detailLoading = false; render(); } }
  }
  async function load(showLoading = true): Promise<void> {
    if (showLoading) loading = true;
    errorMessage = ""; refreshButton.disabled = true; render();
    try {
      const [taskResult, clientResult] = await Promise.allSettled([fetchTasks({ limit: 100 }), fetchMcpClients()]);
      if (taskResult.status === "rejected") throw taskResult.reason;
      tasks = taskResult.value;
      clients = clientResult.status === "fulfilled" ? clientResult.value : [];
    }
    catch (error) { errorMessage = error instanceof Error ? error.message : "Unable to load tasks."; }
    finally { if (active) { loading = false; refreshButton.disabled = false; render(); } }
  }
  const createForm = element("form", { className: "create-task-form" });
  const createTitle = element("input", { className: "control-input", required: true, placeholder: "Task title", "aria-label": "Task title" });
  const createGoal = element("input", { className: "control-input", required: true, placeholder: "Operational goal", "aria-label": "Task goal" });
  const createButton = element("button", { className: "primary-action", type: "submit" }, "Create task");
  createForm.append(createTitle, createGoal, createButton);
  createForm.addEventListener("submit", async (event) => {
    event.preventDefault(); createButton.disabled = true; actionError = "";
    try { const created = await createTask(createTitle.value.trim(), createGoal.value.trim()); createForm.reset(); await load(false); window.history.replaceState(null, "", `#tasks/${encodeURIComponent(created.id)}`); await selectTask(created.id); }
    catch (error) { actionError = error instanceof Error ? error.message : "Unable to create task."; renderDetail(); }
    finally { createButton.disabled = false; }
  });
  const handleSearch = () => { query = searchInput.value; render(); };
  searchInput.addEventListener("input", handleSearch); refreshButton.addEventListener("click", () => void load());
  replaceChildren(target, element("section", { className: "inbox-page", "aria-labelledby": "tasks-heading" }, element("header", { className: "inbox-heading compact-heading" }, element("div", {}, element("p", { className: "eyebrow" }, "Work ledger"), element("h1", { id: "tasks-heading" }, "Tasks"), element("p", { className: "inbox-intro" }, "Durable operational work, ordered without ceremony.")), refreshButton), createForm, filters, summary, element("div", { className: "inbox-workspace" }, list, detail)));
  void load().then(() => {
    const taskId = window.location.hash.startsWith("#tasks/") ? decodeURIComponent(window.location.hash.slice("#tasks/".length)) : "";
    if (taskId) void selectTask(taskId);
  });
  return () => { active = false; searchInput.removeEventListener("input", handleSearch); };
}
