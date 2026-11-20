/** Domain contracts shared by the storage and HTTP layers. */

export type BusinessSystemStatus = "active" | "archived";

export interface BusinessSystem {
  id: string;
  name: string;
  description?: string;
  status: BusinessSystemStatus;
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export type BusinessUnitKind = "operator" | "machine";
export type BusinessUnitStatus = "pending" | "ready" | "running" | "blocked" | "archived";

export interface BusinessUnit {
  id: string;
  businessSystemId: string;
  projectId?: string;
  kind: BusinessUnitKind;
  name: string;
  harnessRuntime?: string;
  model?: string;
  machineRef?: string;
  status: BusinessUnitStatus;
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export type BusinessRunStatus = "queued" | "running" | "completed" | "failed" | "cancelled";
export type UnitRunStatus = "queued" | "running" | "waiting_resources" | "completed" | "failed" | "blocked" | "cancelled";

export interface BusinessRun {
  id: string;
  businessSystemId: string;
  status: BusinessRunStatus;
  trigger: string;
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export interface UnitRun {
  id: string;
  runId: string;
  unitId: string;
  status: UnitRunStatus;
  sessionId?: string;
  inputs: string[];
  outputs: string[];
  error?: string;
  createdAt: string;
  updatedAt: string;
}

export type BusinessConversionStatus = "planned" | "running" | "completed" | "failed";

export interface BusinessConversion {
  id: string;
  businessSystemId: string;
  kind: string;
  summary: string;
  status: BusinessConversionStatus;
  inputs: string[];
  outputs: string[];
  producer?: { operator?: string; machine?: string; harness?: string };
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export type ProjectSourceKind = "repo_url" | "uploaded_file" | "local_folder";

export interface ProjectSource {
  id: string;
  projectId: string;
  kind: ProjectSourceKind;
  repoUrl?: string;
  fileRef?: string;
  localFolder?: string;
  metadata: Record<string, unknown>;
  createdAt: string;
}

export interface Artifact {
  id: string;
  projectId: string;
  sourceId?: string;
  /** A human-readable business record that can travel between Harnesses. */
  recordType?: "artifact" | "material";
  kind: string;
  ref: string;
  title?: string;
  description?: string;
  metadata: Record<string, unknown>;
  summary?: string;
  purpose?: string;
  status?: string;
  producer?: { operator?: string; machine?: string; harness?: string };
  files?: Array<{ ref: string; path?: string; mediaType?: string; digest?: string }>;
  inputs?: string[];
  outputs?: string[];
  lineage?: string[];
  createdAt: string;
}

export type Material = Artifact & { recordType: "material" };

export type SecretRequestStatus = "requested" | "available" | "denied" | "revoked" | "expired";

export interface SecretRequest {
  id: string;
  projectId: string;
  name: string;
  vaultRef: string;
  status: SecretRequestStatus;
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export interface Event<T = unknown> {
  id: string;
  seq: number;
  type: string;
  data: T;
  createdAt: string;
}

export interface Project {
  id: string;
  businessSystemId?: string;
  name: string;
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
  sources: ProjectSource[];
  secretRequests: SecretRequest[];
}

export interface TalkMessage {
  id: string;
  role: string;
  body: string;
  metadata: Record<string, unknown>;
  createdAt: string;
}

export interface ForemanEnvelope {
  id: string;
  messageId: string;
  text: string;
  status: "queued" | "claimed" | "done" | "failed" | "cancelled";
  createdAt: string;
  claimedAt?: string;
}

export type ForemanTurnState = Pick<ForemanEnvelope, "id" | "messageId" | "status"> & { cancellable: boolean };

export interface HarnessProfile {
  id: string;
  runtime: string;
  role: "operator" | "foreman" | string;
  baseUrl: string;
  model: string;
  mode: string;
  sessionTimeoutSeconds: number;
  image?: string;
  network?: string;
  secretRef?: string;
  verification?: { state: string; reason?: string; checkedAt: string };
  updatedAt: string;
}

