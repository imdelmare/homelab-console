import { fetchApprovals, fetchTools, requestApproval, runTool } from "../src/lib/api";
import { buildToolInput, defaultToolInput, hasMissingRequiredInput, schemaType, toolInputProperties, toolInputRequired } from "../src/lib/ui";
import type { Approval, ToolDefinition } from "../src/lib/types";
import { button, element, replaceChildren } from "./dom";
import { confirmAction } from "./modal";

function needsApproval(tool: ToolDefinition): boolean {
  return tool.mode === "write" || tool.risk === "high" || tool.risk === "critical";
}

export function mountTools(target: HTMLElement, searchInput: HTMLInputElement): () => void {
  let tools: ToolDefinition[] = [];
  let selectedId: string | null = null;
  let query = "";
  let providerFilter = "";
  let modeFilter = "";
  let riskFilter = "";
  let availabilityFilter = "enabled";
  let loading = true;
  let running = false;
  let statusMessage = "";
  let errorMessage = "";
  let result: unknown = null;
  let active = true;
  const refreshButton = button("Refresh", "quiet-button");
  const filters = element("div", { className: "tool-filter-bar", "aria-label": "Tool catalog filters" });
  const summary = element("p", { className: "result-summary" });
  const list = element("div", { className: "record-list" });
  const detail = element("aside", { className: "record-detail tool-detail", "aria-label": "Selected tool" });

  async function waitForApproval(request: Approval): Promise<Approval> {
    const expiresAt = Date.parse(request.expires_at);
    while (active && Date.now() < expiresAt) {
      await new Promise((resolve) => window.setTimeout(resolve, 3_000));
      const approvals = await fetchApprovals({ limit: 100 });
      const current = approvals.find((approval) => approval.id === request.id);
      if (current && current.status !== "pending") return current;
    }
    throw new Error("Approval expired before a decision was received.");
  }

  function renderToolForm(tool: ToolDefinition): HTMLElement {
    const values = defaultToolInput(tool);
    const required = new Set(toolInputRequired(tool));
    const form = element("form", { className: "tool-form" });
    for (const [key, schema] of toolInputProperties(tool)) {
      const type = schemaType(schema);
      const description = typeof schema.description === "string" ? schema.description : "";
      const input = type === "boolean"
        ? element("select", { className: "control-input", name: key }, element("option", { value: values[key] || "" }, values[key] ? values[key] : "Choose…"), element("option", { value: "true" }, "True"), element("option", { value: "false" }, "False"))
        : element("input", { className: "control-input", name: key, value: values[key] || "", placeholder: type === "array" ? "Comma-separated values" : description, required: required.has(key) });
      form.append(element("label", { className: "control-field" }, element("span", {}, `${key}${required.has(key) ? " *" : ""}`), input, description ? element("small", {}, description) : null));
    }
    const execute = element("button", { className: "primary-action", type: "submit" }, running ? "Running…" : needsApproval(tool) ? "Request approval & run" : "Run tool");
    execute.disabled = running || !tool.enabled;
    form.append(execute);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formData = new FormData(form);
      const rawValues = Object.fromEntries([...formData.entries()].map(([key, value]) => [key, String(value)]));
      errorMessage = ""; result = null;
      if (hasMissingRequiredInput(tool, rawValues)) { errorMessage = "Complete every required input."; renderDetail(); return; }
      let input: Record<string, unknown>;
      try { input = buildToolInput(tool, rawValues); }
      catch (error) { errorMessage = error instanceof Error ? error.message : "Invalid tool input."; renderDetail(); return; }
      const confirmationNeeded = tool.requires_confirmation || needsApproval(tool);
      if (confirmationNeeded && !await confirmAction("Confirm tool execution", `${tool.name} (${tool.id}) will run with the exact values shown. ${needsApproval(tool) ? "An operator approval will be requested before execution." : ""}`, needsApproval(tool) ? "Request approval" : "Run")) return;
      running = true; statusMessage = needsApproval(tool) ? "Waiting for operator approval…" : "Executing through the shared tool core…"; renderDetail();
      try {
        let approvalId: string | undefined;
        if (needsApproval(tool)) {
          const requested = await requestApproval(tool.id, input);
          const decision = await waitForApproval(requested);
          if (decision.status !== "approved") throw new Error(`Approval ${decision.status}.`);
          approvalId = decision.id;
          statusMessage = "Approval received. Executing exact request…"; renderDetail();
        }
        const execution = await runTool(tool.id, input, approvalId);
        if (!execution.ok) throw new Error(execution.error.message);
        result = execution.result;
        statusMessage = `Completed in ${execution.duration_ms} ms · ${execution.invocation_id}`;
      } catch (error) { errorMessage = error instanceof Error ? error.message : "Tool execution failed."; statusMessage = ""; }
      finally { running = false; if (active) renderDetail(); }
    });
    return form;
  }

  function renderDetail(): void {
    const tool = tools.find((item) => item.id === selectedId);
    if (!tool) { replaceChildren(detail, element("p", { className: "detail-index" }, "EXECUTION CORE"), element("h2", {}, "Select a tool"), element("p", { className: "detail-empty" }, "Every call uses the same validation, redaction, audit, and approval boundary.")); return; }
    replaceChildren(detail, element("button", { className: "detail-close", type: "button", "aria-label": "Close details" }, "×"), element("p", { className: `item-kind ${tool.mode === "write" ? "state-critical" : "state-healthy"}` }, `${tool.mode} · ${tool.risk}`), element("h2", {}, tool.name), element("p", { className: "detail-description" }, tool.description), element("p", { className: "tool-id" }, tool.id), renderToolForm(tool), statusMessage ? element("p", { className: "operation-status" }, statusMessage) : null, errorMessage ? element("p", { className: "error-banner", role: "alert" }, errorMessage) : null, result !== null ? element("pre", { className: "metadata-output tool-result" }, JSON.stringify(result, null, 2)) : null);
    detail.querySelector<HTMLButtonElement>(".detail-close")?.addEventListener("click", () => { selectedId = null; result = null; render(); });
  }

  function renderFilters(): void {
    const providers = [...new Set(tools.map((tool) => tool.provider_id))].sort();
    const risks = [...new Set(tools.map((tool) => tool.risk))].sort();
    const selectFilter = (label: string, value: string, options: Array<[string, string]>, update: (next: string) => void) => {
      const select = element("select", { className: "control-input", "aria-label": label }, ...options.map(([optionValue, optionLabel]) => element("option", { value: optionValue }, optionLabel)));
      select.value = value;
      select.addEventListener("change", () => update(select.value));
      return element("label", { className: "tool-filter" }, element("span", {}, label), select);
    };
    replaceChildren(filters,
      selectFilter("Provider", providerFilter, [["", "All providers"], ...providers.map((provider): [string, string] => [provider, provider])], (value) => { providerFilter = value; render(); }),
      selectFilter("Mode", modeFilter, [["", "Read and write"], ["read", "Read only"], ["write", "Write only"]], (value) => { modeFilter = value; render(); }),
      selectFilter("Risk", riskFilter, [["", "All risk levels"], ...risks.map((risk): [string, string] => [risk, risk])], (value) => { riskFilter = value; render(); }),
      selectFilter("Availability", availabilityFilter, [["enabled", "Enabled"], ["disabled", "Disabled"], ["", "All entries"]], (value) => { availabilityFilter = value; render(); }),
    );
  }
  function render(): void {
    renderFilters();
    if (loading) { summary.textContent = "Loading governed catalog…"; replaceChildren(list, element("div", { className: "loading-state" }, "Loading tools")); renderDetail(); return; }
    const normalized = query.trim().toLowerCase();
    const visible = tools.filter((tool) =>
      (!providerFilter || tool.provider_id === providerFilter) &&
      (!modeFilter || tool.mode === modeFilter) &&
      (!riskFilter || tool.risk === riskFilter) &&
      (!availabilityFilter || (availabilityFilter === "enabled" ? tool.enabled : !tool.enabled)) &&
      (!normalized || [tool.name, tool.id, tool.description, tool.provider_id, tool.category].join(" ").toLowerCase().includes(normalized))
    );
    summary.textContent = `${visible.length} shown · ${tools.filter((tool) => tool.enabled).length} enabled · ${tools.filter((tool) => tool.mode === "write").length} write · ${tools.length} total`;
    replaceChildren(list, ...(visible.length ? visible.map((tool, index) => {
      const row = button(tool.name, `record-row${selectedId === tool.id ? " record-row--selected" : ""}`);
      replaceChildren(row, element("span", { className: `state-dot state-dot--${!tool.enabled ? "neutral" : tool.mode === "write" ? "warning" : "healthy"}` }), element("span", { className: "row-index" }, String(index + 1).padStart(2, "0")), element("span", { className: "record-copy" }, element("strong", {}, tool.name), element("small", {}, tool.id)), element("span", { className: "state-label" }, tool.risk), element("span", { className: "record-meta" }, tool.provider_id), element("span", { className: "row-arrow" }, "↗"));
      row.addEventListener("click", () => { selectedId = tool.id; result = null; errorMessage = ""; statusMessage = ""; render(); }); return row;
    }) : [element("p", { className: "empty-state" }, "No tools match this view.")])); renderDetail();
  }
  async function load(): Promise<void> { loading = true; refreshButton.disabled = true; render(); try { tools = await fetchTools(); errorMessage = ""; } catch (error) { errorMessage = error instanceof Error ? error.message : "Unable to load tools."; } finally { if (active) { loading = false; refreshButton.disabled = false; render(); } } }
  const handleSearch = () => { query = searchInput.value; render(); };
  searchInput.addEventListener("input", handleSearch); refreshButton.addEventListener("click", () => void load());
  replaceChildren(target, element("section", { className: "inbox-page", "aria-labelledby": "tools-heading" }, element("header", { className: "inbox-heading compact-heading" }, element("div", {}, element("p", { className: "eyebrow" }, "Governed catalog"), element("h1", { id: "tools-heading" }, "Tools"), element("p", { className: "inbox-intro" }, "Read and write capabilities behind one execution boundary.")), refreshButton), filters, summary, element("div", { className: "inbox-workspace" }, list, detail)));
  void load();
  return () => { active = false; searchInput.removeEventListener("input", handleSearch); };
}
