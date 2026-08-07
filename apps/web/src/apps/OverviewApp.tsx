import { useState } from "react";
import { Button } from "react95";
import { useQueryClient } from "@tanstack/react-query";
import {
  fetchIncidents,
  fetchMcpClients,
  fetchOperationalHealth,
  fetchOpsErrors,
  fetchProviders,
  fetchRunbooks,
  fetchTasks,
  fetchWatcherRuns,
  fetchWatcherStatus,
  runWatchers,
} from "../lib/api";
import { formatDateTime } from "../lib/format";
import { describeError, isMcpClientOnline, shortId, statusClass, taskAgent, taskStatusLabel } from "../lib/ui";
import { combineLoadStates, usePanelQuery } from "../lib/usePanelQuery";
import { StatusLed } from "../components/StatusLed";
import { StatusBadge } from "../components/StatusBadge";
import { PanelLoadingScreen } from "../components/PanelLoadingScreen";
import { ProviderIcon } from "./shared";
import type { AppId } from "../lib/types";

export function OverviewApp({ onOpenApp }: { onOpenApp: (id: AppId) => void }) {
  const queryClient = useQueryClient();
  const providersQuery = usePanelQuery(["providers"], fetchProviders);
  const tasksQuery = usePanelQuery(["tasks", "open"], () => fetchTasks({ status: "open", limit: 100 }));
  const incidentsQuery = usePanelQuery(["incidents", "open"], () => fetchIncidents({ status: "open", limit: 100 }));
  const clientsQuery = usePanelQuery(["mcp-clients"], fetchMcpClients);
  const watcherStatusQuery = usePanelQuery(["watcher-status"], fetchWatcherStatus);
  const runsQuery = usePanelQuery(["watcher-runs", 5], () => fetchWatcherRuns(5));
  const opsErrorsQuery = usePanelQuery(["ops-errors", 5], () => fetchOpsErrors(5));
  const opsHealthQuery = usePanelQuery(["ops-health"], fetchOperationalHealth);
  const runbooksQuery = usePanelQuery(["runbooks"], fetchRunbooks);

  const providers = providersQuery.data ?? [];
  const tasks = tasksQuery.data ?? [];
  const incidents = incidentsQuery.data ?? [];
  const clients = clientsQuery.data ?? [];
  const watcherStatus = watcherStatusQuery.data ?? null;
  const runs = runsQuery.data ?? [];
  const opsErrors = opsErrorsQuery.data ?? [];
  const opsHealth = opsHealthQuery.data ?? null;
  const runbooks = runbooksQuery.data ?? [];

  const queries = [
    providersQuery,
    tasksQuery,
    incidentsQuery,
    clientsQuery,
    watcherStatusQuery,
    runsQuery,
    opsErrorsQuery,
    opsHealthQuery,
    runbooksQuery,
  ];
  const loadState = combineLoadStates(...queries.map((query) => query.loadState));
  const isRefreshing = queries.some((query) => query.isFetching);
  const errorMessage = queries.map((query) => query.errorMessage).find(Boolean) ?? null;

  const [selectedRunbookType, setSelectedRunbookType] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [runningWatchers, setRunningWatchers] = useState(false);
  const [lastRunSummary, setLastRunSummary] = useState<string | null>(null);

  function refresh() {
    for (const query of queries) query.refresh();
  }

  const providerIssues = providers.filter((provider) => provider.status !== "healthy");
  const activeClients = clients.filter((client) => !client.revoked_at);
  const onlineClients = activeClients.filter((client) => isMcpClientOnline(client));
  const latestRun = runs[0] ?? null;
  const lastWorkerRun = opsHealth?.workers.last_watcher_run ?? latestRun;
  const pendingNotifications = opsHealth?.workers.notification_counts.pending ?? 0;
  const failedNotifications = opsHealth?.workers.notification_counts.failed ?? 0;
  const criticalIncidents = incidents.filter((incident) => incident.severity === "critical");
  const unassignedTasks = tasks.filter((task) => !task.assigned_agent);
  const claimedTasks = tasks.filter((task) => task.assigned_agent);
  const scheduledWatcherIds = watcherStatus?.scheduled_watcher_ids ?? [];
  const selectedRunbook =
    runbooks.find((runbook) => runbook.incident_type === selectedRunbookType) ?? runbooks[0] ?? null;
  const shownError = errorMessage ?? actionError;

  async function handleRunScheduledWatchers() {
    setRunningWatchers(true);
    setActionError(null);
    setLastRunSummary(null);
    try {
      const result = await runWatchers(scheduledWatcherIds);
      setLastRunSummary(
        `Watcher run: ${result.created_tasks} task(s), ${result.updated_incidents} update(s), ${result.resolved_incidents} resolved.`,
      );
      void queryClient.invalidateQueries({ queryKey: ["incidents"] });
      void queryClient.invalidateQueries({ queryKey: ["watcher-runs"] });
      void queryClient.invalidateQueries({ queryKey: ["watcher-status"] });
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      void queryClient.invalidateQueries({ queryKey: ["ops-errors"] });
      void queryClient.invalidateQueries({ queryKey: ["providers"] });
      void queryClient.invalidateQueries({ queryKey: ["audit"] });
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setRunningWatchers(false);
    }
  }

  if (loadState === "loading") {
    return <PanelLoadingScreen label="Loading overview…" />;
  }

  return (
    <div className="panel-app overview-app">
      <div className="panel-toolbar">
        <Button onClick={refresh} disabled={isRefreshing}>
          Refresh
        </Button>
        <Button onClick={handleRunScheduledWatchers} disabled={runningWatchers || scheduledWatcherIds.length === 0}>
          {runningWatchers ? "Running…" : "Run scheduled watchers"}
        </Button>
        <Button onClick={() => onOpenApp("watchers")}>Watchers</Button>
        <Button onClick={() => onOpenApp("tasks")}>Tasks</Button>
        <Button onClick={() => onOpenApp("topology")}>Topology</Button>
      </div>
      {shownError && <p className="login-error">{shownError}</p>}
      <section className="overview-status-grid">
        <Button className={`overview-tile ${providerIssues.length ? "overview-warn" : "overview-ok"}`} onClick={() => onOpenApp("providers")}>
          <span>Provider</span>
          <strong>{providerIssues.length}</strong>
          <small>{providerIssues.length ? "need attention" : "operational"}</small>
        </Button>
        <Button className={`overview-tile ${criticalIncidents.length ? "overview-critical" : incidents.length ? "overview-warn" : "overview-ok"}`} onClick={() => onOpenApp("watchers")}>
          <span>Open incidents</span>
          <strong>{incidents.length}</strong>
          <small>{criticalIncidents.length} critical</small>
        </Button>
        <Button className={`overview-tile ${tasks.length ? "overview-warn" : "overview-ok"}`} onClick={() => onOpenApp("tasks")}>
          <span>Open tasks</span>
          <strong>{tasks.length}</strong>
          <small>{tasks.filter((task) => !task.assigned_agent).length} unassigned</small>
        </Button>
        <Button className="overview-tile" onClick={() => onOpenApp("mcp")}>
          <span>MCP Clients</span>
          <strong>
            {onlineClients.length}/{activeClients.length}
          </strong>
          <small>online / active</small>
        </Button>
      </section>
      <section className="overview-strip">
        <span>Watcher automation: {watcherStatus?.enabled ? "enabled" : "disabled"}</span>
        <span>{scheduledWatcherIds.length} scheduled watchers</span>
        <span>Last run: {latestRun ? formatDateTime(latestRun.started_at) : "never"}</span>
        <span>Task queue: {unassignedTasks.length} unassigned · {claimedTasks.length} assigned</span>
      </section>
      <div className="overview-workspace">
      {lastRunSummary && <p className="overview-summary">{lastRunSummary}</p>}
      <section className="control-room-grid">
        <div className="control-room-box control-room-wide">
          <h3>Task queue</h3>
          <div className="item-list">
            {tasks.slice(0, 6).map((task) => (
              <Button
                type="button"
                className={`control-task-row task-row-${statusClass(task.status)}`}
                key={task.id}
                onClick={() => {
                  window.dispatchEvent(new CustomEvent("homelab:open-task", { detail: { taskId: task.id } }));
                  onOpenApp("tasks");
                }}
              >
                <StatusBadge label={taskStatusLabel(task.status)} tone={task.status === "blocked" ? "danger" : task.status === "open" ? "neutral" : "warning"} />
                <span className="overview-task-copy">
                  <strong>{task.title}</strong>
                  <small className="clamp-two-lines">{task.goal}</small>
                  <small className="metadata-line">{taskAgent(task)} · updated {formatDateTime(task.last_activity_at ?? task.updated_at)}</small>
                </span>
                <span className="overview-task-action">Open task</span>
              </Button>
            ))}
            {loadState === "ready" && tasks.length === 0 && <p>No open tasks.</p>}
          </div>
        </div>
        <div className="control-room-box">
          <h3>MCP Roster</h3>
          <div className="compact-list">
            {activeClients.slice(0, 5).map((client) => (
              <Button type="button" key={client.id} onClick={() => onOpenApp("mcp")}>
                <span>{client.agent_id}</span>
                <strong>{client.client_label || shortId(client.id)}</strong>
                <mark className={`task-status task-status-${isMcpClientOnline(client) ? "completed" : "waiting_operator"}`}>
                  {isMcpClientOnline(client) ? "online" : "idle"}
                </mark>
              </Button>
            ))}
            {loadState === "ready" && activeClients.length === 0 && <p>No active clients.</p>}
          </div>
        </div>
        <div className="control-room-box">
          <h3>Operational status</h3>
          <div className="compact-list">
            <Button type="button" onClick={() => onOpenApp("audit")}>
              <span>Database</span>
              <strong>{opsHealth?.database.size_pretty || opsHealth?.database.dialect || "Unavailable"}</strong>
              <small>{opsHealth?.database.connections != null ? `${opsHealth.database.connections} connections` : "Connection count unavailable"}</small>
            </Button>
            <Button type="button" onClick={() => onOpenApp("watchers")}>
              <span>Latest watcher</span>
              <strong>{lastWorkerRun ? lastWorkerRun.status : "Never run"}</strong>
              <small>{lastWorkerRun ? `${lastWorkerRun.watcher_id} · ${formatDateTime(lastWorkerRun.started_at)}` : "No runs"}</small>
            </Button>
            <Button type="button" onClick={() => onOpenApp("audit")}>
              <span>Outbox</span>
              <strong>{pendingNotifications} pending</strong>
              <small>{failedNotifications} failed · retention {opsHealth?.retention.enabled ? "enabled" : "disabled"}</small>
            </Button>
            <Button type="button" onClick={() => onOpenApp("providers")}>
              <span>Provider errors</span>
              <strong>{opsHealth?.provider_errors.length ?? 0}</strong>
              <small>{opsHealth?.provider_errors[0]?.message || "No recent provider errors"}</small>
            </Button>
          </div>
        </div>
        <div className="control-room-box">
          <h3>Latest runs</h3>
          <div className="compact-list">
            {runs.slice(0, 5).map((run) => (
              <Button type="button" key={run.id} onClick={() => onOpenApp("watchers")}>
                <span>{run.watcher_id}</span>
                <strong>{run.status}</strong>
                <small>{formatDateTime(run.started_at)}</small>
              </Button>
            ))}
            {loadState === "ready" && runs.length === 0 && <p>No watcher runs.</p>}
          </div>
        </div>
        <div className="control-room-box">
          <h3>Recent events</h3>
          <div className="compact-list">
            {opsErrors.slice(0, 5).map((item) => (
              <Button type="button" key={`${item.kind}-${item.id}`} onClick={() => item.kind === "watcher" ? onOpenApp("watchers") : onOpenApp("audit")}>
                <span>{item.source}</span>
                <strong>{item.title}</strong>
                <small>{item.detail || formatDateTime(item.created_at)}</small>
              </Button>
            ))}
            {loadState === "ready" && opsErrors.length === 0 && <p>No recent operational events.</p>}
          </div>
        </div>
        <div className="control-room-box control-room-runbook">
          <h3>Operational procedures</h3>
          <div className="runbook-viewer">
            <div className="compact-list runbook-selector">
              {runbooks.map((runbook) => (
                <Button
                  type="button"
                  className={selectedRunbook?.incident_type === runbook.incident_type ? "runbook-selected" : ""}
                  key={runbook.incident_type}
                  onClick={() => setSelectedRunbookType(runbook.incident_type)}
                >
                  <span>{runbook.incident_type}</span>
                  <strong>{runbook.label}</strong>
                  <small>{runbook.steps.length} steps</small>
                </Button>
              ))}
              {loadState === "ready" && runbooks.length === 0 && <p>No procedures loaded.</p>}
            </div>
            {selectedRunbook && (
              <article className="runbook-detail">
                <strong>{selectedRunbook.label}</strong>
                <span>{selectedRunbook.incident_type}</span>
                <ol>
                  {selectedRunbook.steps.map((step, index) => (
                    <li key={`${step.tool_id}-${index}`}>
                      <code>{step.tool_id}</code>
                      <span>{step.evidence}</span>
                    </li>
                  ))}
                </ol>
                {selectedRunbook.escalation_note && <p>{selectedRunbook.escalation_note}</p>}
              </article>
            )}
          </div>
        </div>
      </section>
      <div className="overview-columns">
        <section className="watcher-section">
          <h3>Open incidents</h3>
          <div className="item-list">
            {incidents.slice(0, 6).map((incident) => (
              <article className="item-row item-row-stacked" key={incident.id}>
                <strong>{incident.title}</strong>
                <span>{incident.provider_id} · {formatDateTime(incident.last_seen_at)}</span>
              </article>
            ))}
            {loadState === "ready" && incidents.length === 0 && <p>No open incidents.</p>}
          </div>
        </section>
        <section className="watcher-section">
          <h3>Providers to check</h3>
          <div className="item-list">
            {providerIssues.slice(0, 6).map((provider) => (
              <article className="item-row" key={provider.id}>
                <div className="provider-main">
                  <ProviderIcon providerId={provider.id} />
                  <div>
                    <strong>{provider.name}</strong>
                    <span>{provider.detail || provider.id}</span>
                  </div>
                </div>
                <StatusLed status={provider.status} />
              </article>
            ))}
            {loadState === "ready" && providerIssues.length === 0 && <p>No provider issues.</p>}
          </div>
        </section>
      </div>
      </div>
    </div>
  );
}
