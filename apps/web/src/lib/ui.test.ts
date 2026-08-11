import { describe, expect, it } from "vitest";
import type { McpClient, McpPairingRequest, Task, TaskEvent, ToolDefinition } from "./types";
import {
  buildToolInput,
  describeError,
  isMcpClientOnline,
  mcpPairingDisplayStatus,
  taskAgent,
  taskInitialRouting,
  taskRouterLabel,
  taskStatusActionLabel,
} from "./ui";

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    title: "Test task",
    goal: "Exercise labels",
    status: "open",
    source: "test",
    created_by: "user:test",
    assigned_agent: "",
    claimed_at: null,
    last_activity_at: "2026-07-16T12:00:00",
    completed_at: null,
    version: 1,
    created_at: "2026-07-16T12:00:00",
    updated_at: "2026-07-16T12:00:00",
    summary: null,
    ...overrides,
  };
}

function makeClient(overrides: Partial<McpClient> = {}): McpClient {
  return {
    id: "client-1",
    agent_id: "codex",
    client_label: "Codex workstation",
    host_fingerprint: "host-1",
    token_hint: "abcd",
    created_at: "2026-07-16T12:00:00",
    approved_at: "2026-07-16T12:00:00",
    last_seen_at: "2026-07-16T12:04:00",
    revoked_at: null,
    revoked_reason: "",
    created_by: "user:test",
    capabilities: [],
    principal_id: "agent:codex",
    ...overrides,
  };
}

function makeTool(): ToolDefinition {
  return {
    id: "test.read",
    name: "Test read tool",
    description: "Exercises input conversion",
    provider_id: "test",
    category: "test",
    mode: "read",
    risk: "low",
    enabled: true,
    requires_confirmation: false,
    timeout_seconds: 10,
    input_schema: {
      type: "object",
      required: ["host", "count"],
      properties: {
        host: { type: "string" },
        count: { type: "integer" },
        ratio: { type: "number" },
        enabled: { type: "boolean" },
        ports: { type: "array", items: { type: "integer" } },
        tags: { type: "array", items: { type: "string" } },
        note: { type: "string" },
      },
    },
  };
}

describe("task labels", () => {
  it("maps task status actions and preserves an unknown fallback", () => {
    expect(taskStatusActionLabel("investigating")).toBe("Start investigation");
    expect(taskStatusActionLabel("paused")).toBe("Set status to paused");
  });

  it("uses canonical ownership and completion labels", () => {
    expect(taskAgent(makeTask({ assigned_agent: "agent:codex" }))).toBe("Codex");
    expect(taskAgent(makeTask({ assigned_agent: "user:operator" }))).toBe("operator");
    expect(taskAgent(makeTask({ status: "completed", resolution_label: "human_handled" }))).toBe("Handled by human");
    expect(taskAgent(makeTask({ status: "completed", auto_closed: true }))).toBe("Auto closed");
    expect(taskAgent(makeTask({ status: "completed", resolution_label: "operator_handled" }))).toBe(
      "Closed by operator",
    );
  });

  it("maps router states", () => {
    expect(taskRouterLabel(makeTask({ router_status: "queued" }))).toBe("Routing queued");
    expect(taskRouterLabel(makeTask({ router_status: "running" }))).toBe("Routing in progress");
    expect(taskRouterLabel(makeTask({ router_status: "routed" }))).toBe("Routing completed");
    expect(taskRouterLabel(makeTask({ router_status: "policy_failed" }))).toBe(
      "Routed; follow-up policy failed",
    );
    expect(taskRouterLabel(makeTask({ router_status: "failed" }))).toBe("Routing failed");
    expect(taskRouterLabel(makeTask())).toBe("");
  });
});

