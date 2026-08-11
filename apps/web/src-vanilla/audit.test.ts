import { describe, expect, it } from "vitest";
import type { AuditEntry } from "../src/lib/types";
import { filterAuditEntries, outcomeLabel, outcomeTone } from "./audit";

const entries: AuditEntry[] = [
  { id: "1", created_at: "2026-08-11T10:00:00Z", actor: "operator", source: "web", action: "tool.execute", outcome: "completed", tool_id: "adguard.status", task_id: null, metadata: null },
  { id: "2", created_at: "2026-08-11T09:00:00Z", actor: "agent:opencode", source: "mcp", action: "approval.request", outcome: "pending", tool_id: null, task_id: "task-7", metadata: null },
];

describe("vanilla audit helpers", () => {
  it("maps outcome text to stable tones and labels", () => {
    expect(outcomeTone("completed")).toBe("success");
    expect(outcomeTone("approval_required")).toBe("warning");
    expect(outcomeTone("request_denied")).toBe("danger");
    expect(outcomeTone("observed")).toBe("neutral");
    expect(outcomeLabel("request_denied")).toBe("Error");
  });

  it("filters across audit fields without mutating the source", () => {
    expect(filterAuditEntries(entries, { query: "task-7", outcome: "" })).toEqual([entries[1]]);
    expect(filterAuditEntries(entries, { query: "", outcome: "completed" })).toEqual([entries[0]]);
    expect(entries).toHaveLength(2);
  });
});
