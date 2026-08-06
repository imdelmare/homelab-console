import { useEffect, useMemo, useState } from "react";
import { Button } from "react95";
import { useQueryClient } from "@tanstack/react-query";
import { decideApproval, fetchApprovals } from "../lib/api";
import { formatCountdown, formatDateTime, shortId } from "../lib/format";
import { describeError } from "../lib/ui";
import { usePanelQuery } from "../lib/usePanelQuery";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PanelLoadingScreen } from "../components/PanelLoadingScreen";
import { SelectControl } from "../components/SelectControl";
import { StatusBadge } from "../components/StatusBadge";
import type { Approval, ApprovalStatus } from "../lib/types";

const STATUS_LABEL: Record<ApprovalStatus, string> = {
  pending: "Waiting",
  approved: "Approved",
  denied: "Denied",
  expired: "Expired",
  consumed: "Consumed",
};

function statusTone(status: string): "success" | "warning" | "danger" | "neutral" {
  if (status === "pending") return "warning";
  if (status === "approved") return "success";
  if (status === "denied") return "danger";
  return "neutral";
}

export function ApprovalsApp() {
  const queryClient = useQueryClient();
  const { data, loadState, isFetching, errorMessage, refresh } = usePanelQuery(
    ["approvals"],
    () => fetchApprovals({ limit: 100 }),
    // Pending approvals expire within minutes; keep this list fresher than
    // the app-wide cadence while the window is open.
    { refetchInterval: 5_000 },
  );
  const approvals = useMemo(() => data ?? [], [data]);
  const [statusFilter, setStatusFilter] = useState("");
  const [pendingDecision, setPendingDecision] = useState<{ approval: Approval; approve: boolean } | null>(null);
  const [decidingId, setDecidingId] = useState<string | null>(null);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const hasPending = approvals.some((approval) => approval.status === "pending");
  useEffect(() => {
    if (!hasPending) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [hasPending]);

  const filtered = useMemo(
    () => approvals.filter((approval) => !statusFilter || approval.status === statusFilter),
    [approvals, statusFilter],
  );
  const pendingCount = approvals.filter((approval) => approval.status === "pending").length;

  async function decide(approval: Approval, approve: boolean) {
    setPendingDecision(null);
    setDecidingId(approval.id);
    setDecisionError(null);
    try {
      await decideApproval(approval.id, approve);
    } catch (error) {
      setDecisionError(describeError(error));
    } finally {
      setDecidingId(null);
      void queryClient.invalidateQueries({ queryKey: ["approvals"] });
      void queryClient.invalidateQueries({ queryKey: ["audit"] });
    }
  }

  if (loadState === "loading") {
    return <PanelLoadingScreen label="Loading approvals…" />;
  }

  return (
    <div className="panel-app approvals-app">
      <div className="panel-toolbar">
        <Button onClick={refresh} disabled={isFetching}>Refresh</Button>
        <SelectControl aria-label="Filter by status" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
          <option value="">All statuses</option>
          <option value="pending">Waiting</option>
          <option value="approved">Approved</option>
          <option value="denied">Denied</option>
          <option value="expired">Expired</option>
          <option value="consumed">Consumed</option>
        </SelectControl>
        <span className="toolbar-count">
          {pendingCount > 0 ? `${pendingCount} pending` : "none pending"}
        </span>
      </div>
      <p className="audit-scope-note">
        Every write-tool execution requires an explicit approval, valid only once and only for the exact requested input. You can decide here or with the Telegram buttons.
      </p>
      {loadState === "error" && <p className="login-error">{errorMessage}</p>}
      {decisionError && <p className="login-error">{decisionError}</p>}
      <div className="audit-event-list">
        {filtered.map((approval) => {
          const isPending = approval.status === "pending";
          const expiresIn = formatCountdown(approval.expires_at, now);
          const effectivelyExpired = isPending && expiresIn === "0:00";
          return (
            <details className="audit-event" key={approval.id} open={isPending}>
              <summary>
                <StatusBadge
                  label={effectivelyExpired ? STATUS_LABEL.expired : STATUS_LABEL[approval.status] ?? approval.status}
                  tone={effectivelyExpired ? "neutral" : statusTone(approval.status)}
                />
                <strong>{approval.tool_id}</strong>
                <span>{approval.requested_by}</span>
                <time>{formatDateTime(approval.created_at)}</time>
                {isPending && !effectivelyExpired && <span className="toolbar-count">expires in {expiresIn}</span>}
              </summary>
              <dl>
                <div><dt>Request</dt><dd>{approval.action || approval.tool_id}</dd></div>
                <div><dt>Task</dt><dd>{approval.task_id ? shortId(approval.task_id) : "—"}</dd></div>
                <div><dt>Expiry</dt><dd>{formatDateTime(approval.expires_at)}</dd></div>
                <div><dt>Decided by</dt><dd>{approval.decided_by || "—"}</dd></div>
                <div><dt>Decided at</dt><dd>{formatDateTime(approval.decided_at)}</dd></div>
                <div><dt>Consumed at</dt><dd>{formatDateTime(approval.consumed_at)}</dd></div>
                <div><dt>ID</dt><dd>{approval.id}</dd></div>
              </dl>
              {isPending && !effectivelyExpired && (
                <div className="tool-row-actions">
                  <Button
                    disabled={decidingId === approval.id}
                    onClick={() => setPendingDecision({ approval, approve: true })}
                  >
                    ✅ Approve
                  </Button>
                  <Button
                    disabled={decidingId === approval.id}
                    onClick={() => void decide(approval, false)}
                  >
                    ⛔ Deny
                  </Button>
                </div>
              )}
            </details>
          );
        })}
        {loadState === "ready" && filtered.length === 0 && (
          <p className="section-state">
            {statusFilter ? "No approvals have this status." : "No approval requests recorded."}
          </p>
        )}
      </div>
      {pendingDecision && (
        <ConfirmDialog
          title="Confirm approval"
          message={`Approve “${pendingDecision.approval.action || pendingDecision.approval.tool_id}” requested by ${pendingDecision.approval.requested_by}? The approval is valid for one execution with this exact input.`}
          confirmLabel="Approve"
          onConfirm={() => void decide(pendingDecision.approval, pendingDecision.approve)}
          onCancel={() => setPendingDecision(null)}
        />
      )}
    </div>
  );
}
