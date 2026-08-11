import { createProviderTask, fetchCapabilityObservations, fetchProviderDefinitions, fetchProviders, fetchTools } from "../src/lib/api";
import { formatDateTime } from "../src/lib/format";
import type { CapabilityObservation, Provider, ProviderDefinition, ProviderStatus, ToolDefinition } from "../src/lib/types";
import { button, element, replaceChildren } from "./dom";

export function providerAttention(status: ProviderStatus): "healthy" | "warning" | "critical" {
  if (status === "healthy") return "healthy";
  if (status === "degraded" || status === "unknown") return "warning";
  return "critical";
}

export function mountSystems(target: HTMLElement, searchInput: HTMLInputElement): () => void {
  let providers: Provider[] = [];
  let definitions: ProviderDefinition[] = [];
  let tools: ToolDefinition[] = [];
  let observations: CapabilityObservation[] = [];
  let selectedId: string | null = null;
  let detailTab: "summary" | "capabilities" | "observations" = "summary";
  let query = "";
  let loading = true;
  let detailLoading = false;
  let actionBusy = false;
  let errorMessage = "";
  let detailError = "";
  let active = true;
  const refreshButton = button("Refresh", "quiet-button");
  const summary = element("p", { className: "result-summary" });
  const list = element("div", { className: "record-list" });
  const detail = element("aside", { className: "record-detail", "aria-label": "Selected system" });

  async function createInvestigation(provider: Provider): Promise<void> {
    actionBusy = true; detailError = ""; renderDetail();
    try {
      const task = await createProviderTask(provider.id, `Investigate ${provider.name}: ${provider.detail || provider.last_error?.message || provider.status}`);
      window.location.hash = `tasks/${task.id}`;
    } catch (error) {
      detailError = error instanceof Error ? error.message : "Unable to create the investigation task.";
    } finally {
      if (active) { actionBusy = false; renderDetail(); }
    }
  }

  function renderDetail(): void {
    const provider = providers.find((item) => item.id === selectedId);
    if (!provider) {
      replaceChildren(detail, element("p", { className: "detail-index" }, "SYSTEM CONTEXT"), element("h2", {}, "Select a system"), element("p", { className: "detail-empty" }, "Inspect health, timing, capabilities, and observations without opening a separate dashboard."));
      return;
    }
    const definition = definitions.find((item) => item.id === provider.id);
    const providerTools = tools.filter((tool) => tool.provider_id === provider.id);
    const providerObservations = observations.filter((observation) => observation.provider_id === provider.id);
    const facts = element("dl", { className: "detail-facts" });
    for (const [label, value] of [
      ["Status", provider.status], ["Checked", formatDateTime(provider.checked_at)], ["Last healthy", formatDateTime(provider.last_ok_at)], ["Tools", String(provider.tool_count)], ["Watchers", provider.watchers.join(", ") || "None"], ["Driver", definition?.driver_id ?? "Unknown"], ["Transport", definition?.transport ?? "Unknown"],
    ]) facts.append(element("div", {}, element("dt", {}, label), element("dd", {}, value)));
    const tabs = element("div", { className: "filter-bar detail-tabs" }, ...(["summary", "capabilities", "observations"] as const).map((tab) => {
      const control = button(tab === "capabilities" ? `Capabilities ${providerTools.length}` : tab === "observations" ? `Observations ${providerObservations.length}` : "Summary", `filter-button${detailTab === tab ? " filter-button--active" : ""}`);
      control.addEventListener("click", () => { detailTab = tab; renderDetail(); });
      return control;
    }));
    let body: HTMLElement;
    if (detailLoading) body = element("div", { className: "loading-state detail-loading" }, "Loading provider detail");
    else if (detailTab === "capabilities") {
      body = element("div", { className: "detail-notes" }, element("h3", {}, "Governed tools"), ...(providerTools.length ? providerTools.map((tool) => element("p", {}, element("strong", {}, tool.name), `${tool.id} · ${tool.mode} · ${tool.risk}${tool.enabled ? "" : " · disabled"}`)) : [element("p", {}, "No tools declared for this provider.")]));
    } else if (detailTab === "observations") {
      body = element("div", { className: "detail-notes" }, element("h3", {}, "Capability observations"), ...(providerObservations.length ? providerObservations.map((observation) => element("p", {}, element("strong", {}, observation.label), `${observation.status} · ${observation.detail} · checked ${formatDateTime(observation.checked_at)}`)) : [element("p", {}, "No observations recorded for this provider.")]));
    } else {
      body = element("div", {}, facts, provider.status !== "healthy" ? element("button", { className: "primary-action", type: "button", disabled: actionBusy }, actionBusy ? "Creating…" : "Create investigation task") : null);
      body.querySelector(".primary-action")?.addEventListener("click", () => void createInvestigation(provider));
    }
    replaceChildren(detail,
      element("button", { className: "detail-close", type: "button", "aria-label": "Close details" }, "×"),
      element("p", { className: `item-kind state-${providerAttention(provider.status)}` }, provider.status),
      element("h2", {}, provider.name),
      element("p", { className: "detail-description" }, provider.detail || provider.last_error?.message || "No additional provider detail."),
      tabs,
      detailError ? element("p", { className: "error-banner", role: "alert" }, detailError) : null,
      body,
    );
    detail.querySelector<HTMLButtonElement>(".detail-close")?.addEventListener("click", () => { selectedId = null; render(); });
  }

  function render(): void {
    if (loading) {
      summary.textContent = "Checking configured systems…";
      replaceChildren(list, element("div", { className: "loading-state" }, "Loading systems"));
      renderDetail();
      return;
    }
    if (errorMessage) {
      summary.textContent = "System state unavailable";
      replaceChildren(list, element("p", { className: "error-banner", role: "alert" }, errorMessage));
      renderDetail();
      return;
    }
    const normalized = query.trim().toLowerCase();
    const visible = providers.filter((provider) => !normalized || [provider.name, provider.id, provider.status, provider.detail].filter(Boolean).join(" ").toLowerCase().includes(normalized));
    const attention = providers.filter((provider) => provider.status !== "healthy").length;
    summary.textContent = `${providers.length} configured systems · ${attention ? `${attention} need attention` : "all reporting healthy"}`;
    replaceChildren(list, ...(visible.length ? visible.map((provider, index) => {
      const row = button(provider.name, `record-row${selectedId === provider.id ? " record-row--selected" : ""}`);
      replaceChildren(row,
        element("span", { className: `state-dot state-dot--${providerAttention(provider.status)}`, "aria-label": provider.status }),
        element("span", { className: "row-index" }, String(index + 1).padStart(2, "0")),
        element("span", { className: "record-copy" }, element("strong", {}, provider.name), element("small", {}, provider.detail || provider.id)),
        element("span", { className: `state-label state-${providerAttention(provider.status)}` }, provider.status),
        element("span", { className: "record-meta" }, `${provider.tool_count} tools`),
        element("span", { className: "row-arrow", "aria-hidden": "true" }, "↗"),
      );
      row.addEventListener("click", () => {
        selectedId = provider.id;
        detailTab = "summary";
        window.history.replaceState(null, "", `#systems/${encodeURIComponent(provider.id)}`);
        render();
        void loadProviderDetail(provider.id);
      });
      return row;
    }) : [element("p", { className: "empty-state" }, "No systems match this search.")]));
    renderDetail();
  }

  async function load(): Promise<void> {
    loading = true; errorMessage = ""; refreshButton.disabled = true; render();
    const [providerResult, definitionResult, toolResult] = await Promise.allSettled([fetchProviders(), fetchProviderDefinitions(), fetchTools()]);
    if (!active) return;
    if (providerResult.status === "rejected") errorMessage = providerResult.reason instanceof Error ? providerResult.reason.message : "Unable to load systems.";
    else providers = providerResult.value;
    definitions = definitionResult.status === "fulfilled" ? definitionResult.value : [];
    tools = toolResult.status === "fulfilled" ? toolResult.value : [];
    loading = false; refreshButton.disabled = false; render();
  }

  async function loadProviderDetail(providerId: string): Promise<void> {
    detailLoading = true; renderDetail();
    try {
      observations = await fetchCapabilityObservations(providerId);
    } catch {
      observations = [];
    } finally {
      if (active && selectedId === providerId) { detailLoading = false; renderDetail(); }
    }
  }

  const handleSearch = () => { query = searchInput.value; render(); };
  searchInput.addEventListener("input", handleSearch);
  refreshButton.addEventListener("click", () => void load());
  replaceChildren(target, element("section", { className: "inbox-page", "aria-labelledby": "systems-heading" },
    element("header", { className: "inbox-heading compact-heading" }, element("div", {}, element("p", { className: "eyebrow" }, "Infrastructure"), element("h1", { id: "systems-heading" }, "Systems"), element("p", { className: "inbox-intro" }, "One health roster for every configured provider.")), refreshButton),
    summary, element("div", { className: "inbox-workspace" }, list, detail),
  ));
  void load().then(() => {
    const initialId = window.location.hash.startsWith("#systems/") ? decodeURIComponent(window.location.hash.slice("#systems/".length)) : "";
    if (initialId && providers.some((provider) => provider.id === initialId)) {
      selectedId = initialId;
      render();
      void loadProviderDetail(initialId);
    }
  });
  return () => { active = false; searchInput.removeEventListener("input", handleSearch); };
}
