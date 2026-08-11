import { fetchConversationStatus, fetchLunaMetrics, reviewTaskRouter } from "../src/lib/api";
import { formatDateTime } from "../src/lib/format";
import type { ConversationStatus, LunaMetrics, TaskRouterCorrections } from "../src/lib/types";
import { button, element, replaceChildren } from "./dom";
import { confirmAction } from "./modal";

function metric(label: string, value: string, detail = ""): HTMLElement {
  return element("div", { className: "metric-cell" }, element("span", {}, label), element("strong", {}, value), detail ? element("small", {}, detail) : null);
}

export function mountMetrics(target: HTMLElement, searchInput: HTMLInputElement): () => void {
  let data: LunaMetrics | null = null;
  let days = 30;
  let query = "";
  let loading = true;
  let busyTask = "";
  let errorMessage = "";
  let active = true;
  const refreshButton = button("Refresh", "quiet-button");
  const periods = element("div", { className: "filter-bar" });
  const content = element("div", { className: "metrics-content" });

  async function review(taskId: string, verdict: "accepted" | "corrected" | "rejected", corrections?: TaskRouterCorrections): Promise<void> {
    if (verdict === "rejected" && !await confirmAction("Reject routing suggestion", "Record this router decision as rejected?", "Reject")) return;
    busyTask = taskId; errorMessage = ""; render();
    try { await reviewTaskRouter(taskId, { verdict, corrections }); await load(); }
    catch (error) { errorMessage = error instanceof Error ? error.message : "Unable to review routing."; }
    finally { if (active) { busyTask = ""; render(); } }
  }

  function reviewCard(item: LunaMetrics["review_queue"][number]): HTMLElement {
    const action = element("input", { className: "control-input", value: item.action, "aria-label": "Corrected action" });
    const category = element("input", { className: "control-input", value: item.category, "aria-label": "Corrected category" });
    const owner = element("input", { className: "control-input", value: item.suggested_owner, "aria-label": "Corrected owner" });
    const accept = button("Accept", "quiet-button"); const correct = button("Save correction", "quiet-button"); const reject = button("Reject", "quiet-button danger-text");
    accept.disabled = correct.disabled = reject.disabled = busyTask === item.task_id;
    accept.addEventListener("click", () => void review(item.task_id, "accepted"));
    correct.addEventListener("click", () => void review(item.task_id, "corrected", { action: action.value.trim(), category: category.value.trim(), suggested_owner: owner.value.trim(), priority: item.priority, severity: item.severity, needs_operator: item.needs_operator }));
    reject.addEventListener("click", () => void review(item.task_id, "rejected"));
    return element("article", { className: "review-card" }, element("div", { className: "record-copy" }, element("strong", {}, item.task_title), element("small", {}, `${item.task_id} · confidence ${item.confidence ?? "—"}`)), element("p", {}, item.summary), element("div", { className: "review-fields" }, action, category, owner), element("div", { className: "task-quick-actions" }, accept, correct, reject));
  }

  function renderPeriods(): void {
    replaceChildren(periods, ...[1, 7, 30, 90, 365].map((period) => { const control = button(`${period}d`, `filter-button${days === period ? " filter-button--active" : ""}`); control.addEventListener("click", () => { days = period; void load(); }); return control; }));
  }
  function render(): void {
    renderPeriods();
    if (loading) { replaceChildren(content, element("div", { className: "loading-state" }, "Loading AI operations metrics")); return; }
    if (!data) { replaceChildren(content, element("p", { className: "error-banner", role: "alert" }, errorMessage || "Metrics unavailable.")); return; }
    const normalized = query.trim().toLowerCase();
    const reviews = data.review_queue.filter((item) => !normalized || [item.task_title, item.action, item.category, item.suggested_owner].join(" ").toLowerCase().includes(normalized));
    replaceChildren(content, errorMessage ? element("p", { className: "error-banner", role: "alert" }, errorMessage) : null,
      element("section", { className: "metric-grid" }, metric("Attributed cost", `$${data.summary.attributed_cost_usd.toFixed(4)}`, `${data.summary.priced_calls} priced calls`), metric("Technical success", `${Math.round(data.router.technical_success_rate * 100)}%`, `${data.router.decisions} routing decisions`), metric("Reviewed accuracy", data.router.reviewed_accuracy === null ? "—" : `${Math.round(data.router.reviewed_accuracy * 100)}%`, `${data.router.reviewed} reviewed`), metric("Local rate", `${Math.round(data.ai_manager.local_rate * 100)}%`, `${data.ai_manager.fallback_calls} fallback calls`), metric("Metering", `${Math.round(data.summary.metering_coverage * 100)}%`, `${data.summary.input_tokens + data.summary.output_tokens} tokens`), metric("Delivery", `${data.ai_delivery.successful_calls}/${data.ai_delivery.calls}`, `${Math.round(data.ai_delivery.fallback_rate * 100)}% fallback`)),
      element("section", { className: "metrics-block" }, element("p", { className: "eyebrow" }, "Component usage"), element("div", { className: "run-list" }, ...data.components.map((component) => element("div", { className: "run-row" }, element("span", { className: "state-dot state-dot--healthy" }), element("strong", {}, component.label), element("span", {}, `${component.calls} calls`), element("span", {}, `${component.input_tokens + component.output_tokens} tokens`), element("span", {}, `$${component.attributed_cost_usd.toFixed(4)}`))))),
      element("section", { className: "metrics-block" }, element("p", { className: "eyebrow" }, `Router review queue · ${reviews.length}`), element("div", { className: "review-grid" }, ...(reviews.length ? reviews.map(reviewCard) : [element("p", { className: "empty-state" }, "No routing decisions need review.")]))));
  }
  async function load(): Promise<void> { loading = true; refreshButton.disabled = true; render(); try { data = await fetchLunaMetrics(days); errorMessage = ""; } catch (error) { errorMessage = error instanceof Error ? error.message : "Unable to load metrics."; } finally { if (active) { loading = false; refreshButton.disabled = false; render(); } } }
  const handleSearch = () => { query = searchInput.value; render(); };
  searchInput.addEventListener("input", handleSearch); refreshButton.addEventListener("click", () => void load());
  replaceChildren(target, element("section", { className: "inbox-page", "aria-labelledby": "metrics-heading" }, element("header", { className: "inbox-heading compact-heading" }, element("div", {}, element("p", { className: "eyebrow" }, "Luna ledger"), element("h1", { id: "metrics-heading" }, "Metrics"), element("p", { className: "inbox-intro" }, "Cost, reliability, routing quality, and human review.")), refreshButton), periods, content));
  void load();
  return () => { active = false; searchInput.removeEventListener("input", handleSearch); };
}

