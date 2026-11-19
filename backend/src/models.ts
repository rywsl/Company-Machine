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
  kind: string;
  ref: string;
  metadata: Record<string, unknown>;
  summary?: string;
  purpose?: string;
  files?: Array<{ ref: string; path?: string; mediaType?: string; digest?: string }>;
  inputs?: string[];
  outputs?: string[];
  lineage?: string[];
  createdAt: string;
}

export type SecretRequestStatus = "requested" | "available" | "denied" | "revoked";

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
  status: "queued" | "claimed" | "done";
  createdAt: string;
  claimedAt?: string;
}

export interface HarnessProfile {
  id: string;
  runtime: string;
  role: "operator" | "foreman" | string;
  baseUrl: string;
  model: string;
  mode: string;
  image?: string;
  network?: string;
  secretRef?: string;
  verification?: { state: string; reason?: string; checkedAt: string };
  updatedAt: string;
}

