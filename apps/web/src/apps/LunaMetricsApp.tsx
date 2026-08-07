import { useState } from "react";
import { Button, ProgressBar } from "react95";
import { useQueryClient } from "@tanstack/react-query";
import { fetchLunaMetrics, reviewTaskRouter } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { describeError, shortId } from "../lib/ui";
import { usePanelQuery } from "../lib/usePanelQuery";
import { PanelLoadingScreen } from "../components/PanelLoadingScreen";
import { SelectControl } from "../components/SelectControl";
import { ConfirmDialog } from "../components/ConfirmDialog";
import type { TaskRouterCorrections } from "../lib/types";

const owners = ["operator", "fixer", "claude", "codex", "cline", "opencode", "none"];
const categories = ["network", "dns", "backup", "security", "provider", "watcher", "mcp", "ui", "docs", "automation", "unknown"];
const actions = ["keep", "merge_candidate", "operator_review"];
const priorities = ["low", "medium", "high", "urgent"];
const severities = ["info", "warning", "critical"];

function percent(value: number | null): string {
  return value === null ? "N/D" : `${Math.round(value * 100)}%`;
}

function progressPercent(value: number | null): number | null {
  return value === null ? null : Math.round(value * 100);
}

function usd(value: number): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 4 }).format(value);
}

