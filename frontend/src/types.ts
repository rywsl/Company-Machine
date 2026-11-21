export type Page = "dashboard" | "compose" | "assets" | "logs" | "settings";
export type Scope = "factory" | "workarea";

export interface OperatorBindingView {
  businessSystemId: string;
  harnessRuntime?: string;
  model?: string;
  effectiveModel?: string;
  profileId?: string;
  reason?: string;
}

export interface GraphNode {
  id: string;
  type: "workarea" | "operator" | "machine";
  group: string;
  label: string;
  position: { x: number; y: number };
  status?: string;
  metadata?: Record<string, unknown>;
  operatorBinding?: OperatorBindingView;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  kind: "material" | "condition" | "default" | "aggregate" | string;
  condition?: string | null;
  status?: string;
  metadata?: Record<string, unknown>;
}

export interface Graph {
  scope: Scope;
  id: string;
  title: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  metadata?: Record<string, unknown>;
}

export interface EventRecord {
  id: string;
  kind: string;
  at?: string;
  run_id?: string;
  job_id?: string;
  data?: Record<string, unknown>;
  [key: string]: unknown;
}

export type BusinessRunStatus = "queued" | "running" | "completed" | "failed" | "cancelled" | string;
export type UnitRunStatus = "queued" | "running" | "waiting_resources" | "completed" | "failed" | "blocked" | string;

export interface UnitRun {
  id: string;
  runId: string;
  unitId: string;
  status: UnitRunStatus;
  sessionId?: string;
  inputs: string[];
  outputs: string[];
  error?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface BusinessRun {
  id: string;
  businessSystemId: string;
  status: BusinessRunStatus;
  trigger: string;
  metadata?: Record<string, unknown>;
  createdAt?: string;
  updatedAt?: string;
  units: UnitRun[];
}

export interface StreamEvent {
  id?: string;
  type: string;
  data?: Record<string, unknown>;
  at?: string;
}

export interface AssetRecord {
  id: string;
  type: string;
  record_type?: "artifact" | "material" | string;
  ref?: string;
  title?: string;
  description?: string;
  summary?: string;
  purpose?: string;
  status?: string;
  validation_epoch?: string;
  files?: Array<{ ref: string; path?: string; mediaType?: string; digest?: string; download?: string }>;
  inputs?: string[];
  outputs?: string[];
  lineage?: string[];
  sealed: boolean;
  expired: boolean;
  created_at?: string;
  meta?: Record<string, unknown>;
  source?: { run?: string; node?: string } | null;
}

export interface AssetPageData {
  items: AssetRecord[];
  total: number;
  olderCursor?: string | null;
}

export interface HarnessRecord {
  id: string;
  name: string;
  version?: string | number | null;
  kind?: string;
  source?: string;
  roles?: string[];
  available?: boolean;
  host_available?: boolean;
  container_configured?: boolean;
  capabilities?: Record<string, boolean>;
  configuration?: HarnessProfile | null;
}

export interface DependencyStatus {
  state: string;
  provider?: string;
  version?: string | null;
  message?: string;
  id?: string;
  name?: string;
  integrity?: Record<string, boolean>;
}

export interface StorageProviderStatus {
  kind?: string;
  mode?: string;
  available?: boolean;
  code?: string;
  reason?: string;
}

export interface HarnessProfile {
  id: string;
  runtime: string;
  role: "operator" | "foreman" | string;
  base_url: string;
  model: string;
  thinking_effort?: string;
  context?: number;
  api_key_configured: boolean;
  image?: string;
  network?: string;
  session_timeout_s?: number;
  session_budget_usd?: number;
  daily_budget_usd?: number;
  max_turns?: number;
  max_attempts?: number;
  backoff_s?: number;
  mode?: string;
  execution_mode?: "trusted_container" | "native_host" | string;
  runtime_state?: string;
  runtime_reason?: string;
  verification?: { state?: string; reason?: string; checked_at?: string | null; request_id?: string };
  updated_at?: string;
}

export interface SecretRecord {
  id: string;
  key: string;
  masked_value: string;
  configured: boolean;
  usage_scope?: string;
  bound_objects?: string[];
  last_used?: string | null;
  expires_at?: string | null;
  expired?: boolean;
  status?: string;
  kind?: string;
  source_tool?: string;
  request?: { id?: string; project_id?: string; project_name?: string; status?: string; purpose?: string; scope?: string } | null;
  agent_generated_checks?: string[];
}

export interface Bootstrap {
  version: string;
  eventCursor?: number;
  pendingForemanReplies?: Record<string, string>;
  foremanTurns?: ForemanTurn[];
  connection: { state: string; message?: string };
  status: {
    at?: string;
    runs?: Record<string, number>;
    alerts?: unknown[];
    jobs?: Record<string, unknown>;
    [key: string]: unknown;
  };
  graph: { factory: Graph; workarea?: Graph | null };
  assets: AssetRecord[];
  assetTotal?: number;
  assetOlderCursor?: string | null;
  logs: EventRecord[];
  talk: TalkMessage[];
  harnesses: HarnessRecord[];
  harness_profiles?: HarnessProfile[];
  dependencies?: { harnesses: DependencyStatus[]; podman?: DependencyStatus; kiri?: DependencyStatus; docker?: DependencyStatus; storage?: { mode?: string; state?: StorageProviderStatus; blob?: StorageProviderStatus; secret?: StorageProviderStatus; kms?: StorageProviderStatus } };
  foreman: {
    configured?: boolean;
    setup_required?: boolean;
    runtime?: string | null;
    model?: string | null;
    mode?: string | null;
    runtime_state?: string;
    runtime_reason?: string;
    runtime_provider?: string;
    profile?: HarnessProfile | null;
  };
  secrets: SecretRecord[];
  constitution: {
    available?: boolean;
    status?: string;
    version?: string | null;
    recent_change?: Record<string, unknown> | null;
  };
}

export interface TalkMessage {
  id?: string;
  role?: string;
  body?: string;
  state?: string;
  at?: string;
  createdAt?: string;
  metadata?: Record<string, unknown>;
  card?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ForemanTurn {
  id: string;
  messageId: string;
  status: "queued" | "claimed" | "done" | "failed" | "cancelled" | "blocked" | "waiting_foreman_runtime";
  cancellable: boolean;
  queuedAt?: string;
  startedAt?: string;
  lastEventAt?: string;
  completedAt?: string;
  elapsedMs?: number;
  error?: string;
  businessStatus?: string;
  businessReason?: string;
}

export interface LayoutGraph {
  nodes: Record<string, { x: number; y: number }>;
  viewport: { x: number; y: number; zoom: number };
}

export interface LayoutState {
  sidebarDock?: "left" | "right";
  maoTop?: number;
  graphs?: Record<string, LayoutGraph>;
}
