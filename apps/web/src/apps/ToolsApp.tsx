import { useEffect, useMemo, useState } from "react";
import { Button, TextInput as Input } from "react95";
import { useQueryClient } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { fetchApprovals, fetchTools, requestApproval, runTool } from "../lib/api";
import {
  asList,
  asRecord,
  buildToolInput,
  defaultToolInput,
  describeError,
  formatBytes,
  hasMissingRequiredInput,
  isRecord,
  schemaType,
  text,
  toolInputProperties,
  toolInputRequired,
} from "../lib/ui";
import { usePanelQuery } from "../lib/usePanelQuery";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyState } from "../components/EmptyState";
import { LoadingIndicator } from "../components/LoadingIndicator";
import { PanelLoadingScreen } from "../components/PanelLoadingScreen";
import { SelectControl } from "../components/SelectControl";
import { StatusBadge } from "../components/StatusBadge";
import { KeyValueGrid, ResultTable } from "./shared";
import type { ToolDefinition, ToolRunResult } from "../lib/types";

// Write tools and high-risk tools only run with a consumed per-invocation
// approval; the backend enforces this, the UI drives the request/wait flow.
function needsApproval(tool: ToolDefinition): boolean {
  return tool.mode === "write" || tool.risk === "high";
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function ToolInputForm({
  tool,
  values,
  onChange,
  focusFirstMissing = false,
}: {
  tool: ToolDefinition;
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
  focusFirstMissing?: boolean;
}) {
  const properties = toolInputProperties(tool);
  if (!properties.length) {
    return null;
  }

  const required = new Set(toolInputRequired(tool));
  const firstMissingKey = focusFirstMissing
    ? properties.find(([key]) => required.has(key) && !(values[key] ?? "").trim())?.[0]
    : undefined;
  return (
    <div className="tool-input-grid">
      {properties.map(([key, schema]) => {
        const type = schemaType(schema);
        const title = text(schema.title, key.replace(/_/g, " "));
        const placeholder = type === "array" ? "comma separated" : required.has(key) ? "required" : "optional";
        return (
          <label className="tool-input-field" key={key}>
            <span>
              {title}
              {required.has(key) ? " *" : ""}
            </span>
            <Input
              fullWidth
              autoFocus={key === firstMissingKey}
              type={type === "integer" || type === "number" ? "number" : "text"}
              value={values[key] ?? ""}
              onChange={(event) => onChange(key, event.target.value)}
              placeholder={placeholder}
            />
          </label>
        );
      })}
    </div>
  );
}

export function ToolResultView({ result }: { result: ToolRunResult }) {
  if (!result.ok) {
    return (
      <div className="tool-result tool-result-error">
        <strong>{result.error.code}</strong>
        <span>{result.error.message}</span>
      </div>
    );
  }

  const payload = asRecord(result.result);
  const nodes = asList(payload.nodes);
  const guests = asList(payload.guests);
  const storage = asList(payload.storage);
  const firmware = asRecord(payload.firmware);
  const system = asRecord(payload.system);
  const interfaces = asRecord(payload.interfaces);
  const gateways = asList(asRecord(payload.gateways).items);
  const resources = asRecord(payload.resources);
  const memory = asRecord(resources.memory);
  const summary = asRecord(payload.summary);
  const agentSummary = "provider_id" in summary ? summary : {};
  const agentMetrics = asRecord(agentSummary.metrics);
  const agentFindings = asList(agentSummary.findings);
  const agentActions = Array.isArray(agentSummary.next_actions) ? agentSummary.next_actions.map((item) => text(item)) : [];
  const domainCounts = asRecord(summary.domains);
  const entities = asList(payload.entities);
  const problemEntities = asList(payload.problem_entities);
  const serviceDomains = asList(payload.domains);
  const logLines = Array.isArray(payload.lines) ? payload.lines.map((line) => text(line)) : [];
  const rawEvents = asList(payload.events);
  const frigateEvents = rawEvents.some((event) => "camera" in event || "label" in event) ? rawEvents : [];
  const logbookEvents = frigateEvents.length > 0 ? [] : rawEvents;
  const rawConfig = asRecord(payload.config);
  const frigateConfig =
    "cameras_total" in rawConfig || "safe_mode" in rawConfig || "mqtt_enabled" in rawConfig
      ? rawConfig
      : {};
  const frigateCameras = asList(payload.cameras);
  const frigateDetectors = asList(payload.detectors);
  const frigateReviews = asList(payload.reviews);
  const frigateService = asRecord(payload.service);
  const frigateVersion = typeof payload.version === "string" ? payload.version : "";
  const frigateSubLabels = Array.isArray(payload.sub_labels) ? payload.sub_labels.map((item) => text(item)) : [];

  return (
    <div className="tool-result">
      <div className="tool-result-meta">
        <span>ok</span>
        <span>{result.duration_ms} ms</span>
        <span>{result.invocation_id.slice(0, 8)}</span>
      </div>

      {nodes.length > 0 && (
        <ResultTable
          columns={[
            { key: "node", label: "Node" },
            { key: "status", label: "Status" },
            { key: "cpu_usage", label: "CPU" },
            { key: "memory", label: "Memory", render: (row) => `${formatBytes(row.memory_used_bytes)} / ${formatBytes(row.memory_total_bytes)}` },
            { key: "uptime_seconds", label: "Uptime" },
          ]}
          rows={nodes}
        />
      )}

      {guests.length > 0 && (
        <ResultTable
          columns={[
            { key: "vmid", label: "VMID" },
            { key: "name", label: "Name" },
            { key: "guest_type", label: "Type" },
            { key: "status", label: "Status" },
            { key: "node", label: "Node" },
            { key: "memory", label: "Memory", render: (row) => `${formatBytes(row.memory_used_bytes)} / ${formatBytes(row.memory_total_bytes)}` },
          ]}
          rows={guests}
        />
      )}

      {storage.length > 0 && (
        <ResultTable
          columns={[
            { key: "id", label: "Storage" },
            { key: "node", label: "Node" },
            { key: "storage_type", label: "Type" },
            { key: "used", label: "Used", render: (row) => `${formatBytes(row.used_bytes)} / ${formatBytes(row.total_bytes)}` },
            { key: "active", label: "Active" },
            { key: "shared", label: "Shared" },
          ]}
          rows={storage}
        />
      )}

      {Object.keys(firmware).length > 0 && (
        <KeyValueGrid
          items={[
            ["Version", firmware.os_version],
            ["Connection", firmware.connection],
            ["Needs reboot", firmware.needs_reboot === "1" ? "yes" : "no"],
            ["Last check", firmware.last_check],
            ["Updates", asList(firmware.new_packages).length],
          ]}
        />
      )}

      {Object.keys(system).length > 0 && (
        <KeyValueGrid items={[["Name", system.name], ["Versions", system.versions], ["Updates", system.updates]]} />
      )}

      {Object.keys(memory).length > 0 && (
        <KeyValueGrid items={[["Memory used", memory.used_frmt ? `${memory.used_frmt} MB` : memory.used], ["Memory total", memory.total_frmt ? `${memory.total_frmt} MB` : memory.total], ["ARC", memory.arc_txt]]} />
      )}

      {Object.keys(agentSummary).length > 0 && (
        <div className="agent-summary">
          <div className="agent-summary-head">
            <strong>{text(agentSummary.provider_id)} summary</strong>
            <span className={`summary-badge summary-${text(agentSummary.severity, "unknown")}`}>{text(agentSummary.severity)}</span>
            <span className="summary-badge">{text(agentSummary.status)}</span>
          </div>
          <KeyValueGrid
            items={Object.entries(agentMetrics)
              .filter(([, value]) => !Array.isArray(value) && !isRecord(value))
              .slice(0, 12)
              .map(([key, value]) => [key.replace(/_/g, " "), value])}
          />
          {agentFindings.length > 0 && (
            <ResultTable
              columns={[
                { key: "severity", label: "Severity" },
                { key: "message", label: "Finding" },
              ]}
              rows={agentFindings}
            />
          )}
          {agentActions.length > 0 && <pre className="tool-output">{agentActions.join("\n")}</pre>}
        </div>
      )}

      {Object.keys(summary).length > 0 && Object.keys(agentSummary).length === 0 && (
        <>
          <KeyValueGrid
            items={[
              ["Entities", summary.entities_total],
              ["Problem entities", summary.problem_entities],
              ["Domains", Object.keys(domainCounts).length],
            ]}
          />
          <ResultTable
            columns={[
              { key: "domain", label: "Domain" },
              { key: "count", label: "Entities" },
            ]}
            rows={Object.entries(domainCounts)
              .map(([domain, count]) => ({ domain, count }))
              .sort((a, b) => Number(b.count) - Number(a.count))
              .slice(0, 24)}
          />
        </>
      )}

      {entities.length > 0 && (
        <ResultTable
          columns={[
            { key: "entity_id", label: "Entity" },
            { key: "friendly_name", label: "Name" },
            { key: "state", label: "State" },
            { key: "last_changed", label: "Changed" },
          ]}
          rows={entities}
        />
      )}

      {problemEntities.length > 0 && (
        <ResultTable
          columns={[
            { key: "entity_id", label: "Problem entity" },
            { key: "friendly_name", label: "Name" },
            { key: "state", label: "State" },
            { key: "last_changed", label: "Changed" },
          ]}
          rows={problemEntities}
        />
      )}

      {serviceDomains.length > 0 && (
        <ResultTable
          columns={[
            { key: "domain", label: "Domain" },
            { key: "count", label: "Services" },
            { key: "services", label: "Names", render: (row) => (Array.isArray(row.services) ? row.services.map((item) => text(item)).join(", ") : "—") },
          ]}
          rows={serviceDomains}
        />
      )}

      {logLines.length > 0 && (
        <pre className="tool-output">{logLines.join("\n")}</pre>
      )}

      {frigateVersion && <KeyValueGrid items={[["Frigate version", frigateVersion]]} />}

      {Object.keys(frigateConfig).length > 0 && (
        <KeyValueGrid
          items={[
            ["Version", frigateConfig.version],
            ["Safe mode", frigateConfig.safe_mode],
            ["MQTT", frigateConfig.mqtt_enabled],
            ["Record", frigateConfig.record_enabled],
            ["Snapshots", frigateConfig.snapshots_enabled],
            ["Cameras", frigateConfig.cameras_total],
          ]}
        />
      )}

      {Object.keys(frigateService).length > 0 && (
        <KeyValueGrid
          items={[
            ["Detection FPS", frigateService.detection_fps],
            ["Process uptime", frigateService.process_uptime],
          ]}
        />
      )}

      {frigateCameras.length > 0 && (
        <ResultTable
          columns={[
            { key: "name", label: "Camera" },
            { key: "enabled", label: "Enabled" },
            { key: "detect_enabled", label: "Detect" },
            { key: "detect_fps", label: "Detect FPS" },
            { key: "camera_fps", label: "Camera FPS" },
            { key: "process_fps", label: "Process FPS" },
            { key: "zones", label: "Zones", render: (row) => (Array.isArray(row.zones) ? row.zones.map((item) => text(item)).join(", ") : "—") },
          ]}
          rows={frigateCameras}
        />
      )}

      {frigateDetectors.length > 0 && (
        <ResultTable
          columns={[
            { key: "name", label: "Detector" },
            { key: "detection_start", label: "Detection start" },
            { key: "inference_speed", label: "Inference" },
            { key: "pid", label: "PID" },
          ]}
          rows={frigateDetectors}
        />
      )}

      {frigateEvents.length > 0 && (
        <ResultTable
          columns={[
            { key: "start_time", label: "Start time" },
            { key: "camera", label: "Camera" },
            { key: "label", label: "Label" },
            { key: "top_score", label: "Score" },
            { key: "has_clip", label: "Clip" },
            { key: "has_snapshot", label: "Snapshot" },
          ]}
          rows={frigateEvents}
        />
      )}

      {frigateReviews.length > 0 && (
        <ResultTable
          columns={[
            { key: "start_time", label: "Start time" },
            { key: "camera", label: "Camera" },
            { key: "severity", label: "Severity" },
            { key: "detections", label: "Detections" },
            { key: "objects", label: "Objects", render: (row) => (Array.isArray(row.objects) ? row.objects.map((item) => text(item)).join(", ") : "—") },
            { key: "zones", label: "Zones", render: (row) => (Array.isArray(row.zones) ? row.zones.map((item) => text(item)).join(", ") : "—") },
          ]}
          rows={frigateReviews}
        />
      )}

      {frigateSubLabels.length > 0 && <pre className="tool-output">{frigateSubLabels.join("\n")}</pre>}

      {logbookEvents.length > 0 && (
        <ResultTable
          columns={[
            { key: "when", label: "When" },
            { key: "entity_id", label: "Entity" },
            { key: "name", label: "Name" },
            { key: "state", label: "State" },
            { key: "message", label: "Message" },
          ]}
          rows={logbookEvents}
        />
      )}

      {Object.keys(interfaces).length > 0 && (
        <ResultTable
          columns={[
            { key: "name", label: "Interface" },
            { key: "label", label: "Label" },
          ]}
          rows={Object.entries(interfaces).map(([name, label]) => ({ name, label }))}
        />
      )}

      {gateways.length > 0 && (
        <ResultTable
          columns={[
            { key: "name", label: "Gateway" },
            { key: "address", label: "Address" },
            { key: "status_translated", label: "Status" },
            { key: "loss", label: "Loss" },
            { key: "delay", label: "Delay" },
          ]}
          rows={gateways}
        />
      )}

      {!nodes.length &&
        !guests.length &&
        !storage.length &&
        !Object.keys(firmware).length &&
        !Object.keys(system).length &&
        !Object.keys(memory).length &&
        !Object.keys(summary).length &&
        !entities.length &&
        !problemEntities.length &&
        !serviceDomains.length &&
        !logLines.length &&
        !Object.keys(frigateConfig).length &&
        !frigateVersion &&
        !frigateSubLabels.length &&
        !Object.keys(frigateService).length &&
        !frigateCameras.length &&
        !frigateDetectors.length &&
        !frigateEvents.length &&
        !frigateReviews.length &&
        !logbookEvents.length &&
        !Object.keys(interfaces).length &&
        !gateways.length && <pre className="tool-output">{JSON.stringify(result, null, 2)}</pre>}
    </div>
  );
}

export function ToolsApp() {
  const queryClient = useQueryClient();
  const { data, loadState, isFetching, errorMessage, refresh } = usePanelQuery(["tools"], fetchTools);
  const tools = useMemo(() => data ?? [], [data]);
  const [runningToolId, setRunningToolId] = useState<string | null>(null);
  const [approvalWaitToolId, setApprovalWaitToolId] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, ToolRunResult>>({});
  const [pendingConfirm, setPendingConfirm] = useState<ToolDefinition | null>(null);
  const [expandedToolIds, setExpandedToolIds] = useState<Set<string>>(() => new Set());
  const [toolQuery, setToolQuery] = useState("");
  const [providerFilter, setProviderFilter] = useState("");
  const [riskFilter, setRiskFilter] = useState("");
  const [availabilityFilter, setAvailabilityFilter] = useState("");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [toolInputs, setToolInputs] = useState<Record<string, Record<string, string>>>({});

  useEffect(() => {
    if (!tools.length) return;
    setToolInputs((current) => {
      const next = { ...current };
      for (const tool of tools) {
        next[tool.id] = next[tool.id] ?? defaultToolInput(tool);
      }
      return next;
    });
  }, [tools]);

  async function waitForApprovalDecision(approvalId: string, expiresAt: string): Promise<string> {
    const deadline = new Date(expiresAt).getTime() + 5_000;
    while (Date.now() < deadline) {
      await sleep(3_000);
      const approvals = await fetchApprovals({ limit: 100 });
      const current = approvals.find((approval) => approval.id === approvalId);
      if (current && current.status !== "pending") {
        return current.status;
      }
    }
    return "expired";
  }

  async function execute(tool: ToolDefinition) {
    setPendingConfirm(null);
    setExpandedToolIds((current) => {
      const next = new Set(current);
      next.add(tool.id);
      return next;
    });
    setRunningToolId(tool.id);
    try {
      const input = buildToolInput(tool, toolInputs[tool.id] ?? {});
      let approvalId: string | undefined;
      if (needsApproval(tool)) {
        setApprovalWaitToolId(tool.id);
        const approval = await requestApproval(tool.id, input);
        void queryClient.invalidateQueries({ queryKey: ["approvals"] });
        const decision = await waitForApprovalDecision(approval.id, approval.expires_at);
        setApprovalWaitToolId(null);
        if (decision !== "approved") {
          setResults((current) => ({
            ...current,
            [tool.id]: {
              ok: false,
              error:
                decision === "denied"
                  ? { code: "approval_denied", message: "The operator denied the request." }
                  : { code: "approval_expired", message: "The request expired without a decision." },
            },
          }));
          return;
        }
        approvalId = approval.id;
      }
      const result = await runTool(tool.id, input, approvalId);
      setResults((current) => ({ ...current, [tool.id]: result }));
    } catch (error) {
      setResults((current) => ({
        ...current,
        [tool.id]: {
          ok: false,
          error: { code: "client_error", message: describeError(error) },
        },
      }));
    } finally {
      setRunningToolId(null);
      setApprovalWaitToolId(null);
      void queryClient.invalidateQueries({ queryKey: ["audit"] });
      void queryClient.invalidateQueries({ queryKey: ["approvals"] });
    }
  }

  function updateToolInput(toolId: string, key: string, value: string) {
    setToolInputs((current) => ({
      ...current,
      [toolId]: {
        ...(current[toolId] ?? {}),
        [key]: value,
      },
    }));
  }

  function handleToolClick(tool: ToolDefinition) {
    if (!tool.enabled) {
      setExpandedToolIds((current) => {
        const next = new Set(current);
        next.add(tool.id);
        return next;
      });
      return;
    }
    if (hasMissingRequiredInput(tool, toolInputs[tool.id] ?? {}) || tool.requires_confirmation || needsApproval(tool)) {
      setPendingConfirm(tool);
      return;
    }
    void execute(tool);
  }

  function closeToolResult(toolId: string) {
    setExpandedToolIds((current) => {
      const next = new Set(current);
      next.delete(toolId);
      return next;
    });
  }

  const filteredTools = useMemo(() => {
    const query = toolQuery.trim().toLowerCase();
    return tools.filter((tool) => {
      if (providerFilter && tool.provider_id !== providerFilter) return false;
      if (riskFilter && tool.risk !== riskFilter) return false;
      if (availabilityFilter === "available" && !tool.enabled) return false;
      if (availabilityFilter === "unavailable" && tool.enabled) return false;
      const haystack = [
        tool.id,
        tool.name,
        tool.description,
        tool.provider_id,
        tool.category,
        tool.mode,
        tool.risk,
      ].join(" ").toLowerCase();
      return !query || haystack.includes(query);
    });
  }, [tools, toolQuery, providerFilter, riskFilter, availabilityFilter]);

  const providerOptions = useMemo(() => [...new Set(tools.map((tool) => tool.provider_id))].sort(), [tools]);

  const grouped = useMemo(() => {
    const map = new Map<string, ToolDefinition[]>();
    for (const tool of filteredTools) {
      const list = map.get(tool.provider_id) ?? [];
      list.push(tool);
      map.set(tool.provider_id, list);
    }
    return Array.from(map.entries());
  }, [filteredTools]);

  if (loadState === "loading") {
    return <PanelLoadingScreen label="Loading tool catalog…" />;
  }

  return (
    <div className="panel-app mcp-app">
      <div className="panel-toolbar">
        <Button onClick={refresh} disabled={isFetching}>
          Refresh
        </Button>
        <label className="toolbar-search">
          <Search size={15} aria-hidden="true" />
          <Input
            fullWidth
            aria-label="Search tools"
            placeholder="Search tools"
            value={toolQuery}
            onChange={(event) => setToolQuery(event.target.value)}
          />
        </label>
        <span className="toolbar-count">
          {filteredTools.length}/{tools.length}
        </span>
        <Button className="mobile-filter-toggle" type="button" aria-expanded={filtersOpen} onClick={() => setFiltersOpen((open) => !open)}>
          {filtersOpen ? "Close filters" : "Filter"}
        </Button>
        <div className={`tool-filter-row mobile-filter-controls ${filtersOpen ? "mobile-filter-controls-open" : ""}`}>
          <SelectControl aria-label="Filter by provider" value={providerFilter} onChange={(event) => setProviderFilter(event.target.value)}>
            <option value="">All providers</option>{providerOptions.map((provider) => <option key={provider}>{provider}</option>)}
          </SelectControl>
          <SelectControl aria-label="Filter by risk" value={riskFilter} onChange={(event) => setRiskFilter(event.target.value)}>
            <option value="">All risks</option><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option><option value="critical">Critical</option>
          </SelectControl>
          <SelectControl aria-label="Filter by availability" value={availabilityFilter} onChange={(event) => setAvailabilityFilter(event.target.value)}>
            <option value="">Any availability</option><option value="available">Available</option><option value="unavailable">Unavailable</option>
          </SelectControl>
        </div>
      </div>
      <Button className="mobile-list-cta" type="button" onClick={() => document.getElementById("tool-catalog-groups")?.scrollIntoView({ behavior: "smooth", block: "start" })}>
        View {filteredTools.length} tools in {grouped.length} groups ↓
      </Button>
      {loadState === "error" && <p className="login-error">{errorMessage}</p>}
      <div className="tool-groups" id="tool-catalog-groups">
        {grouped.map(([providerId, providerTools]) => (
          <details className="tool-group tool-provider-group" key={providerId}>
            <summary className="tool-provider-summary">
              <strong>{providerId}</strong>
              <span>{providerTools.length} tools available</span>
            </summary>
            <div className="tool-list">
              {providerTools.map((tool) => {
                const runnable = tool.enabled;
                const values = toolInputs[tool.id] ?? {};
                const missingInput = hasMissingRequiredInput(tool, values);
                const result = results[tool.id];
                const requiredInputs = toolInputRequired(tool);
                return (
                  <details
                    className={`tool-row ${runnable ? "" : "tool-row-disabled"}`}
                    key={tool.id}
                    open={expandedToolIds.has(tool.id)}
                  >
                    <summary
                      className="tool-row-summary"
                      onClick={(event) => {
                        event.preventDefault();
                        if (runningToolId !== tool.id) handleToolClick(tool);
                      }}
                    >
                      <StatusBadge label={`Risk ${{ low: "low", medium: "medium", high: "high", critical: "critical" }[tool.risk] ?? tool.risk}`} tone={tool.risk === "low" ? "success" : tool.risk === "medium" ? "warning" : "danger"} />
                      <div>
                        <strong>{tool.name}</strong>
                        <span className="clamp-one-line">{tool.description}</span>
                        <small className="metadata-line">{tool.mode === "read" ? "Read-only — does not modify systems" : "Write — may modify systems"}{needsApproval(tool) ? " · approval required" : ""} · {requiredInputs.length ? `${requiredInputs.length} required inputs` : "no required inputs"}{!tool.enabled ? " · unavailable" : ""}</small>
                      </div>
                      <span className="tool-expand-label">
                        {!runnable
                          ? "Unavailable"
                          : approvalWaitToolId === tool.id
                            ? "Waiting for approval…"
                            : runningToolId === tool.id
                              ? "Running…"
                              : missingInput
                                ? "Complete inputs"
                                : needsApproval(tool)
                                  ? "Request and run"
                                  : tool.requires_confirmation
                                    ? "Confirm and run"
                                    : result
                                       ? "Run again"
                                       : "Run"}
                      </span>
                    </summary>
                    <div className="tool-row-expanded">
                    <ToolInputForm tool={tool} values={values} onChange={(key, value) => updateToolInput(tool.id, key, value)} />
                    <div className="tool-row-actions">
                      <mark className={tool.mode === "write" ? "mode-write" : "mode-read"}>{tool.mode === "read" ? "Read-only" : "Write"}</mark>
                      <Button disabled={!runnable || missingInput || runningToolId === tool.id} onClick={() => handleToolClick(tool)} title={missingInput ? "Complete required inputs first" : undefined}>
                        {runningToolId === tool.id ? "Running…" : result ? "Run again" : "Run"}
                      </Button>
                      <Button disabled={runningToolId === tool.id} onClick={() => closeToolResult(tool.id)}>Close</Button>
                    </div>
                    {runningToolId === tool.id && (
                      <LoadingIndicator
                        label={
                          approvalWaitToolId === tool.id
                            ? "Waiting for your approval (Telegram or Approvals window)…"
                            : `Running ${tool.name}…`
                        }
                        size={18}
                      />
                    )}
                    {result && <ToolResultView result={result} />}
                    </div>
                  </details>
                );
              })}
            </div>
          </details>
        ))}
        {loadState === "ready" && tools.length === 0 && (
          <EmptyState title="No tools available" description="The execution core returned an empty catalog. Refresh after registering providers." actionLabel="Refresh catalog" onAction={refresh} />
        )}
        {loadState === "ready" && tools.length > 0 && filteredTools.length === 0 && (
          <EmptyState title="No matching tools" description={`No tools match “${toolQuery}”. Change or clear the search.`} />
        )}
      </div>
      {pendingConfirm && (
        <ConfirmDialog
          title={hasMissingRequiredInput(pendingConfirm, toolInputs[pendingConfirm.id] ?? {}) ? "Tool parameters" : needsApproval(pendingConfirm) ? "Request approval" : "Confirm tool execution"}
          message={
            needsApproval(pendingConfirm)
              ? `Run “${pendingConfirm.name}”? An approval request will be created: confirm it from the Telegram buttons or the Approvals window, then the tool starts automatically with the exact approved input.`
              : pendingConfirm.requires_confirmation
                ? `Run “${pendingConfirm.name}”? This action requires confirmation.`
                : `Complete the required inputs to run “${pendingConfirm.name}”.`
          }
          confirmLabel={needsApproval(pendingConfirm) ? "Request and run" : "Run"}
          confirmDisabled={hasMissingRequiredInput(pendingConfirm, toolInputs[pendingConfirm.id] ?? {})}
          onConfirm={() => execute(pendingConfirm)}
          onCancel={() => setPendingConfirm(null)}
        >
          <ToolInputForm
            tool={pendingConfirm}
            values={toolInputs[pendingConfirm.id] ?? {}}
            onChange={(key, value) => updateToolInput(pendingConfirm.id, key, value)}
            focusFirstMissing
          />
        </ConfirmDialog>
      )}
    </div>
  );
}
