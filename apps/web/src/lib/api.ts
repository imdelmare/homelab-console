import type {
  ApiErrorBody,
  Approval,
  AuditEntry,
  AuthConfig,
  AuthCompleteResponse,
  CapabilityObservation,
  ChallengePoll,
  ConversationStatus,
  Incident,
  LoginChallenge,
  LunaMetrics,
  TaskRouterCorrections,
  McpClient,
  McpPairingRequest,
  McpPairingConsumeResult,
  McpPairingStart,
  McpRotateResult,
  OperationalHealth,
  OpsError,
  Provider,
  ProviderDefinition,
  Runbook,
  SessionResponse,
  Task,
  TaskContext,
  TaskDetail,
  TaskEvent,
  TopologyGraph,
  TopologySnapshot,
  ToolDefinition,
  ToolRunResult,
  WatcherRun,
  WatcherRunResult,
  WatcherStatus,
} from "./types";

// Empty means "same origin, relative /api/... paths" — every call below
// already prefixes its path with /api. In dev, vite.config.ts proxies /api
// (and /health) to the backend so this stays empty there too.
export const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "";

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null) {
  unauthorizedHandler = handler;
}

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

type RequestOptions = {
  method?: string;
  body?: unknown;
  cache?: RequestCache;
  // Skip triggering the global "drop to login" handler on 401. Used by the
  // auth endpoints themselves, where a 401 is an expected outcome (bad
  // credentials) rather than a signal that the session dropped.
  skipAuthRedirect?: boolean;
};

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, cache, skipAuthRedirect = false } = options;
  const headers: Record<string, string> = {};
  let payload: string | undefined;

  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  if (method !== "GET" && csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
  }

  const response = await fetch(`${apiBaseUrl}${path}`, {
    method,
    headers,
    body: payload,
    cache,
    credentials: "include",
  });

  if (response.status === 401 && !skipAuthRedirect) {
    unauthorizedHandler?.();
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!response.ok) {
    const errorBody = data as ApiErrorBody | null;
    const detail = errorBody?.detail;
    const message =
      errorBody?.error?.message ??
      errorBody?.message ??
      (typeof detail === "object" ? detail?.message : detail) ??
      `Request failed with status ${response.status}`;
    throw new ApiError(response.status, message, data);
  }

  return data as T;
}

// --- Auth ---

export function fetchAuthConfig() {
  return request<AuthConfig>("/api/auth/config", {
    cache: "no-store",
    skipAuthRedirect: true,
  });
}

export function fetchSession() {
  return request<SessionResponse>("/api/auth/session", { cache: "no-store", skipAuthRedirect: true });
}

export function login(username: string, password?: string) {
  return request<LoginChallenge>("/api/auth/login", {
    method: "POST",
    body: password === undefined ? { username } : { username, password },
    skipAuthRedirect: true,
  });
}

export function pollChallenge(challengeId: string) {
  return request<ChallengePoll>(`/api/auth/challenge/${encodeURIComponent(challengeId)}`, {
    skipAuthRedirect: true,
  });
}

export function completeChallenge(challengeId: string) {
  return request<AuthCompleteResponse>("/api/auth/complete", {
    method: "POST",
    body: { challenge_id: challengeId },
    skipAuthRedirect: true,
  });
}

export function verifyOtp(challengeId: string, otp: string) {
  return request<AuthCompleteResponse>("/api/auth/verify-otp", {
    method: "POST",
    body: { challenge_id: challengeId, otp },
    skipAuthRedirect: true,
  });
}

export function logout() {
  return request<void>("/api/auth/logout", { method: "POST" });
}

// --- Domain ---

export function fetchProviders() {
  return request<Provider[]>("/api/providers");
}

export function fetchProviderDefinitions() {
  return request<ProviderDefinition[]>("/api/provider-definitions");
}

export function fetchCapabilityObservations(providerId?: string) {
  const path = providerId
    ? `/api/providers/${encodeURIComponent(providerId)}/observations`
    : "/api/observations";
  return request<CapabilityObservation[]>(path);
}

export function fetchTools() {
  return request<ToolDefinition[]>("/api/tools");
}

export function runTool(toolId: string, input: Record<string, unknown>, approvalId?: string) {
  return request<ToolRunResult>(`/api/tools/${encodeURIComponent(toolId)}/run`, {
    method: "POST",
    body: approvalId ? { input, approval_id: approvalId } : { input },
  });
}

export function requestApproval(toolId: string, input: Record<string, unknown>, taskId?: string) {
  return request<Approval>("/api/approvals", {
    method: "POST",
    body: { tool_id: toolId, input, ...(taskId ? { task_id: taskId } : {}) },
  });
}

