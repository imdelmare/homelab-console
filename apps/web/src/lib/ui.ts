// Pure helpers shared by the desktop app panels. No React in here so the
// whole module is unit-testable.
import { ApiError } from "./api";
import { parseApiDate } from "./format";
import type { McpClient, McpPairingRequest, Task, TaskEvent, ToolDefinition } from "./types";

export type LoadState = "loading" | "ready" | "error";
export type JsonRecord = Record<string, unknown>;

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "Unable to reach the server.";
}

export function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function asRecord(value: unknown): JsonRecord {
  return isRecord(value) ? value : {};
}

export function asList(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

export function text(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") {
    return fallback;
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(Math.round(value * 1000) / 1000);
  }
  if (typeof value === "boolean") {
    return value ? "yes" : "no";
  }
  return String(value);
}

export function shortId(value: string | null | undefined): string {
  return value ? value.slice(0, 8) : "—";
}

export function statusClass(value: string | null | undefined): string {
  return (value || "unknown").replace(/[^a-z0-9_-]/gi, "-").toLowerCase();
}

export function formatBytes(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "—";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = value;
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  return `${amount >= 10 ? amount.toFixed(1) : amount.toFixed(2)} ${units[unitIndex]}`;
}

// Mirrors RELEASABLE_STATUSES in app/services/tasks_service.py.
export const RELEASABLE_STATUSES = new Set(["claimed", "investigating", "waiting_operator", "blocked"]);

// Mirrors TASK_TRANSITIONS in app/services/tasks_service.py. The backend is
// still the source of truth (it validates every transition); this only
// decides which options this generic control offers so an operator isn't
// stuck without a way to move a task that an agent left in a stale status
// (e.g. claimed but never set to investigating).
export const TASK_TRANSITIONS: Record<string, string[]> = {
  open: ["claimed", "cancelled"],
  claimed: ["investigating", "open", "cancelled"],
  investigating: ["waiting_operator", "blocked", "completed", "cancelled"],
  waiting_operator: ["investigating", "cancelled"],
  blocked: ["investigating", "cancelled"],
  completed: ["open"],
  cancelled: [],
};

const TASK_STATUS_LABELS: Record<string, string> = {
  open: "Reopen",
  claimed: "Claim",
  investigating: "Start investigation",
  waiting_operator: "Put on hold",
  blocked: "Block",
  completed: "Complete",
  cancelled: "Cancel",
};

export function taskStatusActionLabel(status: string): string {
  return TASK_STATUS_LABELS[status] ?? `Set status to ${status}`;
}

const TASK_STATUS_DISPLAY: Record<string, string> = {
  open: "Open",
  claimed: "Claimed",
  investigating: "Investigating",
  waiting_operator: "Waiting for operator",
  blocked: "Blocked",
  completed: "Completed",
  resolved: "Resolved",
  cancelled: "Canceled",
};

export function taskStatusLabel(status: string): string {
  return TASK_STATUS_DISPLAY[status] ?? status;
}

// assigned_agent is the only authoritative "who's working on this" field.
export function taskAgent(task: Pick<Task, "assigned_agent" | "status" | "resolution_label" | "auto_closed">): string {
  if (task.status === "completed" && (task.auto_closed || task.resolution_label === "auto_closed")) return "Auto closed";
  if (task.status === "completed" && task.resolution_label === "human_handled") return "Handled by human";
  if (task.status === "completed" && task.resolution_label === "operator_handled") return "Closed by operator";
  if (task.status === "completed" && task.resolution_label === "already_handled") return "Already handled";
  if (task.assigned_agent === "agent:claude") return "Claude";
  if (task.assigned_agent === "agent:fixer") return "Fixer";
  if (task.assigned_agent === "agent:codex") return "Codex";
  if (task.assigned_agent === "agent:cline") return "Cline";
  if (task.assigned_agent === "agent:opencode") return "OpenCode";
  if (task.assigned_agent.startsWith("user:")) return task.assigned_agent.slice("user:".length);
  return "Unassigned";
}

export function taskRouterLabel(task: Pick<Task, "router_status">): string {
  if (task.router_status === "queued") return "Routing queued";
  if (task.router_status === "running") return "Routing in progress";
  if (task.router_status === "routed") return "Routing completed";
  if (task.router_status === "policy_failed") return "Routed; follow-up policy failed";
  if (task.router_status === "failed") return "Routing failed";
  return "";
}

export type TaskRoutingSummary = {
  status: "routed" | "failed";
  createdAt: string;
  model: string;
  category: string;
  priority: string;
  severity: string;
  suggestedOwner: string;
  action: string;
  confidence: number | null;
  summary: string;
  runbook: string;
  labels: string[];
  failureMessage: string;
  failureReason: string;
};

