import { fetchApprovals, fetchIncidents, fetchProviders, fetchTasks } from "../src/lib/api";
import { formatDateTime } from "../src/lib/format";
import type { Approval, Incident, Task } from "../src/lib/types";
import { button, element, replaceChildren } from "./dom";

export type InboxKind = "approval" | "incident" | "task";
export type InboxPriority = "critical" | "action" | "routine";

export type InboxItem = {
  id: string;
  kind: InboxKind;
  priority: InboxPriority;
  title: string;
  description: string;
  source: string;
  timestamp: string;
  facts: Array<[string, string]>;
};

const PRIORITY_ORDER: Record<InboxPriority, number> = { critical: 0, action: 1, routine: 2 };

export function buildInboxItems(approvals: Approval[], incidents: Incident[], tasks: Task[]): InboxItem[] {
  const items: InboxItem[] = [
    ...approvals
      .filter((approval) => approval.status === "pending")
      .map((approval): InboxItem => ({
        id: `approval:${approval.id}`,
        kind: "approval",
        priority: "action",
        title: approval.action || approval.tool_id,
        description: `Requested by ${approval.requested_by}. This approval is valid for one exact execution.`,
        source: approval.tool_id,
        timestamp: approval.created_at,
        facts: [["Request", approval.id], ["Expires", formatDateTime(approval.expires_at)], ["Task", approval.task_id ?? "None"]],
      })),
    ...incidents.map((incident): InboxItem => ({
      id: `incident:${incident.id}`,
      kind: "incident",
      priority: incident.severity === "critical" ? "critical" : incident.severity === "warning" ? "action" : "routine",
      title: incident.title,
      description: incident.description,
      source: incident.provider_id || incident.watcher_id,
      timestamp: incident.last_seen_at,
      facts: [["Severity", incident.severity], ["Occurrences", String(incident.occurrences)], ["Task", incident.task_id ?? "None"]],
    })),
    ...tasks.map((task): InboxItem => ({
      id: `task:${task.id}`,
      kind: "task",
      priority: task.status === "blocked" ? "critical" : task.assigned_agent ? "routine" : "action",
      title: task.title,
      description: task.goal,
      source: task.assigned_agent || "Unassigned",
      timestamp: task.last_activity_at || task.updated_at,
      facts: [["Status", task.status], ["Owner", task.assigned_agent || "Unassigned"], ["Task", task.id]],
    })),
  ];

  return items.sort((left, right) => {
    const priorityDifference = PRIORITY_ORDER[left.priority] - PRIORITY_ORDER[right.priority];
    if (priorityDifference !== 0) return priorityDifference;
    return Date.parse(right.timestamp) - Date.parse(left.timestamp);
  });
}

function matches(item: InboxItem, query: string, filter: string): boolean {
  if (filter !== "all" && item.kind !== filter) return false;
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  return [item.title, item.description, item.source, item.kind, ...item.facts.flat()]
    .join(" ")
    .toLowerCase()
    .includes(normalized);
}

function itemLabel(kind: InboxKind): string {
  return { approval: "Approval", incident: "Incident", task: "Task" }[kind];
}

