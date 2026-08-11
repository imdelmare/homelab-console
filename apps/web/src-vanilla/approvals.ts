import { decideApproval, fetchApprovals } from "../src/lib/api";
import { formatCountdown, formatDateTime } from "../src/lib/format";
import type { Approval } from "../src/lib/types";
import { button, element, replaceChildren } from "./dom";
import { confirmAction } from "./modal";

export function mountApprovals(target: HTMLElement, searchInput: HTMLInputElement): () => void {
  let approvals: Approval[] = [];
  let selectedId: string | null = null;
  let filter = "";
  let query = "";
  let loading = true;
  let busyId: string | null = null;
  let errorMessage = "";
  let active = true;
  let loadInFlight = false;
  const refreshButton = button("Refresh", "quiet-button");
  const filterBar = element("div", { className: "filter-bar", role: "group", "aria-label": "Approval status" });
  const summary = element("p", { className: "result-summary" });
  const list = element("div", { className: "record-list" });
  const detail = element("aside", { className: "record-detail", "aria-label": "Selected approval" });

  function effectivelyExpired(approval: Approval): boolean {
    return approval.status === "pending" && formatCountdown(approval.expires_at) === "0:00";
  }

  async function decide(approval: Approval, approve: boolean): Promise<void> {
    if (approve && !await confirmAction("Approve exact execution", `Approve “${approval.action || approval.tool_id}” requested by ${approval.requested_by}? This grant can be consumed once and only with the requested input.`, "Approve")) return;
    busyId = approval.id; errorMessage = ""; render();
    try { await decideApproval(approval.id, approve); await load(); }
    catch (error) { errorMessage = error instanceof Error ? error.message : "Unable to record the decision."; }
    finally { if (active) { busyId = null; render(); } }
  }

  function renderDetail(): void {
    const approval = approvals.find((item) => item.id === selectedId);
    if (!approval) { replaceChildren(detail, element("p", { className: "detail-index" }, "SINGLE-USE GRANT"), element("h2", {}, "Select a request"), element("p", { className: "detail-empty" }, "Review exact attribution and expiry before deciding an infrastructure request.")); return; }
    const expired = effectivelyExpired(approval);
    const facts = element("dl", { className: "detail-facts" });
    for (const [label, value] of [["Status", expired ? "expired" : approval.status], ["Requested by", approval.requested_by], ["Created", formatDateTime(approval.created_at)], ["Expires", formatDateTime(approval.expires_at)], ["Task", approval.task_id ?? "None"], ["Request", approval.id]]) facts.append(element("div", {}, element("dt", {}, label), element("dd", {}, value)));
    const actions = element("div", { className: "detail-actions" });
    if (approval.status === "pending" && !expired) {
      const approve = button(busyId === approval.id ? "Working…" : "Approve", "primary-action");
      const deny = button("Deny", "quiet-button danger-text");
      approve.disabled = deny.disabled = busyId === approval.id;
      approve.addEventListener("click", () => void decide(approval, true));
      deny.addEventListener("click", () => void decide(approval, false));
      actions.append(approve, deny);
    }
    replaceChildren(detail, element("button", { className: "detail-close", type: "button", "aria-label": "Close details" }, "×"), element("p", { className: "item-kind" }, expired ? "Expired" : approval.status), element("h2", {}, approval.action || approval.tool_id), element("p", { className: "detail-description" }, approval.tool_id), facts, actions);
    detail.querySelector<HTMLButtonElement>(".detail-close")?.addEventListener("click", () => { selectedId = null; render(); });
  }

  function renderFilters(): void {
    replaceChildren(filterBar, ...["", "pending", "approved", "denied", "expired", "consumed"].map((status) => {
      const label = status ? status[0].toUpperCase() + status.slice(1) : "All";
      const control = button(label, `filter-button${filter === status ? " filter-button--active" : ""}`);
      control.addEventListener("click", () => { filter = status; render(); }); return control;
    }));
  }

  function render(): void {
    renderFilters();
    if (loading) { summary.textContent = "Reading approval ledger…"; replaceChildren(list, element("div", { className: "loading-state" }, "Loading approvals")); renderDetail(); return; }
    const normalized = query.trim().toLowerCase();
    const visible = approvals.filter((approval) => (!filter || (filter === "expired" ? approval.status === "expired" || effectivelyExpired(approval) : approval.status === filter)) && (!normalized || [approval.action, approval.tool_id, approval.requested_by, approval.task_id, approval.id].filter(Boolean).join(" ").toLowerCase().includes(normalized)));
    const pending = approvals.filter((approval) => approval.status === "pending" && !effectivelyExpired(approval)).length;
    summary.textContent = `${pending} waiting · ${approvals.length} recent requests`;
    replaceChildren(list, errorMessage ? element("p", { className: "error-banner", role: "alert" }, errorMessage) : null, ...(visible.length ? visible.map((approval, index) => {
      const expired = effectivelyExpired(approval);
      const status = expired ? "expired" : approval.status;
      const row = button(approval.action || approval.tool_id, `record-row${selectedId === approval.id ? " record-row--selected" : ""}`);
      replaceChildren(row, element("span", { className: `state-dot state-dot--${status === "pending" ? "warning" : status === "denied" ? "critical" : status === "approved" || status === "consumed" ? "healthy" : "neutral"}` }), element("span", { className: "row-index" }, String(index + 1).padStart(2, "0")), element("span", { className: "record-copy" }, element("strong", {}, approval.action || approval.tool_id), element("small", {}, `${approval.tool_id} · ${approval.requested_by}`)), element("span", { className: "state-label" }, status), element("span", { className: "record-meta" }, status === "pending" ? `expires ${formatCountdown(approval.expires_at)}` : formatDateTime(approval.created_at)), element("span", { className: "row-arrow" }, "↗"));
      row.addEventListener("click", () => { selectedId = approval.id; window.history.replaceState(null, "", `#approvals/${encodeURIComponent(approval.id)}`); render(); }); return row;
    }) : [element("p", { className: "empty-state" }, "No approvals match this view.")]));
    renderDetail();
  }

  async function load(): Promise<void> {
    if (loadInFlight) return;
    loadInFlight = true; refreshButton.disabled = true;
    if (!approvals.length) loading = true;
    try { approvals = await fetchApprovals({ limit: 100 }); errorMessage = ""; }
    catch (error) { errorMessage = error instanceof Error ? error.message : "Unable to load approvals."; }
    finally { if (active) { loading = false; loadInFlight = false; refreshButton.disabled = false; render(); } }
  }
  const handleSearch = () => { query = searchInput.value; render(); };
  const pollTimer = window.setInterval(() => { if (!document.hidden) void load(); }, 5_000);
  const countdownTimer = window.setInterval(() => { if (approvals.some((approval) => approval.status === "pending")) render(); }, 1_000);
  searchInput.addEventListener("input", handleSearch); refreshButton.addEventListener("click", () => void load());
  replaceChildren(target, element("section", { className: "inbox-page", "aria-labelledby": "approvals-heading" }, element("header", { className: "inbox-heading compact-heading" }, element("div", {}, element("p", { className: "eyebrow" }, "Operator decisions"), element("h1", { id: "approvals-heading" }, "Approvals"), element("p", { className: "inbox-intro" }, "Single-use grants for exact infrastructure actions.")), refreshButton), filterBar, summary, element("div", { className: "inbox-workspace" }, list, detail)));
  void load().then(() => {
    const approvalId = window.location.hash.startsWith("#approvals/") ? decodeURIComponent(window.location.hash.slice("#approvals/".length)) : "";
    if (approvalId && approvals.some((approval) => approval.id === approvalId)) { selectedId = approvalId; render(); }
  });
  return () => { active = false; window.clearInterval(pollTimer); window.clearInterval(countdownTimer); searchInput.removeEventListener("input", handleSearch); };
}
