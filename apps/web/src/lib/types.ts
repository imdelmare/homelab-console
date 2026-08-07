export type AppId = "overview" | "providers" | "tools" | "tasks" | "watchers" | "luna" | "delivery" | "topology" | "mcp" | "audit" | "approvals";

export type WindowState = {
  id: AppId;
  title: string;
  x: number;
  y: number;
  width: number;
  height: number;
  minWidth: number;
  minHeight: number;
  zIndex: number;
  isOpen: boolean;
  isMinimized: boolean;
  isMaximized: boolean;
  restoreBounds?: {
    x: number;
    y: number;
    width: number;
    height: number;
  };
};

// --- Auth ---

export type SessionUser = {
  id: string;
  username: string;
  [key: string]: unknown;
};

export type SessionResponse =
  | { authenticated: true; user: SessionUser; csrf_token: string }
  | { authenticated: false };

export type AuthLoginMode = "password" | "telegram_only";

export type AuthConfig = {
  app_name: string;
  environment: string;
  login_mode: AuthLoginMode;
  methods: {
    telegram_approval: boolean;
    otp_fallback: boolean;
    recovery: boolean;
  };
};

export type LoginChallenge = {
  challenge_id: string;
  expires_at: string;
  delivery_status: string;
  methods: string[];
};

export type ChallengeStatus = "pending" | "approved" | "rejected" | "expired" | "consumed";

export type ChallengePoll = {
  status: ChallengeStatus;
  expires_at: string;
};

export type AuthCompleteResponse = {
  authenticated: true;
  user: SessionUser;
  csrf_token: string;
};

export type ApiErrorBody = {
  error?: { code?: string; message?: string };
  detail?: string | { code?: string; message?: string };
  message?: string;
};

// --- Domain ---

export type ProviderStatus =
  | "healthy"
  | "degraded"
  | "unreachable"
  | "unavailable"
  | "misconfigured"
  | "unknown";

export type Provider = {
  id: string;
  name: string;
  status: ProviderStatus;
  last_ok_at: string | null;
  checked_at: string | null;
  detail: string | null;
  tool_count: number;
  watchers: string[];
  last_error: {
    status: ProviderStatus;
    message: string;
    at: string;
  } | null;
};

export type ProviderDefinition = {
  id: string;
  name: string;
  transport: "http_json" | "tcp_text";
  driver_id: string;
  configuration_keys: string[];
  capability_tool_ids: string[];
  observation_ids: string[];
  supports_instances: boolean;
};

export type ToolMode = "read" | "write";

export type ToolDefinition = {
  id: string;
  name: string;
  description: string;
  provider_id: string;
  category: string;
  mode: ToolMode;
  risk: string;
  enabled: boolean;
  requires_confirmation: boolean;
  timeout_seconds: number;
  input_schema: Record<string, unknown>;
};

export type ToolRunSuccess = {
  ok: true;
  invocation_id: string;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  result: unknown;
};

export type ToolRunFailure = {
  ok: false;
  error: { code: string; message: string };
};

export type ToolRunResult = ToolRunSuccess | ToolRunFailure;

export type ApprovalStatus = "pending" | "approved" | "denied" | "expired" | "consumed";

export type Approval = {
  id: string;
  tool_id: string;
  action: string;
  status: ApprovalStatus;
  task_id: string | null;
  requested_by: string;
  decided_by: string;
  created_at: string;
  expires_at: string;
  decided_at: string | null;
  consumed_at: string | null;
};

export type Task = {
  id: string;
  title: string;
  goal: string;
  status: string;
  source: string;
  created_by: string;
  assigned_agent: string;
  claimed_at: string | null;
  last_activity_at: string;
  completed_at: string | null;
  version: number;
  router_status?: string;
  resolution_label?: string;
  auto_closed?: boolean;
  created_at: string;
  updated_at: string;
  summary: string | null;
};

export type TaskFinding = {
  id: string;
  task_id: string;
  severity: "info" | "warning" | "critical";
  title: string;
  description: string;
  source: string;
  tool_invocation_id: string | null;
  created_by: string;
  created_at: string;
  resolved_at: string | null;
};

export type TaskCheck = {
  id: string;
  task_id: string;
  description: string;
  status: "pending" | "completed" | "skipped";
  skip_reason: string;
  created_by: string;
  completed_by: string;
  created_at: string;
  completed_at: string | null;
};

