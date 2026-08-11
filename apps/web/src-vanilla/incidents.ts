import { fetchIncidents, resolveIncidentHandled } from "../src/lib/api";
import { formatDateTime } from "../src/lib/format";
import type { Incident } from "../src/lib/types";
import { button, element, replaceChildren } from "./dom";

export function mountIncidents(target: HTMLElement, searchInput: HTMLInputElement): () => void {
  let incidents: Incident[] = [];
  let selectedId: string | null = null;
  let filter = "open";
  let query = "";
  let loading = true;
  let busy = false;
  let errorMessage = "";
  let active = true;
  let loadInFlight = false;
  const refreshButton = button("Refresh", "quiet-button");
  const filters = element("div", { className: "filter-bar", role: "group", "aria-label": "Incident status" });
  const summary = element("p", { className: "result-summary" });
  const list = element("div", { className: "record-list" });
  const detail = element("aside", { className: "record-detail", "aria-label": "Selected incident" });

  async function resolveHandled(incident: Incident, note: string): Promise<void> {
    if (!note.trim()) return;
    busy = true; errorMessage = ""; renderDetail();
    try { await resolveIncidentHandled(incident.id, note.trim()); await load(false); }
    catch (error) { errorMessage = error instanceof Error ? error.message : "Unable to resolve the incident."; }
    finally { if (active) { busy = false; render(); } }
  }

  function renderDetail(): void {
    const incident = incidents.find((item) => item.id === selectedId);
    if (!incident) {
      replaceChildren(detail, element("p", { className: "detail-index" }, "INCIDENT CONTEXT"), element("h2", {}, "Select an incident"), element("p", { className: "detail-empty" }, "Review evidence, recurrence, linked work, and resolution state."));
      return;
    }
    const facts = element("dl", { className: "detail-facts" });
    for (const [label, value] of [["Status", incident.status], ["Severity", incident.severity], ["Provider", incident.provider_id], ["Watcher", incident.watcher_id], ["First seen", formatDateTime(incident.first_seen_at)], ["Last seen", formatDateTime(incident.last_seen_at)], ["Occurrences", String(incident.occurrences)], ["Task", incident.task_id ?? "None"]]) facts.append(element("div", {}, element("dt", {}, label), element("dd", {}, value)));
    const actions = element("div", { className: "incident-detail-actions" });
    if (incident.task_id) actions.append(element("a", { className: "quiet-button", href: `#tasks/${encodeURIComponent(incident.task_id)}` }, "Open linked task →"));
    if (incident.status === "open") {
      const note = element("textarea", { className: "control-input", rows: 3, placeholder: "Required resolution note", "aria-label": "Incident resolution note" });
      const resolve = button(busy ? "Resolving…" : "Already handled", "primary-action");
      resolve.disabled = busy;
      resolve.addEventListener("click", () => { if (note.value.trim()) void resolveHandled(incident, note.value); else note.focus(); });
      actions.append(element("label", { className: "control-field incident-note" }, element("span", {}, "Operator resolution"), note), resolve);
    }
    replaceChildren(detail, element("button", { className: "detail-close", type: "button", "aria-label": "Close details" }, "×"), element("p", { className: `item-kind state-${incident.severity === "critical" ? "critical" : "warning"}` }, `${incident.severity} · ${incident.status}`), element("h2", {}, incident.title), element("p", { className: "detail-description" }, incident.description), errorMessage ? element("p", { className: "error-banner", role: "alert" }, errorMessage) : null, facts, actions);
    detail.querySelector<HTMLButtonElement>(".detail-close")?.addEventListener("click", () => { selectedId = null; window.history.replaceState(null, "", "#incidents"); render(); });
  }

  function renderFilters(): void {
    replaceChildren(filters, ...["open", "resolved", "all"].map((status) => { const control = button(status[0].toUpperCase() + status.slice(1), `filter-button${filter === status ? " filter-button--active" : ""}`); control.addEventListener("click", () => { filter = status; render(); }); return control; }));
  }

  function render(): void {
    renderFilters();
    if (loading) { summary.textContent = "Reading incident ledger…"; replaceChildren(list, element("div", { className: "loading-state" }, "Loading incidents")); renderDetail(); return; }
    const normalized = query.trim().toLowerCase();
    const visible = incidents.filter((incident) => (filter === "all" || incident.status === filter) && (!normalized || [incident.id, incident.title, incident.description, incident.provider_id, incident.watcher_id, incident.task_id].filter(Boolean).join(" ").toLowerCase().includes(normalized)));
    const critical = incidents.filter((incident) => incident.status === "open" && incident.severity === "critical").length;
    summary.textContent = `${incidents.filter((incident) => incident.status === "open").length} open · ${critical} critical · ${incidents.length} loaded`;
    replaceChildren(list, ...(visible.length ? visible.map((incident, index) => {
      const row = button(incident.title, `record-row${selectedId === incident.id ? " record-row--selected" : ""}`);
      replaceChildren(row, element("span", { className: `state-dot state-dot--${incident.status === "resolved" ? "healthy" : incident.severity === "critical" ? "critical" : "warning"}` }), element("span", { className: "row-index" }, String(index + 1).padStart(2, "0")), element("span", { className: "record-copy" }, element("strong", {}, incident.title), element("small", {}, `${incident.provider_id} · ${incident.description}`)), element("span", { className: "state-label" }, incident.severity), element("span", { className: "record-meta" }, formatDateTime(incident.last_seen_at)), element("span", { className: "row-arrow" }, "↗"));
      row.addEventListener("click", () => { selectedId = incident.id; window.history.replaceState(null, "", `#incidents/${encodeURIComponent(incident.id)}`); render(); }); return row;
    }) : [element("p", { className: "empty-state" }, "No incidents match this view.")]));
    renderDetail();
  }

  async function load(showLoading = true): Promise<void> {
    if (loadInFlight) return;
    loadInFlight = true; if (showLoading && incidents.length === 0) loading = true; refreshButton.disabled = true;
    try { incidents = await fetchIncidents({ limit: 100 }); errorMessage = ""; }
    catch (error) { errorMessage = error instanceof Error ? error.message : "Unable to load incidents."; }
    finally { if (active) { loadInFlight = false; loading = false; refreshButton.disabled = false; render(); } }
  }
  const handleSearch = () => { query = searchInput.value; render(); };
  const timer = window.setInterval(() => { if (!document.hidden && !busy) void load(false); }, 20_000);
  searchInput.addEventListener("input", handleSearch); refreshButton.addEventListener("click", () => void load());
  replaceChildren(target, element("section", { className: "inbox-page", "aria-labelledby": "incidents-heading" }, element("header", { className: "inbox-heading compact-heading" }, element("div", {}, element("p", { className: "eyebrow" }, "Operational response"), element("h1", { id: "incidents-heading" }, "Incidents"), element("p", { className: "inbox-intro" }, "Active failures and anomalies that require investigation or resolution.")), refreshButton), filters, summary, element("div", { className: "inbox-workspace" }, list, detail)));
  void load().then(() => { const incidentId = window.location.hash.startsWith("#incidents/") ? decodeURIComponent(window.location.hash.slice("#incidents/".length)) : ""; if (incidentId && incidents.some((incident) => incident.id === incidentId)) { selectedId = incidentId; render(); } });
  return () => { active = false; window.clearInterval(timer); searchInput.removeEventListener("input", handleSearch); };
}