describe("taskInitialRouting", () => {
  it("extracts the latest structured routing decision", () => {
    const events: TaskEvent[] = [
      { id: "event-1", kind: "task.created", payload: {}, created_at: "2026-07-16T12:00:00Z" },
      {
        id: "event-2",
        kind: "task.router_decision",
        created_at: "2026-07-16T12:00:01Z",
        payload: {
          model: "gpt-5.6-luna",
          decision: {
            action: "keep",
            category: "network",
            priority: "high",
            severity: "critical",
            suggested_owner: "claude",
            confidence: 0.82,
            summary: "Investigate the gateway state.",
            runbook: "gateway_alert",
            labels: ["network", "gateway"],
          },
        },
      },
    ];

    expect(taskInitialRouting(events)).toEqual({
      status: "routed",
      createdAt: "2026-07-16T12:00:01Z",
      model: "gpt-5.6-luna",
      category: "network",
      priority: "high",
      severity: "critical",
      suggestedOwner: "claude",
      action: "keep",
      confidence: 0.82,
      summary: "Investigate the gateway state.",
      runbook: "gateway_alert",
      labels: ["network", "gateway"],
      failureMessage: "",
      failureReason: "",
    });
  });

  it("extracts safe failure diagnostics and ignores unrelated events", () => {
    const events: TaskEvent[] = [
      {
        id: "event-1",
        kind: "task.router_failed",
        created_at: "2026-07-16T12:00:01Z",
        payload: {
          error: {
            message: "task router model response incomplete",
            details: { incomplete_reason: "max_output_tokens" },
          },
        },
      },
      { id: "event-2", kind: "task.claimed", payload: {}, created_at: "2026-07-16T12:01:00Z" },
    ];

    expect(taskInitialRouting(events)).toMatchObject({
      status: "failed",
      failureMessage: "task router model response incomplete",
      failureReason: "max_output_tokens",
    });
    expect(taskInitialRouting([events[1]])).toBeNull();
  });
});

describe("describeError", () => {
  it("preserves intentional client-side validation messages", () => {
    expect(describeError(new Error("Invalid integer: count"))).toBe("Invalid integer: count");
  });
});

describe("isMcpClientOnline", () => {
  const now = Date.parse("2026-07-16T12:05:00Z");

  it("accepts a recent heartbeat", () => {
    expect(isMcpClientOnline(makeClient(), now)).toBe(true);
  });

  it("rejects stale, missing, and revoked clients", () => {
    expect(isMcpClientOnline(makeClient({ last_seen_at: "2026-07-16T12:02:59" }), now)).toBe(false);
    expect(isMcpClientOnline(makeClient({ last_seen_at: null }), now)).toBe(false);
    expect(isMcpClientOnline(makeClient({ revoked_at: "2026-07-16T12:04:30" }), now)).toBe(false);
  });
});

describe("mcpPairingDisplayStatus", () => {
  const request: McpPairingRequest = {
    id: "request-1",
    agent_id: "codex",
    client_label: "Codex workstation",
    host_fingerprint: "host-1",
    status: "pending",
    created_at: "2026-07-16T12:00:00",
    expires_at: "2026-07-16T12:05:00",
    approved_at: null,
    denied_at: null,
    consumed_at: null,
    decided_by: "",
    delivery_status: "sent",
  };

  it("shows a pending request as expired once its UTC deadline passes", () => {
    expect(mcpPairingDisplayStatus(request, Date.parse("2026-07-16T12:05:00Z"))).toBe("expired");
  });

  it("does not override terminal or approved states", () => {
    expect(
      mcpPairingDisplayStatus(
        { ...request, status: "approved", approved_at: "2026-07-16T12:04:00" },
        Date.parse("2026-07-16T12:06:00Z"),
      ),
    ).toBe("approved");
    expect(mcpPairingDisplayStatus(request, Date.parse("2026-07-16T12:04:59Z"))).toBe("pending");
  });
});

describe("buildToolInput", () => {
  it("converts schema-backed form values and omits empty optional inputs", () => {
    expect(
      buildToolInput(makeTool(), {
        host: "pve-1",
        count: "2",
        ratio: "1.5",
        enabled: "yes",
        ports: "8006, 22",
        tags: "lab, primary",
        note: "",
      }),
    ).toEqual({
      host: "pve-1",
      count: 2,
      ratio: 1.5,
      enabled: true,
      ports: [8006, 22],
      tags: ["lab", "primary"],
    });
  });

  it("rejects missing required and malformed numeric inputs", () => {
    expect(() => buildToolInput(makeTool(), { host: "pve-1", count: "" })).toThrow(
      "Missing required input: count",
    );
    expect(() => buildToolInput(makeTool(), { host: "pve-1", count: "two" })).toThrow(
      "Invalid integer: count",
    );
    expect(() => buildToolInput(makeTool(), { host: "pve-1", count: "2.5" })).toThrow(
      "Invalid integer: count",
    );
    expect(() =>
      buildToolInput(makeTool(), { host: "pve-1", count: "2", ports: "22oops" }),
    ).toThrow("Invalid array value for ports: 22oops");
    expect(() =>
      buildToolInput(makeTool(), { host: "pve-1", count: "2", enabled: "perhaps" }),
    ).toThrow("Invalid boolean: enabled");
  });
});