export type TaskEvent = {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type TaskInvocation = {
  id: string;
  tool_id: string;
  status: string;
  error_code: string;
  started_at: string;
  duration_ms: number;
};

export type TaskDetail = Task & {
  events: TaskEvent[];
  findings: TaskFinding[];
  checks: TaskCheck[];
  invocations: TaskInvocation[];
};

export type TaskContextIncident = {
  id?: string;
  type: string;
  dedupe_key?: string;
  watcher_id?: string;
  status?: string;
  severity?: "info" | "warning" | "critical";
  provider_id?: string;
  title?: string;
  description?: string;
  first_seen_at?: string;
  last_seen_at?: string;
  occurrences?: number;
  missing_runs?: number;
  payload?: Record<string, unknown>;
};

export type TaskContextProviderState = {
  provider_id: string;
  display_name?: string;
  enabled?: boolean;
  status: ProviderStatus | string;
  last_ok_at: string | null;
  updated_at?: string;
};

export type TaskContextRecommendedTool = {
  tool_id: string;
  name: string;
  provider_id: string;
  reason: string;
  input_schema: Record<string, unknown>;
};

export type TaskContext = {
  task: Task;
  incident: TaskContextIncident | null;
  provider_ids: string[];
  provider_states: TaskContextProviderState[];
  brief: string;
  recommended_tools: TaskContextRecommendedTool[];
  budget: { max_tool_calls: number; max_minutes: number };
  stop_conditions: string[];
  recommended_next_step: string;
};

export type Incident = {
  id: string;
  dedupe_key: string;
  watcher_id: string;
  status: string;
  severity: "info" | "warning" | "critical";
  provider_id: string;
  title: string;
  description: string;
  task_id: string | null;
  first_seen_at: string;
  last_seen_at: string;
  resolved_at: string | null;
  resolution_reason: string;
  missing_runs: number;
  last_missing_at: string | null;
  occurrences: number;
  payload: Record<string, unknown>;
  root_cause_incident_id: string | null;
  dedupe_basis?: string;
  dedupe_note?: string;
  auto_close_note?: string;
  runbook_incident_type?: string | null;
};

export type WatcherRun = {
  id: string;
  watcher_id: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  created_tasks: number;
  updated_incidents: number;
  resolved_incidents: number;
  error: string;
  payload: Record<string, unknown>;
};

export type WatcherStatus = {
  enabled: boolean;
  interval_seconds: number;
  watcher_ids: string[];
  scheduled_watcher_ids: string[];
  min_severity: "warning" | "critical";
  ignore_patterns: string[];
  resolve_after_missing_runs: number;
  watchers: WatcherConfig[];
};

export type WatcherConfig = {
  id: string;
  label: string;
  enabled: boolean;
  interval_seconds: number;
  min_severity: "warning" | "critical";
  investigation_mode: "manual" | "auto_investigate";
  last_run: WatcherRun | null;
  last_error: string;
  next_run_at: string | null;
  runbook_incident_type: string | null;
};

export type WatcherRunResult = {
  ok: boolean;
  watchers: WatcherRun[];
  created_tasks: number;
  updated_incidents: number;
  resolved_incidents: number;
  error?: string;
};

export type AuditEntry = {
  id: string;
  created_at: string;
  actor: string;
  source: string;
  action: string;
  outcome: string;
  tool_id: string | null;
  task_id: string | null;
  metadata: Record<string, unknown> | null;
};

export type LunaUsageSummary = {
  calls: number;
  successful_calls: number;
  failed_calls: number;
  metered_calls: number;
  metering_coverage: number;
  priced_calls: number;
  pricing_coverage: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  attributed_cost_usd: number;
};

export type LunaMetrics = {
  period_days: number;
  since: string;
  pricing: {
    model: string;
    input_per_million: number;
    cached_input_per_million: number;
    output_per_million: number;
    source: string;
    billing_reconciled: boolean;
  };
  summary: LunaUsageSummary;
  components: Array<LunaUsageSummary & { component: string; label: string }>;
  ai_manager: {
    calls: number;
    local_calls: number;
    fallback_calls: number;
    local_rate: number;
    fallback_rate: number;
    schema_errors: number;
    timeouts: number;
    queue_wait: { average_ms: number | null; p95_ms: number | null; samples: number };
    inference_latency: { average_ms: number | null; p95_ms: number | null; samples: number };
    effective_models: Record<string, number>;
    prompt_versions: Record<string, number>;
    schema_versions: Record<string, number>;
    model_versions: Record<string, number>;
  };
  ai_delivery: {
    calls: number;
    successful_calls: number;
    failed_calls: number;
    fallback_calls: number;
    fallback_rate: number;
    latency: { average_ms: number | null; p95_ms: number | null; samples: number };
    providers: Record<string, number>;
    models: Record<string, number>;
    routes: Record<string, number>;
    recent: Array<{
      id: string;
      created_at: string;
      provider: string;
      model: string;
      status: string;
      fallback_used: boolean;
      fallback_reason: string;
      error_kind: string;
      route_mode: string;
      inference_latency_ms: number | null;
    }>;
  };
  router: {
    decisions: number;
    successful_calls: number;
    failed_calls: number;
    technical_success_rate: number;
    reviewed: number;
    review_coverage: number;
    accepted: number;
    corrected: number;
    rejected: number;
    reviewed_accuracy: number | null;
    average_confidence: number | null;
    owner_distribution: Record<string, number>;
  };
  auto_investigate: Record<string, number>;
  review_queue: Array<{
    task_id: string;
    task_title: string;
    created_at: string;
    action: string;
    category: string;
    priority: string;
    severity: string;
    suggested_owner: string;
    needs_operator: boolean;
    confidence: number | null;
    summary: string;
  }>;
};

export type ConversationStatus = {
  configured: boolean;
  model: string;
  max_turns: number;
  max_tool_calls: number;
  max_output_tokens: number;
  timeout_seconds: number;
};

export type TaskRouterCorrections = {
  action?: string;
  category?: string;
  priority?: string;
  severity?: string;
  suggested_owner?: string;
  needs_operator?: boolean;
};

export type TopologyNode = {
  id: string;
  label: string;
  kind: string;
  layer: "wan" | "edge" | "compute" | "services" | string;
  provider_id: string;
  observation_id: string;
  availability_monitor: string;
  availability_observation_id: string;
  inherit_provider_status: boolean;
  incident_watcher_ids: string[];
  role: string;
  group: string;
  parent_id: string;
  status: ProviderStatus | string;
  status_detail: string;
  dynamic: boolean;
  vmid: number | null;
  runtime_node: string;
  guest_type: string;
};

export type CapabilityObservation = {
  id: string;
  provider_id: string;
  capability_id: string;
  label: string;
  tool_id: string;
  status: ProviderStatus;
  detail: string;
  checked_at: string;
  error_code: string;
  summary: Record<string, string | number | boolean | null>;
};

export type TopologyEdge = {
  id: string;
  source: string;
  target: string;
  kind: string;
  label: string;
  affects_rca: boolean;
  availability_group: string;
  dynamic: boolean;
};

export type TopologyAvailabilityGroup = {
  id: string;
  label: string;
  mode: "all" | "any" | string;
};

export type TopologyGraph = {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
  availability_groups: TopologyAvailabilityGroup[];
  layer_order: string[];
  source_status: "declared" | "live" | string;
  warnings: string[];
  observed_at: string;
};

export type TopologyFreshnessSource = {
  status: "fresh" | "stale" | "error" | string;
  observed_at?: string | null;
  loaded_at?: string | null;
  version?: string;
  warning?: string;
  error?: string;
};

export type TopologySnapshot = {
  graph: TopologyGraph;
  providers: Provider[];
  observations: CapabilityObservation[];
  incidents: Incident[];
  freshness: Record<string, TopologyFreshnessSource>;
  generated_at: string;
  cache_ttl_seconds: number;
};

export type McpClient = {
  id: string;
  agent_id: "claude" | "codex" | string;
  client_label: string;
  host_fingerprint: string;
  token_hint: string;
  created_at: string;
  approved_at: string | null;
  last_seen_at: string | null;
  revoked_at: string | null;
  revoked_reason: string;
  created_by: string;
};

export type McpRotateResult = {
  ok: true;
  token: string;
  client: McpClient;
};

export type McpPairingStart = {
  request_id: string;
  pairing_secret: string;
  status: string;
  delivery_status: string;
  expires_at: string;
};

export type McpPairingRequest = {
  id: string;
  agent_id: "claude" | "codex" | "cline" | string;
  client_label: string;
  host_fingerprint: string;
  status: "pending" | "approved" | "denied" | "expired" | "consumed" | string;
  created_at: string;
  expires_at: string;
  approved_at: string | null;
  denied_at: string | null;
  consumed_at: string | null;
  decided_by: string;
  delivery_status: string;
};

export type McpPairingConsumeResult =
  | {
      ok: true;
      token: string;
      client: McpClient;
    }
  | {
      ok: false;
      error: { code: string; message: string };
    };

export type Runbook = {
  incident_type: string;
  label: string;
  steps: Array<{ tool_id: string; evidence: string }>;
  escalation_note: string;
};

export type OpsError = {
  id: string;
  created_at: string;
  source: string;
  kind: string;
  title: string;
  detail: string;
  tool_id: string;
  task_id: string;
};

export type OperationalHealth = {
  database: {
    dialect: string;
    size_bytes: number | null;
    size_pretty: string;
    connections: number | null;
  };
  retention: {
    enabled: boolean;
    interval_seconds: number;
    days: Record<string, number>;
    batch_size: number;
  };
  workers: {
    watchers_enabled: boolean;
    notification_outbox_enabled: boolean;
    notification_counts: Record<string, number>;
    last_watcher_run: WatcherRun | null;
  };
  provider_errors: Array<{
    provider_id: string;
    display_name: string;
    status: string;
    message: string;
    at: string | null;
  }>;
};
