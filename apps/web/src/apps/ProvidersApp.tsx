import { useMemo, useState } from "react";
import { Button, Checkbox, GroupBox as Fieldset, TextInput as Input, Window, WindowContent, WindowHeader } from "react95";
import { useQueryClient } from "@tanstack/react-query";
import {
  createProviderTask,
  fetchCapabilityObservations,
  fetchProviderDefinitions,
  fetchProviders,
  fetchTools,
  fetchTopology,
} from "../lib/api";
import { formatDateTime } from "../lib/format";
import {
  buildApiReadyYaml,
  providerFamily,
  SPECIAL_PROVIDER_PROTOCOLS,
  validateApiReadyDraft,
} from "../lib/providerManager";
import type { ApiReadyDraft, ProviderFamily } from "../lib/providerManager";
import { describeError } from "../lib/ui";
import { combineLoadStates, usePanelQuery } from "../lib/usePanelQuery";
import { StatusLed } from "../components/StatusLed";
import { LoadingIndicator } from "../components/LoadingIndicator";
import { PanelLoadingScreen } from "../components/PanelLoadingScreen";
import { EmptyState } from "../components/EmptyState";
import { StatusBadge } from "../components/StatusBadge";
import { KeyValueGrid, ProviderIcon } from "./shared";
import type {
  CapabilityObservation,
  Provider,
} from "../lib/types";

type ProviderTab = "overview" | "capabilities" | "observations" | "integration";

const FAMILY_ORDER: ProviderFamily[] = ["standard", "standard-tcp", "api-ready", "special"];
const PROVIDER_STATUS_LABELS: Record<string, string> = { healthy: "Operational", degraded: "Warning", unreachable: "Unreachable", unavailable: "Unavailable", misconfigured: "Misconfigured", unknown: "Unknown status" };
const FAMILY_LABELS: Record<ProviderFamily, string> = {
  standard: "Standard HTTP",
  "standard-tcp": "Standard TCP",
  "api-ready": "API-ready",
  special: "Special protocols",
};

const PROVIDER_RUNBOOK_HINTS: Record<string, string> = {
  adguard: "dns_alert",
  cloudflaretunnel: "connectivity_alert",
  nutups: "power_alert",
  opnsense: "gateway_alert / connectivity_alert",
  pbs: "backup_alert",
  uptimekuma: "watcher-created task context",
  vps: "connectivity_alert",
};

const EMPTY_DRAFT: ApiReadyDraft = {
  id: "",
  name: "",
  baseUrl: "https://",
  verifyTls: true,
  timeoutSeconds: 5,
};