export function mountInbox(target: HTMLElement, searchInput: HTMLInputElement): () => void {
  let items: InboxItem[] = [];
  let systemTotal = 0;
  let healthySystems = 0;
  let selectedId: string | null = null;
  let filter = "all";
  let query = "";
  let loading = true;
  let errorMessage = "";
  let active = true;
  let loadInFlight = false;

  const refreshButton = button("Refresh", "quiet-button");
  const list = element("div", { className: "inbox-list", "aria-live": "polite" });
  const detail = element("aside", { className: "inbox-detail", "aria-label": "Selected inbox item" });
  const resultSummary = element("p", { className: "result-summary" });
  const filterBar = element("div", { className: "filter-bar", role: "group", "aria-label": "Inbox filters" });
  const statusRail = element("nav", { className: "status-rail", "aria-label": "Operational status summary" });

  function renderStatusRail(): void {
    const pendingApprovals = items.filter((item) => item.kind === "approval").length;
    const criticalIncidents = items.filter((item) => item.kind === "incident" && item.priority === "critical").length;
    const attentionTasks = items.filter((item) => item.kind === "task" && item.priority !== "routine").length;
    const statusLink = (label: string, value: string, href: string, alert = false) =>
      element("a", { className: `status-rail-item${alert ? " status-rail-item--alert" : ""}`, href }, element("span", {}, label), element("strong", {}, value));
    replaceChildren(statusRail,
      statusLink("Systems", systemTotal ? `${healthySystems}/${systemTotal} healthy` : "Unavailable", "#systems", systemTotal > 0 && healthySystems < systemTotal),
      statusLink("Critical incidents", String(criticalIncidents), "#incidents", criticalIncidents > 0),
      statusLink("Tasks needing attention", String(attentionTasks), "#tasks", attentionTasks > 0),
      statusLink("Pending approvals", String(pendingApprovals), "#approvals", pendingApprovals > 0),
    );
  }

  function renderDetail(): void {
    const selected = items.find((item) => item.id === selectedId) ?? null;
    if (!selected) {
      replaceChildren(detail,
        element("p", { className: "detail-index" }, "SELECT AN ITEM"),
        element("h2", {}, "Operational context"),
        element("p", { className: "detail-empty" }, "Choose an entry to inspect its source, timing, and identifiers."),
      );
      return;
    }
    const facts = element("dl", { className: "detail-facts" });
    for (const [label, value] of selected.facts) {
      facts.append(element("div", {}, element("dt", {}, label), element("dd", {}, value)));
    }
    replaceChildren(detail,
      element("button", { className: "detail-close", type: "button", "aria-label": "Close details" }, "×"),
      element("p", { className: `item-kind item-kind--${selected.priority}` }, itemLabel(selected.kind)),
      element("h2", {}, selected.title),
      element("p", { className: "detail-description" }, selected.description),
      facts,
      element("div", { className: "detail-actions" }, element("a", { className: "primary-action", href: `#${selected.kind === "approval" ? "approvals" : selected.kind === "incident" ? "incidents" : "tasks"}/${encodeURIComponent(selected.id.split(":").slice(1).join(":"))}` }, `Open ${itemLabel(selected.kind).toLowerCase()} →`)),
    );
    detail.querySelector<HTMLButtonElement>(".detail-close")?.addEventListener("click", () => {
      selectedId = null;
      render();
    });
  }

  function renderList(): void {
    if (loading) {
      resultSummary.textContent = "Collecting live operational state…";
      replaceChildren(list, element("div", { className: "loading-state" }, "Loading inbox"));
      return;
    }
    if (errorMessage && items.length === 0) {
      resultSummary.textContent = "Live state unavailable";
      replaceChildren(list, element("p", { className: "error-banner", role: "alert" }, errorMessage));
      return;
    }
    const visible = items.filter((item) => matches(item, query, filter));
    resultSummary.textContent = `${visible.length} of ${items.length} open items${errorMessage ? " · some sources unavailable" : ""}`;
    replaceChildren(list, ...(visible.length ? visible.map((item, index) => {
      const row = button(item.title, `inbox-row${selectedId === item.id ? " inbox-row--selected" : ""}`);
      replaceChildren(row,
        element("span", { className: `priority-mark priority-mark--${item.priority}`, "aria-label": `${item.priority} priority` }),
        element("span", { className: "row-index" }, String(index + 1).padStart(2, "0")),
        element("span", { className: "row-copy" }, element("span", { className: "item-kind" }, itemLabel(item.kind)), element("strong", {}, item.title), element("small", {}, item.description)),
        element("span", { className: "row-source" }, item.source),
        element("time", { datetime: item.timestamp }, formatDateTime(item.timestamp)),
        element("span", { className: "row-arrow", "aria-hidden": "true" }, "↗"),
      );
      row.addEventListener("click", () => {
        selectedId = item.id;
        render();
      });
      return row;
    }) : [element("p", { className: "empty-state" }, "Nothing here needs attention.")]));
  }

  function renderFilters(): void {
    const counts = {
      all: items.length,
      approval: items.filter((item) => item.kind === "approval").length,
      incident: items.filter((item) => item.kind === "incident").length,
      task: items.filter((item) => item.kind === "task").length,
    };
    replaceChildren(filterBar, ...(["all", "approval", "incident", "task"] as const).map((name) => {
      const label = name === "all" ? "All" : `${name[0].toUpperCase()}${name.slice(1)}s`;
      const control = button(`${label} ${counts[name]}`, `filter-button${filter === name ? " filter-button--active" : ""}`);
      control.addEventListener("click", () => {
        filter = name;
        render();
      });
      return control;
    }));
  }

  function render(): void {
    renderStatusRail();
    renderFilters();
    renderList();
    renderDetail();
  }

  async function load(showLoading = true): Promise<void> {
    if (loadInFlight) return;
    loadInFlight = true;
    if (showLoading && items.length === 0) loading = true;
    errorMessage = "";
    refreshButton.disabled = true;
    renderList();
    const results = await Promise.allSettled([
      fetchApprovals({ status: "pending", limit: 100 }),
      fetchIncidents({ status: "open", limit: 100 }),
      fetchTasks({ status: "open", limit: 100 }),
      fetchProviders(),
    ]);
    if (!active) return;
    const approvals = results[0].status === "fulfilled" ? results[0].value : [];
    const incidents = results[1].status === "fulfilled" ? results[1].value : [];
    const tasks = results[2].status === "fulfilled" ? results[2].value : [];
    const providers = results[3].status === "fulfilled" ? results[3].value : [];
    systemTotal = providers.length;
    healthySystems = providers.filter((provider) => provider.status === "healthy").length;
    items = buildInboxItems(approvals, incidents, tasks);
    const failedSources = results.filter((result) => result.status === "rejected").length;
    errorMessage = failedSources ? `${failedSources} operational source${failedSources === 1 ? " is" : "s are"} unavailable.` : "";
    if (selectedId && !items.some((item) => item.id === selectedId)) selectedId = null;
    loading = false;
    loadInFlight = false;
    refreshButton.disabled = false;
    render();
  }

  const handleSearch = () => {
    query = searchInput.value;
    renderList();
  };
  searchInput.addEventListener("input", handleSearch);
  refreshButton.addEventListener("click", () => void load());
  const pollTimer = window.setInterval(() => { if (!document.hidden) void load(false); }, 20_000);
  const handleVisibility = () => { if (!document.hidden) void load(false); };
  const handleFocus = () => void load(false);
  document.addEventListener("visibilitychange", handleVisibility);
  window.addEventListener("focus", handleFocus);

  replaceChildren(target,
    element("section", { className: "inbox-page", "aria-labelledby": "inbox-heading" },
      element("header", { className: "inbox-heading" },
        element("div", {}, element("p", { className: "eyebrow" }, "Live operations"), element("h1", { id: "inbox-heading" }, "Inbox"), element("p", { className: "inbox-intro" }, "What needs your attention, without the dashboard noise.")),
        refreshButton,
      ),
      statusRail,
      filterBar,
      resultSummary,
      element("div", { className: "inbox-workspace" }, list, detail),
    ),
  );
  void load();

  return () => {
    active = false;
    window.clearInterval(pollTimer);
    document.removeEventListener("visibilitychange", handleVisibility);
    window.removeEventListener("focus", handleFocus);
    searchInput.removeEventListener("input", handleSearch);
  };
}