export function mountDelivery(target: HTMLElement): () => void {
  let metrics: LunaMetrics | null = null;
  let conversation: ConversationStatus | null = null;
  let days = 7;
  let active = true;
  let loading = true;
  let errorMessage = "";
  const refreshButton = button("Refresh", "quiet-button");
  const periods = element("div", { className: "filter-bar" });
  const content = element("div", { className: "metrics-content" });
  function render(): void {
    replaceChildren(periods, ...[1, 7, 30].map((period) => { const control = button(period === 1 ? "24h" : `${period}d`, `filter-button${days === period ? " filter-button--active" : ""}`); control.addEventListener("click", () => { days = period; void load(); }); return control; }));
    if (loading) { replaceChildren(content, element("div", { className: "loading-state" }, "Loading delivery trace")); return; }
    if (!metrics || !conversation) { replaceChildren(content, element("p", { className: "error-banner" }, errorMessage || "Delivery trace unavailable.")); return; }
    const delivery = metrics.ai_delivery;
    replaceChildren(content, element("section", { className: "delivery-route" }, element("span", {}, "Telegram"), element("b", {}, "→"), element("span", {}, conversation.model || "Primary model"), element("b", {}, "→"), element("span", {}, "Validated reply")), element("section", { className: "metric-grid" }, metric("Turns", String(delivery.calls)), metric("Successful", String(delivery.successful_calls)), metric("Failed", String(delivery.failed_calls)), metric("Fallback", `${Math.round(delivery.fallback_rate * 100)}%`), metric("Average latency", delivery.latency.average_ms === null ? "—" : `${Math.round(delivery.latency.average_ms)} ms`), metric("P95 latency", delivery.latency.p95_ms === null ? "—" : `${Math.round(delivery.latency.p95_ms)} ms`)), element("section", { className: "metrics-block" }, element("p", { className: "eyebrow" }, "Recent delivery ledger"), element("div", { className: "run-list" }, ...delivery.recent.slice(0, 20).map((entry) => element("div", { className: "run-row" }, element("span", { className: `state-dot state-dot--${entry.status === "success" || entry.status === "completed" ? "healthy" : "critical"}` }), element("strong", {}, entry.provider), element("span", {}, entry.model), element("span", {}, entry.fallback_used ? `fallback · ${entry.fallback_reason}` : entry.route_mode), element("time", {}, formatDateTime(entry.created_at)))))), element("section", { className: "settings-block" }, element("p", { className: "eyebrow" }, "Conversation limits"), element("h2", {}, conversation.configured ? "Configured" : "Disabled"), element("p", { className: "settings-copy" }, `${conversation.max_turns} turns · ${conversation.max_tool_calls} tool calls · ${conversation.max_output_tokens} output tokens · ${conversation.timeout_seconds}s timeout`)));
  }
  async function load(): Promise<void> { loading = true; refreshButton.disabled = true; render(); const results = await Promise.allSettled([fetchLunaMetrics(days), fetchConversationStatus()]); if (!active) return; if (results[0].status === "fulfilled") metrics = results[0].value; if (results[1].status === "fulfilled") conversation = results[1].value; errorMessage = results.some((result) => result.status === "rejected") ? "Some delivery data could not be loaded." : ""; loading = false; refreshButton.disabled = false; render(); }
  refreshButton.addEventListener("click", () => void load());
  replaceChildren(target, element("section", { className: "inbox-page", "aria-labelledby": "delivery-heading" }, element("header", { className: "inbox-heading compact-heading" }, element("div", {}, element("p", { className: "eyebrow" }, "Conversation route"), element("h1", { id: "delivery-heading" }, "AI Delivery"), element("p", { className: "inbox-intro" }, "The route from Telegram request to validated response.")), refreshButton), periods, content));
  void load(); return () => { active = false; };
}
