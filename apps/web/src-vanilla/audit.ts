import { fetchAudit } from "../src/lib/api";
import { formatDateTime } from "../src/lib/format";
import type { AuditEntry } from "../src/lib/types";
import { button, element, replaceChildren } from "./dom";

export type OutcomeTone = "success" | "warning" | "danger" | "neutral";
export type AuditFilters = { query: string; outcome: string };

export function outcomeTone(outcome: string): OutcomeTone {
  const value = outcome.toLowerCase();
  if (["ok", "success", "approved", "completed"].some((item) => value.includes(item))) return "success";
  if (["error", "failed", "denied", "rejected"].some((item) => value.includes(item))) return "danger";
  if (["pending", "waiting", "required"].some((item) => value.includes(item))) return "warning";
  return "neutral";
}

export function outcomeLabel(outcome: string): string {
  return { success: "Success", warning: "Waiting", danger: "Error", neutral: "Recorded" }[outcomeTone(outcome)];
}

export function filterAuditEntries(entries: AuditEntry[], filters: AuditFilters): AuditEntry[] {
  const query = filters.query.trim().toLowerCase();
  return entries.filter((entry) => {
    if (filters.outcome) {
      const toneFilter = ["success", "warning", "danger", "neutral"].includes(filters.outcome);
      if (toneFilter ? outcomeTone(entry.outcome) !== filters.outcome : entry.outcome !== filters.outcome) return false;
    }
    if (!query) return true;
    return [entry.action, entry.actor, entry.source, entry.outcome, entry.tool_id, entry.task_id].filter(Boolean).join(" ").toLowerCase().includes(query);
  });
}

export function mountActivity(target: HTMLElement, searchInput: HTMLInputElement): () => void {
  let entries: AuditEntry[] = [];
  let selectedId: string | null = null;
  let filters: AuditFilters = { query: "", outcome: "" };
  let loading = true;
  let errorMessage = "";
  let active = true;
  const refreshButton = button("Refresh", "quiet-button");
  const filterBar = element("div", { className: "filter-bar" });
  const summary = element("p", { className: "result-summary" });
  const list = element("div", { className: "record-list" });
  const detail = element("aside", { className: "record-detail", "aria-label": "Selected audit event" });

  function renderDetail(): void {
    const entry = entries.find((item) => item.id === selectedId);
    if (!entry) { replaceChildren(detail, element("p", { className: "detail-index" }, "IMMUTABLE RECORD"), element("h2", {}, "Select an event"), element("p", { className: "detail-empty" }, "Inspect attribution and normalized metadata from the append-only audit trail.")); return; }
    const facts = element("dl", { className: "detail-facts" });
    for (const [label, value] of [["Outcome", entry.outcome], ["Actor", entry.actor], ["Source", entry.source], ["Tool", entry.tool_id ?? "None"], ["Task", entry.task_id ?? "None"], ["Event", entry.id]]) facts.append(element("div", {}, element("dt", {}, label), element("dd", {}, value)));
    replaceChildren(detail,
      element("button", { className: "detail-close", type: "button", "aria-label": "Close details" }, "×"),
      element("p", { className: `item-kind outcome-${outcomeTone(entry.outcome)}` }, outcomeLabel(entry.outcome)), element("h2", {}, entry.action), facts,
      entry.metadata ? element("pre", { className: "metadata-output" }, JSON.stringify(entry.metadata, null, 2)) : element("p", { className: "detail-empty" }, "No event metadata."),
    );
    detail.querySelector<HTMLButtonElement>(".detail-close")?.addEventListener("click", () => { selectedId = null; render(); });
  }

  function renderFilters(): void {
    replaceChildren(filterBar, ...(["", "success", "warning", "danger", "neutral"] as const).map((tone) => {
      const label = tone ? tone[0].toUpperCase() + tone.slice(1) : "All events";
      const control = button(label, `filter-button${filters.outcome === tone ? " filter-button--active" : ""}`);
      control.addEventListener("click", () => { filters = { ...filters, outcome: tone }; render(); }); return control;
    }));
  }

  function render(): void {
    renderFilters();
    if (loading) { summary.textContent = "Reading immutable activity…"; replaceChildren(list, element("div", { className: "loading-state" }, "Loading activity")); renderDetail(); return; }
    if (errorMessage) { summary.textContent = "Activity unavailable"; replaceChildren(list, element("p", { className: "error-banner", role: "alert" }, errorMessage)); renderDetail(); return; }
    const visible = filterAuditEntries(entries, filters);
    summary.textContent = `${visible.length} of ${entries.length} recent events`;
    replaceChildren(list, ...(visible.length ? visible.map((entry, index) => {
      const tone = outcomeTone(entry.outcome);
      const row = button(entry.action, `record-row${selectedId === entry.id ? " record-row--selected" : ""}`);
      replaceChildren(row, element("span", { className: `state-dot state-dot--${tone === "success" ? "healthy" : tone === "danger" ? "critical" : tone === "warning" ? "warning" : "neutral"}` }), element("span", { className: "row-index" }, String(index + 1).padStart(2, "0")), element("span", { className: "record-copy" }, element("strong", {}, entry.action), element("small", {}, `${entry.actor} · ${entry.source}`)), element("span", { className: `state-label outcome-${tone}` }, outcomeLabel(entry.outcome)), element("time", { className: "record-meta", datetime: entry.created_at }, formatDateTime(entry.created_at)), element("span", { className: "row-arrow" }, "↗"));
      row.addEventListener("click", () => { selectedId = entry.id; render(); }); return row;
    }) : [element("p", { className: "empty-state" }, "No activity matches this view.")]));
    renderDetail();
  }
  async function load(): Promise<void> {
    loading = true; errorMessage = ""; refreshButton.disabled = true; render();
    try { entries = await fetchAudit(100); }
    catch (error) { errorMessage = error instanceof Error ? error.message : "Unable to load activity."; }
    finally { if (active) { loading = false; refreshButton.disabled = false; render(); } }
  }
  const handleSearch = () => { filters = { ...filters, query: searchInput.value }; render(); };
  searchInput.addEventListener("input", handleSearch); refreshButton.addEventListener("click", () => void load());
  replaceChildren(target, element("section", { className: "inbox-page", "aria-labelledby": "activity-heading" }, element("header", { className: "inbox-heading compact-heading" }, element("div", {}, element("p", { className: "eyebrow" }, "Audit trail"), element("h1", { id: "activity-heading" }, "Activity"), element("p", { className: "inbox-intro" }, "Every actor, decision, and tool invocation in one chronology.")), refreshButton), filterBar, summary, element("div", { className: "inbox-workspace" }, list, detail)));
  void load();
  return () => { active = false; searchInput.removeEventListener("input", handleSearch); };
}