export function taskInitialRouting(events: TaskEvent[]): TaskRoutingSummary | null {
  const event = [...events]
    .reverse()
    .find((candidate) => candidate.kind === "task.router_decision" || candidate.kind === "task.router_failed");
  if (!event) return null;

  const payload = asRecord(event.payload);
  if (event.kind === "task.router_failed") {
    const error = asRecord(payload.error);
    const details = asRecord(error.details);
    return {
      status: "failed",
      createdAt: event.created_at,
      model: "",
      category: "",
      priority: "",
      severity: "",
      suggestedOwner: "",
      action: "",
      confidence: null,
      summary: "",
      runbook: "",
      labels: [],
      failureMessage: text(error.message, "No routing decision was stored."),
      failureReason: text(details.incomplete_reason, ""),
    };
  }

  const decision = asRecord(payload.decision);
  const confidence = decision.confidence;
  return {
    status: "routed",
    createdAt: event.created_at,
    model: text(payload.model, ""),
    category: text(decision.category, "unknown"),
    priority: text(decision.priority, "medium"),
    severity: text(decision.severity, "warning"),
    suggestedOwner: text(decision.suggested_owner, "operator"),
    action: text(decision.action, "operator_review"),
    confidence: typeof confidence === "number" && Number.isFinite(confidence) ? confidence : null,
    summary: text(decision.summary, ""),
    runbook: text(decision.runbook, ""),
    labels: Array.isArray(decision.labels)
      ? decision.labels.filter((label): label is string => typeof label === "string" && Boolean(label.trim()))
      : [],
    failureMessage: "",
    failureReason: "",
  };
}

export function isMcpClientOnline(client: McpClient, now: number = Date.now()): boolean {
  if (client.revoked_at || !client.last_seen_at) {
    return false;
  }
  return now - parseApiDate(client.last_seen_at).getTime() < 120_000;
}

export function mcpPairingDisplayStatus(request: McpPairingRequest, now: number = Date.now()): string {
  if (request.status === "pending" && parseApiDate(request.expires_at).getTime() <= now) {
    return "expired";
  }
  return request.status;
}

export function toolInputProperties(tool: ToolDefinition): Array<[string, JsonRecord]> {
  return Object.entries(asRecord(tool.input_schema.properties)).filter(([, schema]) => isRecord(schema)) as Array<[string, JsonRecord]>;
}

export function toolInputRequired(tool: ToolDefinition): string[] {
  const required = tool.input_schema.required;
  return Array.isArray(required) ? required.filter((item): item is string => typeof item === "string") : [];
}

export function schemaType(schema: JsonRecord): string {
  if (typeof schema.type === "string") {
    return schema.type;
  }
  const anyOf = Array.isArray(schema.anyOf) ? schema.anyOf.filter(isRecord) : [];
  const nonNull = anyOf.find((item) => item.type !== "null");
  return typeof nonNull?.type === "string" ? nonNull.type : "string";
}

export function schemaArrayItemType(schema: JsonRecord): string {
  const directItems = asRecord(schema.items);
  if (typeof directItems.type === "string") {
    return directItems.type;
  }
  const anyOf = Array.isArray(schema.anyOf) ? schema.anyOf.filter(isRecord) : [];
  const arraySchema = anyOf.find((item) => item.type === "array");
  const items = asRecord(arraySchema?.items);
  return typeof items.type === "string" ? items.type : "string";
}

export function defaultToolInput(tool: ToolDefinition): Record<string, string> {
  return Object.fromEntries(
    toolInputProperties(tool).map(([key, schema]) => [
      key,
      schema.default === null || schema.default === undefined ? "" : String(schema.default),
    ]),
  );
}

export function hasMissingRequiredInput(tool: ToolDefinition, values: Record<string, string>): boolean {
  return toolInputRequired(tool).some((key) => !(values[key] ?? "").trim());
}

function parseToolNumber(raw: string, key: string, integer: boolean): number {
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || (integer && !Number.isInteger(parsed))) {
    throw new Error(`Invalid ${integer ? "integer" : "number"}: ${key}`);
  }
  return parsed;
}

export function buildToolInput(tool: ToolDefinition, values: Record<string, string>): Record<string, unknown> {
  const required = new Set(toolInputRequired(tool));
  const input: Record<string, unknown> = {};

  for (const [key, schema] of toolInputProperties(tool)) {
    const raw = (values[key] ?? "").trim();
    if (!raw && !required.has(key)) {
      continue;
    }
    if (!raw && required.has(key)) {
      throw new Error(`Missing required input: ${key}`);
    }

    const type = schemaType(schema);
    if (type === "integer") {
      input[key] = parseToolNumber(raw, key, true);
    } else if (type === "number") {
      input[key] = parseToolNumber(raw, key, false);
    } else if (type === "array") {
      const itemType = schemaArrayItemType(schema);
      const parts = raw.split(",").map((item) => item.trim()).filter(Boolean);
      input[key] =
        itemType === "integer" || itemType === "number"
          ? parts.map((item) => {
              try {
                return parseToolNumber(item, key, itemType === "integer");
              } catch {
                throw new Error(`Invalid array value for ${key}: ${item}`);
              }
            })
          : parts;
    } else if (type === "boolean") {
      const normalized = raw.toLowerCase();
      if (["1", "true", "yes", "on"].includes(normalized)) {
        input[key] = true;
      } else if (["0", "false", "no", "off"].includes(normalized)) {
        input[key] = false;
      } else {
        throw new Error(`Invalid boolean: ${key}`);
      }
    } else {
      input[key] = raw;
    }
  }

  return input;
}