function integer(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

function milliseconds(value: number | null): string {
  return value === null ? "N/D" : `${Math.round(value)} ms`;
}

export function LunaMetricsApp() {
  const queryClient = useQueryClient();
  const [days, setDays] = useState(30);
  const [actionError, setActionError] = useState<string | null>(null);
  const [savingTaskId, setSavingTaskId] = useState<string | null>(null);
  const [correctingTaskId, setCorrectingTaskId] = useState<string | null>(null);
  const [correctedOwner, setCorrectedOwner] = useState("");
  const [correctedCategory, setCorrectedCategory] = useState("");
  const [correctedAction, setCorrectedAction] = useState("");
  const [correctedPriority, setCorrectedPriority] = useState("");
  const [correctedSeverity, setCorrectedSeverity] = useState("");
  const [correctedNeedsOperator, setCorrectedNeedsOperator] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  const [showAllReviews, setShowAllReviews] = useState(false);
  const [rejectingTaskId, setRejectingTaskId] = useState<string | null>(null);
  const [examiningTaskId, setExaminingTaskId] = useState<string | null>(null);
  const metricsQuery = usePanelQuery(["luna-metrics", days], () => fetchLunaMetrics(days));
  const metrics = metricsQuery.data;

  async function review(
    taskId: string,
    verdict: "accepted" | "corrected" | "rejected",
    corrections: TaskRouterCorrections = {},
    note = "",
  ) {
    setSavingTaskId(taskId);
    setActionError(null);
    try {
      await reviewTaskRouter(taskId, { verdict, corrections, note });
      setCorrectingTaskId(null);
      setCorrectedOwner("");
      setCorrectedCategory("");
      setCorrectedAction("");
      setCorrectedPriority("");
      setCorrectedSeverity("");
      setCorrectedNeedsOperator("");
      setReviewNote("");
      await queryClient.invalidateQueries({ queryKey: ["luna-metrics"] });
      await queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] });
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setSavingTaskId(null);
    }
  }

  function openTask(taskId: string) {
    window.dispatchEvent(new CustomEvent("homelab:open-task", { detail: { taskId } }));
  }

  if (metricsQuery.loadState === "loading") {
    return <PanelLoadingScreen label="Loading metrics…" />;
  }

  return (
    <div className="panel-app luna-metrics-app">
      <div className="panel-toolbar">
        <Button type="button" onClick={() => metricsQuery.refresh()} disabled={metricsQuery.isFetching}>Refresh</Button>
        <label className="luna-period-control" htmlFor="luna-metrics-period"><span>Period</span><SelectControl id="luna-metrics-period" name="luna-metrics-period" autoComplete="off" value={days} onChange={(event) => setDays(Number(event.target.value))}>
          <option value={1}>24 hours</option>
          <option value={7}>7 days</option>
          <option value={30}>30 days</option>
          <option value={90}>90 days</option>
          <option value={365}>1 year</option>
        </SelectControl></label>
        <span className="toolbar-count">AI telemetry</span>
      </div>

      {(metricsQuery.errorMessage || actionError) && <p className="login-error">{metricsQuery.errorMessage ?? actionError}</p>}

      {metrics && (
        <>
          <section className="luna-kpi-grid">
            <article><span>Attributed cost</span><strong>{usd(metrics.summary.attributed_cost_usd)}</strong><small>{days}-day window · not reconciled with invoice</small></article>
            <article><span>Technical reliability</span><strong>{percent(metrics.router.technical_success_rate)}</strong>{progressPercent(metrics.router.technical_success_rate) !== null && <ProgressBar className="luna-kpi-progress" style={{ width: "100%" }} value={progressPercent(metrics.router.technical_success_rate) ?? 0} />}<small>{metrics.router.successful_calls} successful · {metrics.router.failed_calls} failed</small></article>
            <article className={metrics.router.reviewed === 0 ? "luna-kpi-unavailable" : ""}><span>Reviewed accuracy</span><strong>{metrics.router.reviewed === 0 ? "Unavailable" : percent(metrics.router.reviewed_accuracy)}</strong>{progressPercent(metrics.router.reviewed_accuracy) !== null && <ProgressBar className="luna-kpi-progress" style={{ width: "100%" }} value={progressPercent(metrics.router.reviewed_accuracy) ?? 0} />}<small>{metrics.router.reviewed === 0 ? "Unavailable: no human reviews · 0 reviewed decisions" : `${metrics.router.reviewed} reviewed · coverage ${percent(metrics.router.review_coverage)}`}</small></article>
            <article><span>Metering coverage</span><strong>{percent(metrics.summary.metering_coverage)}</strong>{progressPercent(metrics.summary.metering_coverage) !== null && <ProgressBar className="luna-kpi-progress" style={{ width: "100%" }} value={progressPercent(metrics.summary.metering_coverage) ?? 0} />}<small>{metrics.summary.metered_calls}/{metrics.summary.calls} metered calls</small></article>
            <article><span>Local responses</span><strong>{percent(metrics.ai_manager.local_rate)}</strong><ProgressBar className="luna-kpi-progress" style={{ width: "100%" }} value={progressPercent(metrics.ai_manager.local_rate) ?? 0} /><small>{metrics.ai_manager.local_calls}/{metrics.ai_manager.calls} handled by ai-host</small></article>
            <article><span>OpenAI fallback</span><strong>{percent(metrics.ai_manager.fallback_rate)}</strong><ProgressBar className="luna-kpi-progress" style={{ width: "100%" }} value={progressPercent(metrics.ai_manager.fallback_rate) ?? 0} /><small>{metrics.ai_manager.fallback_calls} fallbacks · {metrics.ai_manager.schema_errors} schema errors · {metrics.ai_manager.timeouts} timeouts</small></article>
          </section>

          <details className="luna-mobile-disclosure">
            <summary>Cost and pricing details</summary>
          <section className="luna-note">
            <span className="luna-note-pricing">
              <strong>{metrics.pricing.model}</strong>
              <span>${metrics.pricing.input_per_million}/M input</span>
              <span>${metrics.pricing.cached_input_per_million}/M cached</span>
              <span>${metrics.pricing.output_per_million}/M output</span>
            </span>
            <small>Attributed cost uses recorded API usage; billed cost requires reconciliation with OpenAI Costs.</small>
          </section>
          </details>

          <details className="luna-mobile-disclosure">
            <summary>Technical and routing details</summary>
          <div className="luna-dashboard-grid">
            <section className="task-section">
              <h3>Usage by component</h3>
              <div className="table-wrap">
                <table className="data-table">
                  <thead><tr><th>Component</th><th>Calls</th><th>Input</th><th>Output</th><th>Coverage</th><th>Cost</th></tr></thead>
                  <tbody>
                    {metrics.components.map((component) => (
                      <tr key={component.component}>
                        <td>{component.label}</td><td>{component.calls}</td><td>{integer(component.input_tokens)}</td><td>{integer(component.output_tokens)}</td><td>{percent(component.metering_coverage)}</td><td>{usd(component.attributed_cost_usd)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>

            <section className="task-section luna-router-summary">
              <h3>Routing model signals</h3>
              <dl>
                <div><dt>Decisions</dt><dd>{metrics.router.decisions}</dd></div>
                <div><dt>Accepted</dt><dd>{metrics.router.accepted}</dd></div>
                <div><dt>Corrected</dt><dd>{metrics.router.corrected}</dd></div>
                <div><dt>Rejected</dt><dd>{metrics.router.rejected}</dd></div>
                <div className="luna-model-confidence"><dt>Model confidence (unvalidated)</dt><dd>{percent(metrics.router.average_confidence)}</dd></div>
              </dl>
              {metrics.router.reviewed === 0 && metrics.router.decisions > 0 && (
                 <p className="luna-review-explainer"><strong>No human validation.</strong> Accuracy and coverage are unavailable until the operator reviews the queue.</p>
              )}
               <h4>Suggested owners</h4>
              <div className="luna-owner-list">
                {Object.entries(metrics.router.owner_distribution).map(([owner, count]) => <span key={owner}>{owner}: {count}</span>)}
                 {Object.keys(metrics.router.owner_distribution).length === 0 && <span>No decisions.</span>}
              </div>
               <h4>Automatic investigation policy</h4>
              <div className="luna-owner-list">
                {Object.entries(metrics.auto_investigate).map(([outcome, count]) => <span key={outcome}>{outcome}: {count}</span>)}
                 {Object.keys(metrics.auto_investigate).length === 0 && <span>No events.</span>}
              </div>
               <h4>ai-host performance</h4>
              <dl>
                 <div><dt>Average wait</dt><dd>{milliseconds(metrics.ai_manager.queue_wait.average_ms)}</dd></div>
                 <div><dt>p95 wait</dt><dd>{milliseconds(metrics.ai_manager.queue_wait.p95_ms)}</dd></div>
                 <div><dt>Average inference</dt><dd>{milliseconds(metrics.ai_manager.inference_latency.average_ms)}</dd></div>
                 <div><dt>p95 inference</dt><dd>{milliseconds(metrics.ai_manager.inference_latency.p95_ms)}</dd></div>
              </dl>
               <h4>Effective models</h4>
              <div className="luna-owner-list">
                {Object.entries(metrics.ai_manager.effective_models).map(([model, count]) => <span key={model}>{model}: {count}</span>)}
                 {Object.keys(metrics.ai_manager.effective_models).length === 0 && <span>No versioned inferences.</span>}
              </div>
            </section>
          </div>
          </details>

          <section className="task-section luna-review-section">
             <div className="luna-section-head"><h3>Routing review queue</h3><strong>{Math.min(showAllReviews ? metrics.review_queue.length : 5, metrics.review_queue.length)} of {metrics.review_queue.length} unreviewed</strong></div>
             {metrics.review_queue.length === 0 && <p>No routing decisions to review.</p>}
            <div className="luna-review-list">
              {metrics.review_queue.slice(0, showAllReviews ? undefined : 5).map((item) => {
                const saving = savingTaskId === item.task_id;
                const correcting = correctingTaskId === item.task_id;
                return (
                  <article key={item.task_id}>
                    <div>
                      <strong>{item.task_title}</strong>
                       <small>{shortId(item.task_id)} · {formatDateTime(item.created_at)} · {item.action} · {item.category} · {item.priority}/{item.severity} · owner {item.suggested_owner} · operator {item.needs_operator ? "yes" : "no"} · confidence {percent(item.confidence)}</small>
                      {item.summary && <p>{item.summary}</p>}
                    </div>
                    <div className="luna-review-actions">
                      <Button type="button" onClick={() => { setExaminingTaskId(examiningTaskId === item.task_id ? null : item.task_id); setReviewNote(""); }}>{examiningTaskId === item.task_id ? "Close review" : "Review"}</Button>
                    </div>
                    {examiningTaskId === item.task_id && <div className="luna-review-detail">
                    <p>{item.summary || "No summary available."}</p>
                    <div className="luna-review-actions">
                      <Button className="luna-action-accept" type="button" disabled={saving} onClick={() => review(item.task_id, "accepted", {}, reviewNote)}>Accept suggestion</Button>
                      <Button type="button" disabled={saving} onClick={() => setCorrectingTaskId(correcting ? null : item.task_id)}>Correct</Button>
                      <Button className="luna-action-reject" type="button" disabled={saving} onClick={() => setRejectingTaskId(item.task_id)}>Reject</Button>
                      <Button type="button" onClick={() => openTask(item.task_id)}>Open task</Button>
                    </div>
                    {correcting && (
                      <div className="luna-correction-form">
                         <SelectControl value={correctedAction} onChange={(event) => setCorrectedAction(event.target.value)} aria-label="Correct action">
                           <option value="">Action unchanged</option>
                          {actions.map((action) => <option key={action} value={action}>{action}</option>)}
                        </SelectControl>
                         <SelectControl value={correctedCategory} onChange={(event) => setCorrectedCategory(event.target.value)} aria-label="Correct category">
                           <option value="">Category unchanged</option>
                          {categories.map((category) => <option key={category} value={category}>{category}</option>)}
                        </SelectControl>
                         <SelectControl value={correctedOwner} onChange={(event) => setCorrectedOwner(event.target.value)} aria-label="Correct owner">
                           <option value="">Owner unchanged</option>
                          {owners.map((owner) => <option key={owner} value={owner}>{owner}</option>)}
                        </SelectControl>
                         <SelectControl value={correctedPriority} onChange={(event) => setCorrectedPriority(event.target.value)} aria-label="Correct priority">
                           <option value="">Priority unchanged</option>
                          {priorities.map((priority) => <option key={priority} value={priority}>{priority}</option>)}
                        </SelectControl>
                         <SelectControl value={correctedSeverity} onChange={(event) => setCorrectedSeverity(event.target.value)} aria-label="Correct severity">
                           <option value="">Severity unchanged</option>
                          {severities.map((severity) => <option key={severity} value={severity}>{severity}</option>)}
                        </SelectControl>
                         <SelectControl value={correctedNeedsOperator} onChange={(event) => setCorrectedNeedsOperator(event.target.value)} aria-label="Correct operator requirement">
                           <option value="">Operator requirement unchanged</option>
                           <option value="true">Operator required</option>
                           <option value="false">Operator not required</option>
                        </SelectControl>
                         <Button type="button" disabled={saving || (!correctedAction && !correctedCategory && !correctedOwner && !correctedPriority && !correctedSeverity && !correctedNeedsOperator)} onClick={() => review(item.task_id, "corrected", { ...(correctedAction ? { action: correctedAction } : {}), ...(correctedCategory ? { category: correctedCategory } : {}), ...(correctedOwner ? { suggested_owner: correctedOwner } : {}), ...(correctedPriority ? { priority: correctedPriority } : {}), ...(correctedSeverity ? { severity: correctedSeverity } : {}), ...(correctedNeedsOperator ? { needs_operator: correctedNeedsOperator === "true" } : {}) }, reviewNote)}>Save correction</Button>
                      </div>
                    )}
                    <label className="luna-review-note">Operator note<textarea value={reviewNote} maxLength={1000} rows={3} placeholder="Review rationale or context" onChange={(event) => setReviewNote(event.target.value)} /></label>
                    </div>}
                  </article>
                );
              })}
            </div>
            {metrics.review_queue.length > 5 && (
              <div className="luna-review-more">
                <Button type="button" onClick={() => setShowAllReviews((shown) => !shown)}>
                  {showAllReviews ? "Show first 5" : `Load ${metrics.review_queue.length - 5} more`}
                </Button>
              </div>
            )}
          </section>
          {rejectingTaskId && (
            <ConfirmDialog
              title="Reject routing suggestion"
              message="Mark this decision as rejected? The verdict will update router quality metrics."
              confirmLabel="Reject suggestion"
              busy={savingTaskId === rejectingTaskId}
              onConfirm={() => review(rejectingTaskId, "rejected", {}, reviewNote).then(() => setRejectingTaskId(null))}
              onCancel={() => setRejectingTaskId(null)}
            />
          )}
        </>
      )}
    </div>
  );
}