function ApiReadyWizard({
  existingIds,
  onClose,
}: {
  existingIds: ReadonlySet<string>;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState<ApiReadyDraft>(EMPTY_DRAFT);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState("");
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  function update<K extends keyof ApiReadyDraft>(key: K, value: ApiReadyDraft[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
    setErrors((current) => ({ ...current, [key]: "" }));
    setPreview("");
    setCopyState("idle");
  }

  function generatePreview() {
    const validation = validateApiReadyDraft(draft, existingIds);
    setErrors(validation);
    if (Object.keys(validation).length > 0) return;
    setPreview(buildApiReadyYaml(draft));
  }

  async function copyPreview() {
    try {
      await navigator.clipboard.writeText(preview);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  }

  return (
    <div className="modal-overlay" role="presentation" onMouseDown={onClose}>
      <Window
        className="window provider-wizard react95-window"
        role="dialog"
        aria-modal="true"
        aria-labelledby="provider-wizard-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <WindowHeader active className="title-bar">
          <span className="window-title" id="provider-wizard-title">New API-ready provider</span>
          <Button className="title-control" size="sm" square aria-label="Close" onClick={onClose}>×</Button>
        </WindowHeader>
        <WindowContent className="window-body provider-wizard-body">
          <p className="provider-wizard-intro">
            Generate a server-side <code>json_health_v1</code> declaration. The driver only reads
            <code> GET /health</code>; this window never contacts the URL entered.
          </p>
          <Fieldset label="Provider connection" className="provider-wizard-grid">
            <label>
              Provider ID
              <Input
                value={draft.id}
                placeholder="paperless"
                onChange={(event) => update("id", event.target.value)}
                aria-invalid={Boolean(errors.id)}
              />
              {errors.id && <small className="field-error">{errors.id}</small>}
            </label>
            <label>
              Display name
              <Input
                value={draft.name}
                placeholder="Paperless NGX"
                onChange={(event) => update("name", event.target.value)}
                aria-invalid={Boolean(errors.name)}
              />
              {errors.name && <small className="field-error">{errors.name}</small>}
            </label>
            <label className="provider-wizard-wide">
              Base URL
              <Input
                value={draft.baseUrl}
                inputMode="url"
                placeholder="https://service.internal"
                onChange={(event) => update("baseUrl", event.target.value)}
                aria-invalid={Boolean(errors.baseUrl)}
              />
              {errors.baseUrl && <small className="field-error">{errors.baseUrl}</small>}
            </label>
            <label>
              Timeout (seconds)
              <Input
                type="number"
                min="0.5"
                max="30"
                step="0.5"
                value={draft.timeoutSeconds}
                onChange={(event) => update("timeoutSeconds", Number(event.target.value))}
                aria-invalid={Boolean(errors.timeoutSeconds)}
              />
              {errors.timeoutSeconds && <small className="field-error">{errors.timeoutSeconds}</small>}
            </label>
            <div className="provider-wizard-checkbox">
              <Checkbox
                label="Verify TLS certificates"
                checked={draft.verifyTls}
                onChange={(event) => update("verifyTls", event.target.checked)}
              />
            </div>
          </Fieldset>
          {preview && (
            <div className="provider-yaml-preview">
              <div>
                <strong>homelab.local.yml</strong>
                <span>Restart API and MCP after adding this block.</span>
              </div>
              <pre>{preview}</pre>
              <p>
                Optional bearer token: add <code>{draft.id}.bearer_token</code> to the Git-excluded
                <code> secrets.local.yml</code> file. Never paste it here.
              </p>
            </div>
          )}
          {copyState === "failed" && (
            <p className="login-error">Copy to clipboard failed. Select the YAML manually.</p>
          )}
          <div className="dialog-actions">
            {!preview ? (
              <Button type="button" onClick={generatePreview}>Validate and show preview</Button>
            ) : (
              <Button type="button" onClick={copyPreview}>
                {copyState === "copied" ? "Copied" : "Copy YAML"}
              </Button>
            )}
            <Button type="button" onClick={onClose}>Close</Button>
          </div>
        </WindowContent>
      </Window>
    </div>
  );
}

function ObservationList({ observations }: { observations: CapabilityObservation[] }) {
  if (observations.length === 0) {
    return <p className="provider-empty-state">No observations are linked to this provider.</p>;
  }
  return (
    <div className="provider-observation-list">
      {observations.map((observation) => (
        <article key={observation.id}>
          <StatusLed status={observation.status} />
          <div>
            <strong>{observation.label}</strong>
            <span>{observation.id}</span>
            <p>{observation.detail || "No details returned."}</p>
            {Object.keys(observation.summary).length > 0 && (
              <div className="provider-metadata">
                {Object.entries(observation.summary).map(([key, value]) => (
                  <span key={key}>{key}: {String(value ?? "n/a")}</span>
                ))}
              </div>
            )}
          </div>
        </article>
      ))}
    </div>
  );
}

export function ProvidersApp() {
  const queryClient = useQueryClient();
  const providersQuery = usePanelQuery(["providers"], fetchProviders);
  const definitionsQuery = usePanelQuery(["provider-definitions"], fetchProviderDefinitions, {
    refetchInterval: false,
  });
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<ProviderTab>("overview");
  const [showWizard, setShowWizard] = useState(false);
  const [mobileDetail, setMobileDetail] = useState(false);
  const needsObservationData = activeTab === "observations" || activeTab === "integration";
  const toolsQuery = usePanelQuery(["tools"], fetchTools, {
    enabled: activeTab === "capabilities",
    refetchInterval: false,
  });
  const observationsQuery = usePanelQuery(
    ["capability-observations"],
    () => fetchCapabilityObservations(),
    { enabled: needsObservationData, refetchInterval: 300_000 },
  );
  const topologyQuery = usePanelQuery(["topology"], fetchTopology, {
    enabled: needsObservationData,
    refetchInterval: 60_000,
  });
  const providers = useMemo(() => providersQuery.data ?? [], [providersQuery.data]);
  const definitions = useMemo(() => definitionsQuery.data ?? [], [definitionsQuery.data]);
  const observations = useMemo(() => observationsQuery.data ?? [], [observationsQuery.data]);
  const nodes = useMemo(() => topologyQuery.data?.nodes ?? [], [topologyQuery.data?.nodes]);
  const tools = useMemo(() => toolsQuery.data ?? [], [toolsQuery.data]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [creatingProviderTask, setCreatingProviderTask] = useState<string | null>(null);

  const definitionsById = useMemo(
    () => new Map(definitions.map((definition) => [definition.id, definition])),
    [definitions],
  );
  const groupedProviders = useMemo(() => {
    const groups: Record<ProviderFamily, Provider[]> = {
      standard: [],
      "standard-tcp": [],
      "api-ready": [],
      special: [],
    };
    for (const provider of providers) {
      groups[providerFamily(provider, definitionsById.get(provider.id))].push(provider);
    }
    for (const family of FAMILY_ORDER) {
      groups[family].sort((left, right) => left.name.localeCompare(right.name));
    }
    return groups;
  }, [definitionsById, providers]);
  const fallbackProvider = providers.find((provider) => provider.status !== "healthy") ?? providers[0];
  const selectedProvider =
    providers.find((provider) => provider.id === selectedProviderId) ?? fallbackProvider ?? null;
  const selectedDefinition = selectedProvider
    ? definitionsById.get(selectedProvider.id)
    : undefined;
  const selectedFamily = selectedProvider
    ? providerFamily(selectedProvider, selectedDefinition)
    : "standard";
  const selectedNodes = selectedProvider
    ? nodes.filter((node) => node.provider_id === selectedProvider.id)
    : [];
  const linkedObservationIds = new Set(
    selectedNodes.flatMap((node) => [node.observation_id, node.availability_observation_id]).filter(Boolean),
  );
  const selectedObservations = selectedProvider
    ? observations.filter(
        (observation) =>
          observation.provider_id === selectedProvider.id || linkedObservationIds.has(observation.id),
      )
    : [];
  const selectedTools = selectedProvider
    ? tools.filter((tool) => tool.provider_id === selectedProvider.id)
    : [];
  const loadState = combineLoadStates(providersQuery.loadState, definitionsQuery.loadState);
  const issueCount = providers.filter((provider) => provider.status !== "healthy").length;
  const unreachableCount = providers.filter((provider) => provider.status === "unreachable").length;
  const degradedCount = providers.filter((provider) => provider.status === "degraded").length;
  const shownError = providersQuery.errorMessage ?? definitionsQuery.errorMessage ?? actionError;
  const existingIds = useMemo(() => new Set(providers.map((provider) => provider.id)), [providers]);

  function selectProvider(providerId: string) {
    setSelectedProviderId(providerId);
    setMobileDetail(true);
  }

  function refresh() {
    providersQuery.refresh();
    definitionsQuery.refresh();
    if (needsObservationData) {
      observationsQuery.refresh();
      topologyQuery.refresh();
    }
    if (activeTab === "capabilities") toolsQuery.refresh();
  }

  async function openProviderTask(provider: Provider) {
    setCreatingProviderTask(provider.id);
    setActionError(null);
    try {
      const task = await createProviderTask(provider.id);
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      void queryClient.invalidateQueries({ queryKey: ["audit"] });
      window.dispatchEvent(new CustomEvent("homelab:open-task", { detail: { taskId: task.id } }));
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setCreatingProviderTask(null);
    }
  }

  function openTopology() {
    window.dispatchEvent(new CustomEvent("homelab:open-topology"));
  }

  if (loadState === "loading") {
    return <PanelLoadingScreen label="Loading providers…" />;
  }

  return (
    <div className={`panel-app providers-app provider-manager ${mobileDetail ? "provider-mobile-detail" : ""}`}>
      <div className="panel-toolbar provider-manager-toolbar">
        <Button onClick={refresh} disabled={providersQuery.isFetching || definitionsQuery.isFetching}>
          Refresh
        </Button>
        <Button type="button" onClick={() => setShowWizard(true)}>New API-ready provider…</Button>
        <span className="toolbar-count">{issueCount} issues · {providers.length} total</span>
      </div>
      {shownError && <p className="login-error">{shownError}</p>}
      <div className="provider-summary">
        <span>Operational {providers.length - issueCount}</span>
        <span>Degraded {degradedCount}</span>
        <span>Unreachable {unreachableCount}</span>
        <span>Special {groupedProviders.special.length}</span>
      </div>
      <Button className="mobile-list-cta" type="button" onClick={() => { setMobileDetail(false); document.getElementById("provider-registry")?.scrollIntoView({ behavior: "smooth", block: "start" }); }}>
        View {providers.length} providers ↓
      </Button>
      <div className="provider-manager-workspace">
        <nav className="provider-manager-nav sunken-panel" id="provider-registry" aria-label="Provider families">
          {FAMILY_ORDER.map((family) => (
            <section key={family}>
              <h3>{FAMILY_LABELS[family]} <span>{groupedProviders[family].length}</span></h3>
              {groupedProviders[family].map((provider) => (
                <Button
                  type="button"
                  key={provider.id}
                  className={provider.id === selectedProvider?.id ? "provider-nav-selected" : ""}
                  onClick={() => selectProvider(provider.id)}
                >
                  <ProviderIcon providerId={provider.id} />
                  <span>
                    <strong>{provider.name}</strong>
                    <small>{provider.id}</small>
                  </span>
                  <StatusLed status={provider.status} />
                </Button>
              ))}
              {groupedProviders[family].length === 0 && <p>No providers registered.</p>}
            </section>
          ))}
        </nav>
        <section className="provider-manager-detail sunken-panel">
          {selectedProvider ? (
            <>
              <Button
                type="button"
                className="provider-mobile-back"
                onClick={() => setMobileDetail(false)}
              >
                ← Providers
              </Button>
              <header className="provider-detail-header">
                <ProviderIcon providerId={selectedProvider.id} />
                <div>
                  <h2>{selectedProvider.name}</h2>
                  <span>{selectedProvider.id}</span>
                </div>
                <div className="provider-detail-status">
                  <StatusLed status={selectedProvider.status} />
                  <strong>{PROVIDER_STATUS_LABELS[selectedProvider.status] ?? selectedProvider.status}</strong>
                  <mark className={`provider-family-badge provider-family-${selectedFamily}`}>
                    {FAMILY_LABELS[selectedFamily]}
                  </mark>
                </div>
              </header>
              <div className="provider-detail-summary" aria-label="Provider summary">
                <StatusBadge
                  label={PROVIDER_STATUS_LABELS[selectedProvider.status] ?? selectedProvider.status}
                  tone={selectedProvider.status === "healthy" ? "success" : selectedProvider.status === "degraded" ? "warning" : "danger"}
                />
                <span><strong>{selectedProvider.tool_count}</strong><small>tool</small></span>
                <span><strong>{selectedDefinition?.transport ?? "special"}</strong><small>transport</small></span>
                <span><strong>{formatDateTime(selectedProvider.checked_at)}</strong><small>last check</small></span>
                <span className="provider-summary-problem"><strong>{selectedProvider.last_error?.message ?? "No active issue"}</strong><small>operational status</small></span>
              </div>
              <div className="provider-detail-tabs" role="tablist" aria-label="Provider details">
                {([
                  ["overview", "Summary"],
                  ["capabilities", "Capabilities"],
                  ["observations", "Observations"],
                  ["integration", "Integration"],
                ] as const).map(([tab, label]) => (
                  <Button
                    type="button"
                    role="tab"
                    aria-selected={activeTab === tab}
                    className={activeTab === tab ? "pressed" : ""}
                    key={tab}
                    onClick={() => setActiveTab(tab)}
                  >
                    {label}
                  </Button>
                ))}
              </div>
              <div className="provider-detail-content">
                {activeTab === "overview" && (
                  <>
                    <KeyValueGrid
                      items={[
                        ["Status", PROVIDER_STATUS_LABELS[selectedProvider.status] ?? selectedProvider.status],
                        ["Driver", selectedDefinition?.driver_id ?? SPECIAL_PROVIDER_PROTOCOLS[selectedProvider.id] ?? "dedicated"],
                        ["Transport", selectedDefinition?.transport ?? "special"],
                        ["Last check", formatDateTime(selectedProvider.checked_at)],
                        ["Last successful result", formatDateTime(selectedProvider.last_ok_at)],
                        ["Tools", selectedProvider.tool_count],
                      ]}
                    />
                    {selectedProvider.detail && <p className="provider-detail-message">{selectedProvider.detail}</p>}
                    {selectedProvider.last_error && (() => {
                      const resolved =
                        selectedProvider.last_ok_at !== null &&
                        new Date(selectedProvider.last_ok_at) > new Date(selectedProvider.last_error.at);
                      if (resolved) {
                        return (
                          <details className="provider-last-error resolved">
                            <summary>
                              Previous error resolved · {formatDateTime(selectedProvider.last_error.at)}
                            </summary>
                            <p>{selectedProvider.last_error.message}</p>
                          </details>
                        );
                      }
                      return (
                        <div className="provider-last-error">
                          <strong>Active error</strong>
                          <span>{selectedProvider.last_error.status} · {formatDateTime(selectedProvider.last_error.at)}</span>
                          <p>{selectedProvider.last_error.message}</p>
                        </div>
                      );
                    })()}
                    {selectedFamily === "special" && (
                      <div className="provider-special-note">
                        Provider based on a specific protocol, intentionally kept separate from shared HTTP and API-ready drivers.
                      </div>
                    )}
                    {selectedProvider.status !== "healthy" && (
                      <Button
                        type="button"
                        disabled={creatingProviderTask === selectedProvider.id}
                        onClick={() => openProviderTask(selectedProvider)}
                      >
                        {creatingProviderTask === selectedProvider.id ? "Opening task…" : "Open investigation task"}
                      </Button>
                    )}
                  </>
                )}
                {activeTab === "capabilities" && (
                  <div className="provider-capability-list">
                    {toolsQuery.loadState === "loading" && <LoadingIndicator label="Loading capabilities…" size={24} />}
                    {toolsQuery.errorMessage && <p className="login-error">{toolsQuery.errorMessage}</p>}
                    {selectedTools.map((tool) => (
                      <article key={tool.id}>
                        <strong>{tool.name}</strong>
                        <span>{tool.id}</span>
                        <p>{tool.description}</p>
                        <div className="provider-metadata">
                          <span>{tool.mode}</span>
                          <span>risk {tool.risk}</span>
                          <span>{tool.timeout_seconds}s timeout</span>
                          <span>{tool.enabled ? "enabled" : "disabled"}</span>
                        </div>
                      </article>
                    ))}
                    {toolsQuery.loadState === "ready" && selectedTools.length === 0 && (
                      <EmptyState title="No capabilities registered" description="The provider is connected but exposes no available tool definitions." />
                    )}
                  </div>
                )}
                {activeTab === "observations" && (
                  <>
                    {observationsQuery.loadState === "loading" && <LoadingIndicator label="Loading observations…" size={24} />}
                    {observationsQuery.errorMessage && <p className="login-error">{observationsQuery.errorMessage}</p>}
                    {observationsQuery.loadState === "ready" && (
                      <ObservationList observations={selectedObservations} />
                    )}
                  </>
                )}
                {activeTab === "integration" && (
                  <div className="provider-integration">
                    <KeyValueGrid
                      items={[
                        ["Watcher", selectedProvider.watchers.length ? selectedProvider.watchers : "None"],
                        ["Runbook", PROVIDER_RUNBOOK_HINTS[selectedProvider.id] ?? "task context fallback"],
                        ["Topology nodes", selectedNodes.map((node) => node.label)],
                        ["Monitor Kuma", selectedNodes.map((node) => node.availability_monitor).filter(Boolean)],
                      ]}
                    />
                    {selectedDefinition && (
                      <div className="provider-config-keys">
                        <strong>Expected configuration keys</strong>
                        <div className="provider-metadata">
                          {selectedDefinition.configuration_keys.map((key) => <span key={key}>{key}</span>)}
                        </div>
                      </div>
                    )}
                    {topologyQuery.errorMessage && <p className="login-error">{topologyQuery.errorMessage}</p>}
                    <Button type="button" onClick={openTopology}>Open topology</Button>
                  </div>
                )}
              </div>
            </>
          ) : (
            <EmptyState title="No providers registered" description="Add an API-ready provider or refresh when backend discovery is complete." actionLabel="New API-ready provider" onAction={() => setShowWizard(true)} />
          )}
        </section>
      </div>
      {showWizard && <ApiReadyWizard existingIds={existingIds} onClose={() => setShowWizard(false)} />}
    </div>
  );
}