export function fetchApprovals(options: { status?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (options.status) params.set("status", options.status);
  if (options.limit) params.set("limit", String(options.limit));
  const query = params.toString();
  return request<Approval[]>(`/api/approvals${query ? `?${query}` : ""}`);
}

export function decideApproval(approvalId: string, approve: boolean) {
  return request<Approval & { outcome: string }>(
    `/api/approvals/${encodeURIComponent(approvalId)}/decide`,
    { method: "POST", body: { approve } },
  );
}

export function fetchTasks(options: { status?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (options.status) params.set("status", options.status);
  if (options.limit) params.set("limit", String(options.limit));
  const query = params.toString();
  return request<Task[]>(`/api/tasks${query ? `?${query}` : ""}`);
}

export function createTask(title: string, goal: string) {
  return request<Task>("/api/tasks", {
    method: "POST",
    body: { title, goal },
  });
}

export function createProviderTask(providerId: string, note = "") {
  return request<Task>(`/api/providers/${encodeURIComponent(providerId)}/task`, {
    method: "POST",
    body: { note },
  });
}

export function fetchTaskDetail(taskId: string) {
  return request<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}`);
}

export function fetchTaskContext(taskId: string) {
  return request<TaskContext>(`/api/tasks/${encodeURIComponent(taskId)}/context`);
}

export function claimTask(
  taskId: string,
  agentId: "agent:claude" | "agent:fixer" | "agent:codex" | "agent:cline" | "agent:opencode",
) {
  return request<Task>(`/api/tasks/${encodeURIComponent(taskId)}/claim`, {
    method: "POST",
    body: { agent_id: agentId },
  });
}

export function claimTaskAsOperator(taskId: string, expectedVersion: number) {
  return request<Task>(`/api/tasks/${encodeURIComponent(taskId)}/claim-self`, {
    method: "POST",
    body: { expected_version: expectedVersion },
  });
}

export function addTaskNote(taskId: string, note: string) {
  return request<TaskEvent>(`/api/tasks/${encodeURIComponent(taskId)}/notes`, {
    method: "POST",
    body: { note },
  });
}

export function handoffTaskToClient(taskId: string, clientId: string, note: string, expectedVersion: number) {
  return request<Task>(`/api/tasks/${encodeURIComponent(taskId)}/handoff-to-client`, {
    method: "POST",
    body: { client_id: clientId, note, expected_version: expectedVersion },
  });
}

export function completeTaskAsOperator(taskId: string, note: string, expectedVersion: number) {
  return request<Task>(`/api/tasks/${encodeURIComponent(taskId)}/complete-as-operator`, {
    method: "POST",
    body: { note, expected_version: expectedVersion },
  });
}

export type FixerDispatchResult = {
  task: Task;
  dispatch: {
    ok: boolean;
    code: string;
    message: string;
    duration_ms: number;
  };
};

export function dispatchTaskToFixer(taskId: string) {
  return request<FixerDispatchResult>(`/api/tasks/${encodeURIComponent(taskId)}/dispatch-fixer`, {
    method: "POST",
  });
}

export function assignWorkerTask(taskId: string, clientId: string, expectedVersion: number) {
  return request<{ task: Task }>(`/api/tasks/${encodeURIComponent(taskId)}/assign-worker`, {
    method: "POST",
    body: { client_id: clientId, expected_version: expectedVersion },
  }).then(({ task }) => task);
}
export function releaseTaskWithHandoff(taskId: string, expectedVersion: number, handoffSummary: string) {
  return request<Task>(`/api/tasks/${encodeURIComponent(taskId)}/release`, {
    method: "POST",
    body: { expected_version: expectedVersion, handoff_summary: handoffSummary },
  });
}

export function completeTask(taskId: string, expectedVersion: number) {
  return request<Task>(`/api/tasks/${encodeURIComponent(taskId)}/complete`, {
    method: "POST",
    body: { expected_version: expectedVersion },
  });
}

export function reopenTask(taskId: string, expectedVersion: number) {
  return request<Task>(`/api/tasks/${encodeURIComponent(taskId)}/reopen`, {
    method: "POST",
    body: { expected_version: expectedVersion },
  });
}

export function setTaskStatus(taskId: string, status: string, expectedVersion: number) {
  return request<Task>(`/api/tasks/${encodeURIComponent(taskId)}/status`, {
    method: "POST",
    body: { status, expected_version: expectedVersion },
  });
}

export function fetchAudit(limit = 100) {
  return request<AuditEntry[]>(`/api/audit?limit=${limit}`);
}

export function fetchTopology() {
  return request<TopologyGraph>("/api/topology");
}

export function fetchTopologySnapshot(force = false) {
  return request<TopologySnapshot>(`/api/topology/snapshot${force ? "?force=true" : ""}`);
}

export function fetchMcpClients() {
  return request<McpClient[]>("/api/mcp/clients");
}

export function fetchMcpPairingRequests(limit = 25) {
  return request<McpPairingRequest[]>(`/api/mcp/pairing/requests?limit=${limit}`);
}

export function revokeMcpClient(clientId: string, reason: string) {
  return request<McpClient>(`/api/mcp/clients/${encodeURIComponent(clientId)}/revoke`, {
    method: "POST",
    body: { reason },
  });
}

export function forgetMcpClient(clientId: string) {
  return request<void>(`/api/mcp/clients/${encodeURIComponent(clientId)}`, {
    method: "DELETE",
  });
}

export function rotateMcpClient(clientId: string) {
  return request<McpRotateResult>(`/api/mcp/clients/${encodeURIComponent(clientId)}/rotate`, {
    method: "POST",
  });
}

export function setMcpClientCapabilities(
  clientId: string,
  capabilities: string[],
  confirmWorkerConversion = false,
) {
  return request<McpClient>(`/api/mcp/clients/${encodeURIComponent(clientId)}/capabilities`, {
    method: "PUT",
    body: { capabilities, confirm_worker_conversion: confirmWorkerConversion },
  });
}

export function startMcpPairing(payload: {
  agent_id: "codex" | "claude" | "fixer" | "cline" | "opencode" | "worker";
  client_label: string;
  host_fingerprint: string;
}) {
  return request<McpPairingStart>("/api/mcp/pairing/start", {
    method: "POST",
    body: payload,
  });
}

export function consumeMcpPairing(requestId: string, pairingSecret: string) {
  return request<McpPairingConsumeResult>("/api/mcp/pairing/consume", {
    method: "POST",
    body: { request_id: requestId, pairing_secret: pairingSecret },
  });
}

export function fetchRunbooks() {
  return request<Runbook[]>("/api/runbooks");
}

export function fetchOpsErrors(limit = 20) {
  return request<OpsError[]>(`/api/ops/errors?limit=${limit}`);
}

export function fetchOperationalHealth() {
  return request<OperationalHealth>("/api/ops/health");
}

export function fetchIncidents(options: { status?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (options.status !== undefined) params.set("status", options.status);
  if (options.limit) params.set("limit", String(options.limit));
  const query = params.toString();
  return request<Incident[]>(`/api/watchers/incidents${query ? `?${query}` : ""}`);
}

export function fetchWatcherRuns(limit = 50) {
  return request<WatcherRun[]>(`/api/watchers/runs?limit=${limit}`);
}

export function fetchWatcherStatus() {
  return request<WatcherStatus>("/api/watchers/status");
}

export function updateWatcherAutomation(enabled: boolean) {
  return request<WatcherStatus>("/api/watchers/automation", {
    method: "POST",
    body: { enabled },
  });
}

export function updateWatcherConfig(
  watcherId: string,
  payload: {
    enabled?: boolean;
    interval_seconds?: number;
    min_severity?: "warning" | "critical";
    investigation_mode?: "manual" | "auto_investigate";
  },
) {
  return request<WatcherStatus>(`/api/watchers/config/${encodeURIComponent(watcherId)}`, {
    method: "PATCH",
    body: payload,
  });
}

export function runWatchers(watcherIds: string[] = []) {
  return request<WatcherRunResult>("/api/watchers/run", {
    method: "POST",
    body: { watcher_ids: watcherIds },
  });
}

export function resolveIncidentHandled(incidentId: string, note = "") {
  return request<Incident>(`/api/watchers/incidents/${incidentId}/resolve-handled`, {
    method: "POST",
    body: { note },
  });
}

export function fetchLunaMetrics(days = 30) {
  return request<LunaMetrics>(`/api/luna/metrics?days=${days}`);
}

export function fetchConversationStatus() {
  return request<ConversationStatus>("/api/conversations/status");
}

export function reviewTaskRouter(
  taskId: string,
  payload: {
    verdict: "accepted" | "corrected" | "rejected";
    corrections?: TaskRouterCorrections;
    note?: string;
  },
) {
  return request(`/api/luna/tasks/${encodeURIComponent(taskId)}/review`, {
    method: "POST",
    body: payload,
  });
}
