import { describe, expect, it } from "vitest";
import type { Approval, Incident, Task } from "../src/lib/types";
import { buildInboxItems } from "./inbox";

describe("vanilla operational inbox", () => {
  it("keeps only pending approvals and sorts critical incidents first", () => {
    const approvals = [
      { id: "a1", tool_id: "adguard.pause", action: "Pause filtering", status: "pending", task_id: null, requested_by: "agent:test", decided_by: "", created_at: "2026-08-11T10:00:00Z", expires_at: "2026-08-11T10:05:00Z", decided_at: null, consumed_at: null },
      { id: "a2", tool_id: "adguard.resume", action: "Resume filtering", status: "consumed", task_id: null, requested_by: "agent:test", decided_by: "operator", created_at: "2026-08-11T09:00:00Z", expires_at: "2026-08-11T09:05:00Z", decided_at: "2026-08-11T09:01:00Z", consumed_at: "2026-08-11T09:02:00Z" },
    ] satisfies Approval[];
    const incidents = [{ id: "i1", dedupe_key: "ups", watcher_id: "ups", status: "open", severity: "critical", provider_id: "nutups", title: "UPS on battery", description: "Line power is unavailable.", task_id: null, first_seen_at: "2026-08-11T10:01:00Z", last_seen_at: "2026-08-11T10:02:00Z", resolved_at: null, resolution_reason: "", missing_runs: 0, last_missing_at: null, occurrences: 1, payload: {}, root_cause_incident_id: null }] satisfies Incident[];
    const tasks = [{ id: "t1", title: "Check backup", goal: "Verify the latest snapshot.", status: "open", source: "operator", created_by: "operator", assigned_agent: "", claimed_at: null, last_activity_at: "2026-08-11T09:30:00Z", completed_at: null, version: 1, created_at: "2026-08-11T09:00:00Z", updated_at: "2026-08-11T09:30:00Z", summary: null }] satisfies Task[];

    const items = buildInboxItems(approvals, incidents, tasks);

    expect(items.map((item) => item.id)).toEqual(["incident:i1", "approval:a1", "task:t1"]);
  });
});
