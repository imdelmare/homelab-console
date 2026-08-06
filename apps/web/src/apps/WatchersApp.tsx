import { useState } from "react";
import { Button, Checkbox, GroupBox as Fieldset, TextInput as Input, TextInput as TextArea } from "react95";
import { SelectControl } from "../components/SelectControl";
import { useQueryClient } from "@tanstack/react-query";
import {
  fetchIncidents,
  fetchWatcherRuns,
  fetchWatcherStatus,
  resolveIncidentHandled,
  runWatchers,
  updateWatcherAutomation,
  updateWatcherConfig,
} from "../lib/api";
import { formatDateTime } from "../lib/format";
import { describeError, shortId, statusClass } from "../lib/ui";
import { combineLoadStates, usePanelQuery } from "../lib/usePanelQuery";
import type { Incident } from "../lib/types";
import { PanelLoadingScreen } from "../components/PanelLoadingScreen";

export function WatchersApp() {
  const queryClient = useQueryClient();
  const [incidentStatus, setIncidentStatus] = useState<"open" | "resolved" | "all">("open");
  const incidentsQuery = usePanelQuery(["incidents", incidentStatus], () =>
    fetchIncidents({ status: incidentStatus === "all" ? undefined : incidentStatus, limit: 100 }),
  );
  const runsQuery = usePanelQuery(["watcher-runs", 20], () => fetchWatcherRuns(20));
  const statusQuery = usePanelQuery(["watcher-status"], fetchWatcherStatus);

  const incidents = incidentsQuery.data ?? [];
  const runs = runsQuery.data ?? [];
  const watcherStatus = statusQuery.data ?? null;
  const loadState = combineLoadStates(incidentsQuery.loadState, runsQuery.loadState, statusQuery.loadState);
  const queryError = incidentsQuery.errorMessage ?? runsQuery.errorMessage ?? statusQuery.errorMessage;

  const [actionError, setActionError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [savingAutomation, setSavingAutomation] = useState(false);
  const [savingWatcherId, setSavingWatcherId] = useState<string | null>(null);
  const [resolvingIncidentId, setResolvingIncidentId] = useState<string | null>(null);
  const [handledNoteIncidentId, setHandledNoteIncidentId] = useState<string | null>(null);
  const [handledNote, setHandledNote] = useState("");
  const [lastRunSummary, setLastRunSummary] = useState<string | null>(null);

  function refresh() {
    incidentsQuery.refresh();
    runsQuery.refresh();
    statusQuery.refresh();
  }

  function invalidateWatcherData() {
    void queryClient.invalidateQueries({ queryKey: ["incidents"] });
    void queryClient.invalidateQueries({ queryKey: ["watcher-runs"] });
    void queryClient.invalidateQueries({ queryKey: ["watcher-status"] });
    void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    void queryClient.invalidateQueries({ queryKey: ["providers"] });
    void queryClient.invalidateQueries({ queryKey: ["ops-errors"] });
    void queryClient.invalidateQueries({ queryKey: ["audit"] });
  }

  async function handleRunWatchers(watcherIds: string[], label: string) {
    setRunning(true);
    setActionError(null);
    setLastRunSummary(null);
    try {
      const result = await runWatchers(watcherIds);
      setLastRunSummary(
        `${label}: created ${result.created_tasks} task(s), updated ${result.updated_incidents} incident(s), resolved ${result.resolved_incidents} incident(s).`,
      );
      invalidateWatcherData();
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setRunning(false);
    }
  }

  async function handleAutomationToggle(enabled: boolean) {
    setSavingAutomation(true);
    setActionError(null);
    setLastRunSummary(null);
    try {
      const status = await updateWatcherAutomation(enabled);
      queryClient.setQueryData(["watcher-status"], status);
      void queryClient.invalidateQueries({ queryKey: ["audit"] });
      setLastRunSummary(`Watcher automation ${status.enabled ? "enabled" : "disabled"}.`);
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setSavingAutomation(false);
    }
  }

  async function handleWatcherConfig(
    watcherId: string,
    payload: {
      enabled?: boolean;
      interval_seconds?: number;
      min_severity?: "warning" | "critical";
      investigation_mode?: "manual" | "auto_investigate";
    },
  ) {
    setSavingWatcherId(watcherId);
    setActionError(null);
    setLastRunSummary(null);
    try {
      const status = await updateWatcherConfig(watcherId, payload);
      queryClient.setQueryData(["watcher-status"], status);
      void queryClient.invalidateQueries({ queryKey: ["audit"] });
      setLastRunSummary(`Updated watcher ${watcherId}.`);
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setSavingWatcherId(null);
    }
  }

  async function handleResolveHandled(incident: Incident) {
    const note = handledNote.trim();
    if (!note) {
      setActionError("Add an operator note before marking the incident as already handled.");
      return;
    }
    setResolvingIncidentId(incident.id);
    setActionError(null);
    setLastRunSummary(null);
    try {
      await resolveIncidentHandled(incident.id, note);
      setHandledNoteIncidentId(null);
      setHandledNote("");
      setLastRunSummary(`Resolved ${shortId(incident.id)} as already handled.`);
      invalidateWatcherData();
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setResolvingIncidentId(null);
    }
  }

  function openTask(taskId: string | null) {
    if (!taskId) return;
    window.dispatchEvent(new CustomEvent("homelab:open-task", { detail: { taskId } }));
  }

  const watcherRows = watcherStatus?.watchers ?? [];
  const automaticWatchers = watcherRows.filter((watcher) => watcher.enabled).map((watcher) => watcher.id);
  const autoInvestigationWatchers = watcherRows.filter(
    (watcher) => watcher.investigation_mode === "auto_investigate",
  );
  const shownError = queryError ?? actionError;

  if (loadState === "loading") {
    return <PanelLoadingScreen label="Loading watchers and incidents…" />;
  }

  return (
    <div className="panel-app watchers-app">
      <div className="panel-toolbar">
        <Button onClick={() => handleRunWatchers(automaticWatchers, "Scheduled watchers")} disabled={running || automaticWatchers.length === 0}>
          {running ? "Running…" : "Run scheduled"}
        </Button>
        <Button onClick={refresh} disabled={incidentsQuery.isFetching || runsQuery.isFetching || statusQuery.isFetching || running}>
          Refresh
        </Button>
        <SelectControl
          value={incidentStatus}
          onChange={(event) => setIncidentStatus(event.target.value as "open" | "resolved" | "all")}
          aria-label="Incident status"
          disabled={running}
        >
          <option value="open">Open incidents</option>
          <option value="resolved">Resolved incidents</option>
          <option value="all">All incidents</option>
        </SelectControl>
        <span className="toolbar-count">{incidents.length} incidents shown</span>
      </div>

      {watcherStatus && (
        <div className="watcher-toggle">
          <Checkbox
            type="checkbox"
            label={`Watcher automation ${watcherStatus.enabled ? "enabled" : "disabled"}`}
            checked={watcherStatus.enabled}
            onChange={(event) => handleAutomationToggle(event.target.checked)}
            disabled={savingAutomation || running}
          />
          {savingAutomation && <small>Saving…</small>}
        </div>
      )}

      {shownError && <p className="login-error">{shownError}</p>}
      {lastRunSummary && <p className="watcher-summary">{lastRunSummary}</p>}
      {watcherStatus && (
        <details className="watcher-config">
          <summary>{watcherRows.length} configured watchers · {automaticWatchers.length} active · {incidents.length} incidents {incidentStatus === "open" ? "open" : "shown"}</summary>
          <p className="metadata-line">
            Scheduler {watcherStatus.enabled ? "active" : "disabled"} · interval {watcherStatus.interval_seconds}s · minimum severity {watcherStatus.min_severity} · resolve after {watcherStatus.resolve_after_missing_runs} missing runs
            {watcherStatus.ignore_patterns.length > 0 ? ` · exclusions: ${watcherStatus.ignore_patterns.join(", ")}` : ""}
          </p>
        </details>
      )}

      {watcherStatus && (
        <details className="watcher-config-table">
          <summary>
            Watcher scheduling · {watcherStatus.scheduled_watcher_ids.length} active ·{" "}
            {autoInvestigationWatchers.length} automatic
          </summary>
          <div className="watcher-column-headings" aria-hidden="true">
            <span />
            <span>Watcher</span>
            <span>Mode</span>
            <span>Last run</span>
            <span>Next run</span>
          </div>
          <div className="watcher-config-list">
            {watcherRows.map((watcher) => (
              <details className={`watcher-config-row ${watcher.enabled ? "watcher-enabled" : "watcher-disabled"}`} key={watcher.id}>
                <summary className="watcher-row-summary">
                  <span className={`watcher-state-dot ${watcher.enabled ? "enabled" : "disabled"}`} aria-hidden="true" />
                  <strong>{watcher.label}</strong>
                  <span className="watcher-row-mode">{watcher.investigation_mode === "auto_investigate" ? "Automatic investigation" : "Manual"}</span>
                  <span className="watcher-row-time">
                    <small>Last run</small>
                    <strong>{watcher.last_run ? watcher.last_run.status : "Never"}</strong>
                    <time>{watcher.last_run ? formatDateTime(watcher.last_run.started_at) : "No runs recorded"}</time>
                  </span>
                  <span className="watcher-row-time">
                    <small>Next run</small>
                    <strong>{watcher.next_run_at ? formatDateTime(watcher.next_run_at) : "Not scheduled"}</strong>
                  </span>
                </summary>
                <Fieldset label="Watcher settings" className="watcher-row-editor">
                  <Checkbox label="Active" type="checkbox" checked={watcher.enabled} disabled={savingWatcherId === watcher.id || running} onChange={(event) => handleWatcherConfig(watcher.id, { enabled: event.target.checked })} />
                  <label><span>Minimum severity</span><SelectControl value={watcher.min_severity} disabled={savingWatcherId === watcher.id || running} onChange={(event) => handleWatcherConfig(watcher.id, { min_severity: event.target.value as "warning" | "critical" })}><option value="warning">warning+</option><option value="critical">critical</option></SelectControl></label>
                  <label><span>Investigation</span><SelectControl value={watcher.investigation_mode} disabled={savingWatcherId === watcher.id || running} onChange={(event) => handleWatcherConfig(watcher.id, { investigation_mode: event.target.value as "manual" | "auto_investigate" })}><option value="manual">Manual</option><option value="auto_investigate">Automatic</option></SelectControl></label>
                  <label><span>Interval (seconds)</span><Input type="number" min={60} step={60} value={watcher.interval_seconds} disabled={savingWatcherId === watcher.id || running} onChange={(event) => handleWatcherConfig(watcher.id, { interval_seconds: Number.parseInt(event.target.value, 10) || 60 })} /></label>
                  <Button type="button" onClick={() => handleRunWatchers([watcher.id], watcher.label)} disabled={running}>Run now</Button>
                </Fieldset>
                {(watcher.last_error || watcher.runbook_incident_type) && <p className="watcher-row-note">{watcher.last_error ? `Last error: ${watcher.last_error}` : ""}{watcher.last_error && watcher.runbook_incident_type ? " · " : ""}{watcher.runbook_incident_type ? `Runbook: ${watcher.runbook_incident_type}` : ""}</p>}
              </details>
            ))}
          </div>
        </details>
      )}

      <div className="watcher-layout">
        <section className="watcher-section">
          <h3>{incidentStatus === "all" ? "Incidents" : incidentStatus === "open" ? "Open incidents" : "Resolved incidents"}</h3>
          <div className="item-list watcher-incident-list">
            {incidents.map((incident) => {
              const isDependent = Boolean(incident.root_cause_incident_id);
              return (
                <article className="item-row item-row-stacked watcher-incident-row" key={incident.id}>
                  <div className="task-row-main">
                    <strong>{incident.title}</strong>
                    <span>{incident.description}</span>
                    <small>
                      {incident.provider_id} · detected {incident.occurrences} times · last event{" "}
                      {formatDateTime(incident.last_seen_at)}
                      {incident.missing_runs > 0
                        ? ` · clearing ${incident.missing_runs}/${watcherStatus?.resolve_after_missing_runs ?? 3}`
                        : ""}
                      {incident.resolved_at ? ` · resolved ${formatDateTime(incident.resolved_at)}` : ""}
                    </small>
                    <small>{incident.dedupe_note || "Deduplication metadata pending."}</small>
                    <small>{incident.auto_close_note || "Automatic closure policy pending."}</small>
                    {incident.runbook_incident_type && <small>Runbook: {incident.runbook_incident_type}</small>}
                  </div>
                  <div className="watcher-incident-meta">
                    <mark className={`rca-badge ${isDependent ? "rca-badge-dependent" : "rca-badge-root"}`}>
                      {isDependent ? `RCA -> ${shortId(incident.root_cause_incident_id)}` : "RCA root"}
                    </mark>
                    <mark className={`finding-severity finding-${incident.severity}`}>{incident.severity}</mark>
                    <mark className={`task-status task-status-${statusClass(incident.status)}`}>{incident.status}</mark>
                    <Button type="button" disabled={!incident.task_id} onClick={() => openTask(incident.task_id)}>
                      Task {shortId(incident.task_id)}
                    </Button>
                    {incident.status === "open" && (
                      <>
                        {handledNoteIncidentId === incident.id ? (
                          <div className="watcher-handled-note">
                            <label>
                              <span>Operator note</span>
                              <TextArea
                                multiline
                                value={handledNote}
                                onChange={(event) => setHandledNote(event.target.value)}
                                placeholder="What was done, by whom, and the current result."
                                rows={3}
                                autoFocus
                              />
                            </label>
                            <div className="watcher-handled-actions">
                              <Button
                                type="button"
                                disabled={running || resolvingIncidentId === incident.id || !handledNote.trim()}
                                onClick={() => handleResolveHandled(incident)}
                              >
                                {resolvingIncidentId === incident.id ? "Saving..." : "Confirm handled"}
                              </Button>
                              <Button
                                type="button"
                                disabled={resolvingIncidentId === incident.id}
                                onClick={() => {
                                  setHandledNoteIncidentId(null);
                                  setHandledNote("");
                                }}
                              >
                                Cancel
                              </Button>
                            </div>
                          </div>
                        ) : (
                          <Button
                            type="button"
                            disabled={running || Boolean(resolvingIncidentId)}
                            onClick={() => {
                              setHandledNoteIncidentId(incident.id);
                              setHandledNote("");
                              setActionError(null);
                            }}
                          >
                            Already handled
                          </Button>
                        )}
                      </>
                    )}
                  </div>
                </article>
              );
            })}
            {loadState === "ready" && incidents.length === 0 && <p>No incidents for this filter.</p>}
          </div>
        </section>

        <section className="watcher-section watcher-run-section">
          <h3>Recent runs</h3>
          <div className="audit-table-wrap">
            <table className="audit-table">
              <thead>
                <tr>
                  <th>Started</th>
                  <th>Watcher</th>
                  <th>Status</th>
                  <th>Tasks created</th>
                  <th>Updated</th>
                  <th>Resolved</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id}>
                    <td>{formatDateTime(run.started_at)}</td>
                    <td>{run.watcher_id}</td>
                    <td>{run.status}</td>
                    <td>{run.created_tasks}</td>
                    <td>{run.updated_incidents}</td>
                    <td>{run.resolved_incidents}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {loadState === "ready" && runs.length === 0 && <p>No watcher runs.</p>}
          </div>
        </section>
      </div>
    </div>
  );
}
