import { Database } from "bun:sqlite";
import { randomUUID } from "node:crypto";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import type {
  Artifact,
  BusinessSystem,
  BusinessConversion,
  BusinessRun,
  BusinessUnit,
  Event,
  ForemanEnvelope,
  HarnessProfile,
  Project,
  ProjectSource,
  SecretRequest,
  TalkMessage,
  UnitRun,
} from "./models";

const now = () => new Date().toISOString();
const json = (value: unknown) => JSON.stringify(value ?? {});
const parseJson = (value: unknown): Record<string, unknown> => {
  if (typeof value !== "string" || value.length === 0) return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
};
const parseList = (value: unknown): string[] => {
  if (typeof value !== "string") return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
};

export interface IntakeInput {
  name?: string;
  project_name?: string;
  business_system_id?: string;
  repo_url?: string;
  uploaded_file_refs?: string[];
  files?: Array<{ name?: string; ref?: string; digest?: string }>;
  local_folder?: string;
  metadata?: Record<string, unknown>;
  secret_requests?: Array<{ name: string; vault_ref: string; status?: SecretRequest["status"]; metadata?: Record<string, unknown> }>;
}

export class SqliteStore {
  readonly db: Database;
  readonly legacy?: Database;
  private readonly bridgeLegacy: boolean;

  constructor(dbPath = process.env.BUSINESS_DB_PATH ?? process.env.FACTORY_DB_PATH ?? ".factory/factory.db", legacyPath = process.env.FACTORY_DB_PATH ?? ".factory/factory.db") {
    mkdirSync(dirname(dbPath), { recursive: true });
    this.db = new Database(dbPath, { create: true });
    this.db.exec("PRAGMA journal_mode = WAL");
    this.init();
    this.bridgeLegacy = dbPath === legacyPath && /factory\.db$/i.test(dbPath.replaceAll("\\", "/"));
    if (dbPath === legacyPath) return;
    try {
      this.legacy = new Database(`file:${legacyPath}?mode=ro&immutable=1`, { readonly: true });
    } catch {
      // A missing legacy database is normal for a new installation.
    }
  }

  init() {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS business_systems (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, status TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY, business_system_id TEXT, name TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS project_sources (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
        repo_url TEXT, file_ref TEXT, local_folder TEXT, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS artifacts (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_id TEXT, kind TEXT NOT NULL,
        ref TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS secret_requests (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, vault_ref TEXT NOT NULL,
        status TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS harness_profiles (
        id TEXT PRIMARY KEY, runtime TEXT NOT NULL, role TEXT NOT NULL, base_url TEXT NOT NULL,
        model TEXT NOT NULL, mode TEXT NOT NULL, image TEXT, network TEXT, secret_ref TEXT,
        verification TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS business_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, type TEXT NOT NULL,
        data TEXT NOT NULL, created_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS business_talk_messages (
        id TEXT PRIMARY KEY, role TEXT NOT NULL, body TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS foreman_outbox (
        id TEXT PRIMARY KEY, message_id TEXT NOT NULL, text TEXT NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL, claimed_at TEXT
      );
      CREATE TABLE IF NOT EXISTS business_units (
        id TEXT PRIMARY KEY, business_system_id TEXT NOT NULL, project_id TEXT,
        kind TEXT NOT NULL, name TEXT NOT NULL, harness_runtime TEXT, model TEXT,
        machine_ref TEXT, status TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS business_runs (
        id TEXT PRIMARY KEY, business_system_id TEXT NOT NULL, status TEXT NOT NULL,
        trigger TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS unit_runs (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, unit_id TEXT NOT NULL,
        status TEXT NOT NULL, session_id TEXT, inputs TEXT NOT NULL DEFAULT '[]',
        outputs TEXT NOT NULL DEFAULT '[]', error TEXT, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, UNIQUE(run_id, unit_id)
      );
      CREATE TABLE IF NOT EXISTS business_conversions (
        id TEXT PRIMARY KEY, business_system_id TEXT NOT NULL, kind TEXT NOT NULL,
        summary TEXT NOT NULL, status TEXT NOT NULL, inputs TEXT NOT NULL DEFAULT '[]',
        outputs TEXT NOT NULL DEFAULT '[]', producer TEXT, metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS business_talk_messages_created ON business_talk_messages(created_at, id);
      CREATE INDEX IF NOT EXISTS secret_requests_project ON secret_requests(project_id, created_at);
      CREATE INDEX IF NOT EXISTS foreman_outbox_status ON foreman_outbox(status, created_at, id);
      CREATE INDEX IF NOT EXISTS business_units_system ON business_units(business_system_id, kind, created_at, id);
      CREATE INDEX IF NOT EXISTS business_runs_system ON business_runs(business_system_id, created_at, id);
      CREATE INDEX IF NOT EXISTS unit_runs_run ON unit_runs(run_id, status, created_at, id);
      CREATE INDEX IF NOT EXISTS business_conversions_system ON business_conversions(business_system_id, created_at, id);
    `);
  }

  listBusinessSystems(): BusinessSystem[] {
    return this.db.query("SELECT * FROM business_systems ORDER BY created_at").all().map((row: any) => ({
      id: row.id, name: row.name, description: row.description ?? undefined, status: row.status,
      metadata: parseJson(row.metadata), createdAt: row.created_at, updatedAt: row.updated_at,
    }));
  }

  listProjects(): Project[] {
    return this.db.query("SELECT * FROM projects ORDER BY created_at, id").all().map((row: any) => this.projectFromRow(row));
  }

  getProject(id: string): Project | undefined {
    const row = this.db.query("SELECT * FROM projects WHERE id = ?").get(id) as any;
    return row ? this.projectFromRow(row) : undefined;
  }

  createBusinessSystem(input: Partial<BusinessSystem> & { name: string }): BusinessSystem {
    const timestamp = now();
    const value: BusinessSystem = { id: input.id ?? randomUUID(), name: input.name, description: input.description,
      status: input.status ?? "active", metadata: input.metadata ?? {}, createdAt: timestamp, updatedAt: timestamp };
    this.db.query("INSERT INTO business_systems (id,name,description,status,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?)")
      .run(value.id, value.name, value.description ?? null, value.status, json(value.metadata), timestamp, timestamp);
    return value;
  }

  updateBusinessSystemMetadata(id: string, metadata: Record<string, unknown>): BusinessSystem | undefined {
    const current = this.db.query("SELECT * FROM business_systems WHERE id = ?").get(id) as any;
    if (!current) return undefined;
    const merged = { ...parseJson(current.metadata), ...metadata };
    const updatedAt = now();
    this.db.query("UPDATE business_systems SET metadata = ?, updated_at = ? WHERE id = ?").run(json(merged), updatedAt, id);
    return { id: current.id, name: current.name, description: current.description ?? undefined, status: current.status, metadata: merged, createdAt: current.created_at, updatedAt };
  }

  listHarnessProfiles(): HarnessProfile[] {
    return this.db.query("SELECT * FROM harness_profiles ORDER BY role, runtime").all().map((row: any) => ({
      id: row.id,
      runtime: row.runtime,
      role: row.role,
      baseUrl: row.base_url,
      model: row.model,
      mode: row.mode,
      image: row.image ?? undefined,
      network: row.network ?? undefined,
      secretRef: row.secret_ref ?? undefined,
      verification: parseJson(row.verification) as HarnessProfile["verification"],
      updatedAt: row.updated_at,
    }));
  }

  saveHarnessProfile(input: Omit<HarnessProfile, "updatedAt" | "id"> & { id?: string }): HarnessProfile {
    const updatedAt = now();
    const value: HarnessProfile = { ...input, id: input.id ?? `${input.role}:${input.runtime}`, updatedAt };
    this.db.query("INSERT INTO harness_profiles (id,runtime,role,base_url,model,mode,image,network,secret_ref,verification,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET runtime=excluded.runtime, role=excluded.role, base_url=excluded.base_url, model=excluded.model, mode=excluded.mode, image=excluded.image, network=excluded.network, secret_ref=excluded.secret_ref, verification=excluded.verification, updated_at=excluded.updated_at")
      .run(value.id, value.runtime, value.role, value.baseUrl, value.model, value.mode, value.image ?? null, value.network ?? null, value.secretRef ?? null, json(value.verification ?? {}), updatedAt);
    return value;
  }

  markHarnessVerification(id: string, state: string, reason?: string): HarnessProfile | undefined {
    const current = this.listHarnessProfiles().find((profile) => profile.id === id);
    if (!current) return undefined;
    return this.saveHarnessProfile({ ...current, verification: { state, reason, checkedAt: now() } });
  }

  createIntake(input: IntakeInput): Project {
    const timestamp = now();
    const project: Project = { id: randomUUID(), businessSystemId: input.business_system_id,
      name: input.name ?? input.project_name ?? "Untitled project", metadata: input.metadata ?? {},
      createdAt: timestamp, updatedAt: timestamp, sources: [], secretRequests: [] };
    this.db.query("INSERT INTO projects (id,business_system_id,name,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?)")
      .run(project.id, project.businessSystemId ?? null, project.name, json(project.metadata), timestamp, timestamp);
    const addSource = (kind: ProjectSource["kind"], fields: { repoUrl?: string; fileRef?: string; localFolder?: string }) => {
      const source: ProjectSource = { id: randomUUID(), projectId: project.id, kind, ...fields, metadata: {}, createdAt: timestamp };
      this.db.query("INSERT INTO project_sources (id,project_id,kind,repo_url,file_ref,local_folder,metadata,created_at) VALUES (?,?,?,?,?,?,?,?)")
        .run(source.id, source.projectId, source.kind, source.repoUrl ?? null, source.fileRef ?? null, source.localFolder ?? null, "{}", timestamp);
      project.sources.push(source);
    };
    if (input.repo_url) addSource("repo_url", { repoUrl: input.repo_url });
    for (const fileRef of input.uploaded_file_refs ?? []) addSource("uploaded_file", { fileRef });
    for (const file of input.files ?? []) {
      const fileRef = file.ref ?? file.name;
      if (fileRef) addSource("uploaded_file", { fileRef });
    }
    if (input.local_folder) addSource("local_folder", { localFolder: input.local_folder });
    for (const request of input.secret_requests ?? []) {
      const secret: SecretRequest = { id: randomUUID(), projectId: project.id, name: request.name, vaultRef: request.vault_ref,
        status: request.status ?? "requested", metadata: request.metadata ?? {}, createdAt: timestamp, updatedAt: timestamp };
      this.db.query("INSERT INTO secret_requests (id,project_id,name,vault_ref,status,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)")
        .run(secret.id, secret.projectId, secret.name, secret.vaultRef, secret.status, json(secret.metadata), timestamp, timestamp);
      project.secretRequests.push(secret);
    }
    if (project.businessSystemId) {
      const operatorRuntime = typeof project.metadata.operator_runtime === "string" ? project.metadata.operator_runtime : "codex";
      const operatorModel = typeof project.metadata.operator_model === "string" ? project.metadata.operator_model : "default";
      this.db.query("INSERT OR IGNORE INTO business_units (id,business_system_id,project_id,kind,name,harness_runtime,model,machine_ref,status,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
        .run(`${project.id}:operator`, project.businessSystemId, project.id, "operator", `${project.name} Operator`, operatorRuntime, operatorModel, null, "pending", json({ source: "intake" }), timestamp, timestamp);
      const source = project.sources[0];
      const machineRef = source?.repoUrl ?? source?.fileRef ?? source?.kind ?? null;
      this.db.query("INSERT OR IGNORE INTO business_units (id,business_system_id,project_id,kind,name,harness_runtime,model,machine_ref,status,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
        .run(`${project.id}:machine`, project.businessSystemId, project.id, "machine", project.name, null, null, machineRef, "ready", json({ source_count: project.sources.length, source_kind: source?.kind }), timestamp, timestamp);
    }
    return project;
  }

  listBusinessUnits(businessSystemId: string): BusinessUnit[] {
    return this.db.query("SELECT * FROM business_units WHERE business_system_id = ? ORDER BY kind, created_at, id").all(businessSystemId).map((row: any) => this.businessUnitFromRow(row));
  }

  getBusinessUnit(id: string): BusinessUnit | undefined {
    const row = this.db.query("SELECT * FROM business_units WHERE id = ?").get(id) as any;
    return row ? this.businessUnitFromRow(row) : undefined;
  }

  createBusinessRun(businessSystemId: string, trigger = "manual", metadata: Record<string, unknown> = {}, inputs: string[] = []): BusinessRun | undefined {
    const units = this.listBusinessUnits(businessSystemId).filter((unit) => unit.kind === "operator");
    if (!this.db.query("SELECT 1 FROM business_systems WHERE id = ?").get(businessSystemId)) return undefined;
    const timestamp = now();
    const value: BusinessRun = { id: randomUUID(), businessSystemId, status: units.length ? "queued" : "failed", trigger, metadata, createdAt: timestamp, updatedAt: timestamp };
    this.db.query("INSERT INTO business_runs (id,business_system_id,status,trigger,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?)")
      .run(value.id, value.businessSystemId, value.status, value.trigger, json(value.metadata), timestamp, timestamp);
    for (const unit of units) {
      this.db.query("INSERT INTO unit_runs (id,run_id,unit_id,status,session_id,inputs,outputs,error,created_at,updated_at) VALUES (?,?,?,?,NULL,?,'[]',NULL,?,?)")
        .run(randomUUID(), value.id, unit.id, value.status === "failed" ? "blocked" : "queued", json(inputs), timestamp, timestamp);
    }
    return value;
  }

  listBusinessRuns(businessSystemId: string): BusinessRun[] {
    return this.db.query("SELECT * FROM business_runs WHERE business_system_id = ? ORDER BY created_at, id").all(businessSystemId).map((row: any) => this.businessRunFromRow(row));
  }

  getBusinessRun(id: string): BusinessRun | undefined {
    const row = this.db.query("SELECT * FROM business_runs WHERE id = ?").get(id) as any;
    return row ? this.businessRunFromRow(row) : undefined;
  }

  listUnitRuns(runId: string): UnitRun[] {
    return this.db.query("SELECT * FROM unit_runs WHERE run_id = ? ORDER BY created_at, id").all(runId).map((row: any) => this.unitRunFromRow(row));
  }

  listReadyUnitRuns(runId: string): UnitRun[] {
    const run = this.getBusinessRun(runId);
    if (!run) return [];
    const unitRuns = this.listUnitRuns(runId);
    const statuses = new Map(unitRuns.map((item) => [item.unitId, item.status]));
    const outputs = new Map(unitRuns.map((item) => [item.unitId, item.outputs]));
    const predecessors = new Map<string, string[]>();
    for (const edge of this.businessGraph(run.businessSystemId).edges) {
      predecessors.set(edge.target, [...(predecessors.get(edge.target) ?? []), edge.source]);
    }
    const ready: UnitRun[] = [];
    for (const item of unitRuns.filter((candidate) => candidate.status === "queued")) {
      const required = (predecessors.get(item.unitId) ?? []).filter((unitId) => statuses.has(unitId));
      if (required.some((unitId) => statuses.get(unitId) !== "completed")) continue;
      const inherited = required.flatMap((unitId) => outputs.get(unitId) ?? []);
      const inputs = [...new Set([...item.inputs, ...inherited])];
      if (inputs.length !== item.inputs.length || inputs.some((value, index) => value !== item.inputs[index])) {
        this.db.query("UPDATE unit_runs SET inputs=?, updated_at=? WHERE id=? AND status='queued'").run(json(inputs), now(), item.id);
        item.inputs = inputs;
      }
      ready.push(item);
    }
    return ready;
  }

  claimUnitRun(runId: string, unitId: string, sessionId: string): UnitRun | undefined {
    const timestamp = now();
    this.db.query("UPDATE unit_runs SET status='running', session_id=?, updated_at=? WHERE run_id=? AND unit_id=? AND status='queued'").run(sessionId, timestamp, runId, unitId);
    this.db.query("UPDATE business_units SET status='running', updated_at=? WHERE id=?").run(timestamp, unitId);
    this.db.query("UPDATE business_runs SET status='running', updated_at=? WHERE id=? AND status='queued'").run(timestamp, runId);
    const row = this.db.query("SELECT * FROM unit_runs WHERE run_id=? AND unit_id=?").get(runId, unitId) as any;
    return row ? this.unitRunFromRow(row) : undefined;
  }

  completeUnitRun(id: string, status: UnitRun["status"], outputs: string[] = [], error?: string): UnitRun | undefined {
    const timestamp = now();
    this.db.query("UPDATE unit_runs SET status=?, outputs=?, error=?, updated_at=? WHERE id=?").run(status, json(outputs), error ?? null, timestamp, id);
    const row = this.db.query("SELECT * FROM unit_runs WHERE id=?").get(id) as any;
    if (!row) return undefined;
    const statuses = this.db.query("SELECT status FROM unit_runs WHERE run_id=?").all(row.run_id) as Array<{ status: UnitRun["status"] }>;
    const runStatus: BusinessRun["status"] = statuses.some((item) => item.status === "failed" || item.status === "blocked") ? "failed" : statuses.every((item) => item.status === "completed") ? "completed" : "running";
    this.db.query("UPDATE business_runs SET status=?, updated_at=? WHERE id=?").run(runStatus, timestamp, row.run_id);
    this.db.query("UPDATE business_units SET status=?, updated_at=? WHERE id=?").run(status === "completed" ? "ready" : status, timestamp, row.unit_id);
    return this.unitRunFromRow(row);
  }

  private businessGraph(businessSystemId: string): { edges: Array<{ source: string; target: string }> } {
    const row = this.db.query("SELECT metadata FROM business_systems WHERE id=?").get(businessSystemId) as { metadata?: string } | undefined;
    const metadata = parseJson(row?.metadata);
    const graph = metadata.graph && typeof metadata.graph === "object" ? metadata.graph as { edges?: unknown } : {};
    const edges = Array.isArray(graph.edges) ? graph.edges.filter((edge): edge is { source: string; target: string } => Boolean(edge && typeof edge === "object" && typeof (edge as any).source === "string" && typeof (edge as any).target === "string")) : [];
    return { edges };
  }

  private businessUnitFromRow(row: any): BusinessUnit {
    return { id: row.id, businessSystemId: row.business_system_id, projectId: row.project_id ?? undefined, kind: row.kind, name: row.name, harnessRuntime: row.harness_runtime ?? undefined, model: row.model ?? undefined, machineRef: row.machine_ref ?? undefined, status: row.status, metadata: parseJson(row.metadata), createdAt: row.created_at, updatedAt: row.updated_at };
  }

  private businessRunFromRow(row: any): BusinessRun {
    return { id: row.id, businessSystemId: row.business_system_id, status: row.status, trigger: row.trigger, metadata: parseJson(row.metadata), createdAt: row.created_at, updatedAt: row.updated_at };
  }

  private unitRunFromRow(row: any): UnitRun {
    return { id: row.id, runId: row.run_id, unitId: row.unit_id, status: row.status, sessionId: row.session_id ?? undefined, inputs: parseList(row.inputs), outputs: parseList(row.outputs), error: row.error ?? undefined, createdAt: row.created_at, updatedAt: row.updated_at };
  }

  private projectFromRow(row: any): Project {
    const sources = this.db.query("SELECT * FROM project_sources WHERE project_id = ? ORDER BY created_at, id").all(row.id).map((item: any) => ({
      id: item.id,
      projectId: item.project_id,
      kind: item.kind,
      repoUrl: item.repo_url ?? undefined,
      fileRef: item.file_ref ?? undefined,
      localFolder: item.local_folder ?? undefined,
      metadata: parseJson(item.metadata),
      createdAt: item.created_at,
    }));
    const secretRequests = this.db.query("SELECT * FROM secret_requests WHERE project_id = ? ORDER BY created_at, id").all(row.id).map((item: any) => ({
      id: item.id,
      projectId: item.project_id,
      name: item.name,
      vaultRef: item.vault_ref,
      status: item.status,
      metadata: parseJson(item.metadata),
      createdAt: item.created_at,
      updatedAt: item.updated_at,
    }));
    return {
      id: row.id,
      businessSystemId: row.business_system_id ?? undefined,
      name: row.name,
      metadata: parseJson(row.metadata),
      createdAt: row.created_at,
      updatedAt: row.updated_at,
      sources,
      secretRequests,
    };
  }

  addArtifact(input: Omit<Artifact, "id" | "createdAt">): Artifact {
    const metadata: Record<string, unknown> = {
      ...(input.metadata ?? {}),
      record_type: input.recordType ?? (input.metadata?.record_type === "material" ? "material" : "artifact"),
      title: input.title ?? input.metadata?.title,
      description: input.description ?? input.metadata?.description,
      status: input.status ?? input.metadata?.status,
      producer: input.producer ?? input.metadata?.producer,
    };
    const value: Artifact = {
      ...input,
      id: randomUUID(),
      createdAt: now(),
      recordType: metadata.record_type === "material" ? "material" : "artifact",
      title: typeof metadata.title === "string" ? metadata.title : undefined,
      description: typeof metadata.description === "string" ? metadata.description : undefined,
      summary: input.summary ?? (typeof metadata.summary === "string" ? metadata.summary : undefined),
      purpose: input.purpose ?? (typeof metadata.purpose === "string" ? metadata.purpose : undefined),
      status: typeof metadata.status === "string" ? metadata.status : undefined,
      producer: metadata.producer && typeof metadata.producer === "object" ? metadata.producer as Artifact["producer"] : undefined,
      files: input.files ?? (Array.isArray(metadata.files) ? metadata.files as Artifact["files"] : undefined),
      inputs: input.inputs ?? (Array.isArray(metadata.inputs) ? metadata.inputs as string[] : undefined),
      outputs: input.outputs ?? (Array.isArray(metadata.outputs) ? metadata.outputs as string[] : undefined),
      lineage: input.lineage ?? (Array.isArray(metadata.lineage) ? metadata.lineage as string[] : undefined),
    };
    this.db.query("INSERT INTO artifacts (id,project_id,source_id,kind,ref,metadata,created_at) VALUES (?,?,?,?,?,?,?)")
      .run(value.id, value.projectId, value.sourceId ?? null, value.kind, value.ref, json(value.metadata), value.createdAt);
    return value;
  }

  listArtifacts(projectId: string): Artifact[] {
    return this.db.query("SELECT * FROM artifacts WHERE project_id = ? ORDER BY created_at, id").all(projectId).map((row: any) => this.artifactFromRow(row));
  }

  listBusinessSystemArtifacts(businessSystemId: string): Artifact[] {
    const rows = this.db.query("SELECT artifacts.* FROM artifacts JOIN projects ON projects.id = artifacts.project_id WHERE projects.business_system_id = ? ORDER BY artifacts.created_at, artifacts.id").all(businessSystemId);
    return rows.map((row: any) => this.artifactFromRow(row));
  }

  createBusinessConversion(input: Omit<BusinessConversion, "id" | "createdAt" | "updatedAt">): BusinessConversion {
    const timestamp = now();
    const value: BusinessConversion = { ...input, id: randomUUID(), createdAt: timestamp, updatedAt: timestamp };
    this.db.query("INSERT INTO business_conversions (id,business_system_id,kind,summary,status,inputs,outputs,producer,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
      .run(value.id, value.businessSystemId, value.kind, value.summary, value.status, json(value.inputs), json(value.outputs), value.producer ? json(value.producer) : null, json(value.metadata), timestamp, timestamp);
    return value;
  }

  listBusinessConversions(businessSystemId: string): BusinessConversion[] {
    return this.db.query("SELECT * FROM business_conversions WHERE business_system_id=? ORDER BY created_at,id").all(businessSystemId).map((row: any) => ({
      id: row.id,
      businessSystemId: row.business_system_id,
      kind: row.kind,
      summary: row.summary,
      status: row.status,
      inputs: parseList(row.inputs),
      outputs: parseList(row.outputs),
      producer: row.producer ? parseJson(row.producer) as BusinessConversion["producer"] : undefined,
      metadata: parseJson(row.metadata),
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    }));
  }

  private artifactFromRow(row: any): Artifact {
    const metadata = parseJson(row.metadata);
    return {
      id: row.id,
      projectId: row.project_id,
      sourceId: row.source_id ?? undefined,
      recordType: metadata.record_type === "material" ? "material" : "artifact",
      kind: row.kind,
      ref: row.ref,
      metadata,
      title: typeof metadata.title === "string" ? metadata.title : undefined,
      description: typeof metadata.description === "string" ? metadata.description : undefined,
      summary: typeof metadata.summary === "string" ? metadata.summary : undefined,
      purpose: typeof metadata.purpose === "string" ? metadata.purpose : undefined,
      status: typeof metadata.status === "string" ? metadata.status : undefined,
      producer: metadata.producer && typeof metadata.producer === "object" ? metadata.producer as Artifact["producer"] : undefined,
      files: Array.isArray(metadata.files) ? metadata.files : undefined,
      inputs: Array.isArray(metadata.inputs) ? metadata.inputs : undefined,
      outputs: Array.isArray(metadata.outputs) ? metadata.outputs : undefined,
      lineage: Array.isArray(metadata.lineage) ? metadata.lineage : undefined,
      createdAt: row.created_at,
    };
  }

  createSecretRequest(input: { projectId: string; name: string; vaultRef: string; status?: SecretRequest["status"]; metadata?: Record<string, unknown> }): SecretRequest {
    const timestamp = now();
    const value: SecretRequest = { id: randomUUID(), projectId: input.projectId, name: input.name, vaultRef: input.vaultRef,
      status: input.status ?? "requested", metadata: input.metadata ?? {}, createdAt: timestamp, updatedAt: timestamp };
    this.db.query("INSERT INTO secret_requests (id,project_id,name,vault_ref,status,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)")
      .run(value.id, value.projectId, value.name, value.vaultRef, value.status, json(value.metadata), timestamp, timestamp);
    return value;
  }

  addTalkMessage(input: { role?: string; body: string; metadata?: Record<string, unknown> }): TalkMessage {
    const value: TalkMessage = { id: randomUUID(), role: input.role ?? "user", body: input.body, metadata: input.metadata ?? {}, createdAt: now() };
    this.db.query("INSERT INTO business_talk_messages (id,role,body,metadata,created_at) VALUES (?,?,?,?,?)").run(value.id, value.role, value.body, json(value.metadata), value.createdAt);
    return value;
  }

  listTalkMessages(): TalkMessage[] {
    return this.db.query("SELECT * FROM business_talk_messages ORDER BY created_at, id").all().map((row: any) => ({
      id: row.id, role: row.role, body: row.body, metadata: parseJson(row.metadata), createdAt: row.created_at,
    }));
  }

  queueForemanMessage(messageId: string, text: string): ForemanEnvelope {
    const value: ForemanEnvelope = { id: randomUUID(), messageId, text, status: "queued", createdAt: now() };
    this.db.query("INSERT INTO foreman_outbox (id,message_id,text,status,created_at,claimed_at) VALUES (?,?,?,?,?,NULL)").run(value.id, value.messageId, value.text, value.status, value.createdAt);
    return value;
  }

  bridgeHumanTalk(messageId: string, text: string): void {
    if (!this.bridgeLegacy || this.legacy || !this.hasLegacyTable("talk_messages") || !this.hasLegacyTable("agent_messages")) return;
    const timestamp = now();
    const mailboxId = `bun-am-${randomUUID()}`;
    this.db.query("INSERT OR IGNORE INTO talk_messages (id,role,kind,by_name,body,reply_state,in_reply_to,card_type,ref_kind,ref_id,card,mailbox_id,created_at) VALUES (?,?,?,?,?,'open',NULL,NULL,NULL,NULL,'{}',?,?)")
      .run(messageId, "human", "text", "bun", text, JSON.stringify({}), mailboxId, timestamp);
    const messageBody = JSON.stringify({ message: messageId, by: "bun", text });
    this.db.query("INSERT INTO agents (name,state,updated_at) VALUES ('foreman','active',?) ON CONFLICT(name) DO NOTHING").run(timestamp);
    const columnStatement = this.db.query("PRAGMA table_info(agent_messages)") as any;
    const columns = columnStatement.all() as Array<{ name: string }>;
    columnStatement.finalize?.();
    const hasSequence = columns.some((column) => column.name === "enqueue_seq");
    if (hasSequence) {
      const next = this.db.query("SELECT COALESCE(MAX(enqueue_seq),0) + 1 AS next FROM agent_messages").get() as { next: number };
      this.db.query("INSERT OR IGNORE INTO agent_messages (id,agent,kind,source,body,state,turn,attempts,not_before,created_at,updated_at,enqueue_seq) VALUES (?,?,?,?,?,'pending',NULL,0,NULL,?,?,?)")
        .run(mailboxId, "foreman", "talk", `talk:${messageId}`, messageBody, timestamp, timestamp, next.next);
    } else {
      this.db.query("INSERT OR IGNORE INTO agent_messages (id,agent,kind,source,body,state,turn,attempts,not_before,created_at,updated_at) VALUES (?,?,?,?,?,'pending',NULL,0,NULL,?,?)")
        .run(mailboxId, "foreman", "talk", `talk:${messageId}`, messageBody, timestamp, timestamp);
    }
  }

  listForemanInbox(limit = 50): ForemanEnvelope[] {
    const bounded = Math.max(1, Math.min(200, Math.trunc(limit)));
    return this.db.query("SELECT * FROM foreman_outbox WHERE status IN ('queued','claimed') ORDER BY created_at, id LIMIT ?").all(bounded).map((row: any) => ({
      id: row.id,
      messageId: row.message_id,
      text: row.text,
      status: row.status,
      createdAt: row.created_at,
      claimedAt: row.claimed_at ?? undefined,
    }));
  }

  claimForemanMessage(id: string): ForemanEnvelope | undefined {
    const claimedAt = now();
    this.db.query("UPDATE foreman_outbox SET status='claimed', claimed_at=? WHERE id=? AND status='queued'").run(claimedAt, id);
    const row = this.db.query("SELECT * FROM foreman_outbox WHERE id=?").get(id) as any;
    if (!row) return undefined;
    return { id: row.id, messageId: row.message_id, text: row.text, status: row.status, createdAt: row.created_at, claimedAt: row.claimed_at ?? undefined };
  }

  completeForemanMessage(id: string): ForemanEnvelope | undefined {
    this.db.query("UPDATE foreman_outbox SET status='done' WHERE id=? AND status IN ('queued','claimed')").run(id);
    const row = this.db.query("SELECT * FROM foreman_outbox WHERE id=?").get(id) as any;
    if (!row) return undefined;
    return { id: row.id, messageId: row.message_id, text: row.text, status: row.status, createdAt: row.created_at, claimedAt: row.claimed_at ?? undefined };
  }

  listSecretRequests(): SecretRequest[] {
    return this.db.query("SELECT * FROM secret_requests ORDER BY created_at").all().map((row: any) => ({
      id: row.id, projectId: row.project_id, name: row.name, vaultRef: row.vault_ref, status: row.status,
      metadata: parseJson(row.metadata), createdAt: row.created_at, updatedAt: row.updated_at,
    }));
  }

  getSecretRequest(id: string): SecretRequest | undefined {
    const row = this.db.query("SELECT * FROM secret_requests WHERE id = ?").get(id) as any;
    if (!row) return undefined;
    return {
      id: row.id,
      projectId: row.project_id,
      name: row.name,
      vaultRef: row.vault_ref,
      status: row.status,
      metadata: parseJson(row.metadata),
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    };
  }

  markSecretAvailable(id: string): SecretRequest | undefined {
    const timestamp = now();
    this.db.query("UPDATE secret_requests SET status = 'available', updated_at = ? WHERE id = ?").run(timestamp, id);
    return this.getSecretRequest(id);
  }

  appendEvent(type: string, data: unknown): Event {
    const value = { id: randomUUID(), type, data, createdAt: now() };
    const result = this.db.query("INSERT INTO business_events (id,type,data,created_at) VALUES (?,?,?,?)").run(value.id, value.type, json(value.data), value.createdAt);
    return { ...value, seq: Number(result.lastInsertRowid) };
  }

  eventsAfter(seq = 0): Event[] {
    return this.db.query("SELECT * FROM business_events WHERE seq > ? ORDER BY seq").all(seq).map((row: any) => ({
      id: row.id, seq: Number(row.seq), type: row.type, data: JSON.parse(row.data), createdAt: row.created_at,
    }));
  }

  legacyEvents(): Event[] {
    const source = this.legacy ?? this.db;
    if (!this.hasLegacyTable("events")) return [];
    return source.query("SELECT seq,id,kind,data,at FROM events ORDER BY seq").all().map((row: any) => ({
      seq: Number(row.seq), id: row.id, type: row.kind, data: parseJson(row.data), createdAt: row.at,
    }));
  }

  legacyArtifacts(): Artifact[] {
    const source = this.legacy ?? this.db;
    if (!this.hasLegacyTable("asset_records")) return [];
    return source.query("SELECT id,document,created_at,by_actor FROM asset_records ORDER BY created_at").all().map((row: any) => ({
      id: row.id, projectId: "legacy", kind: "asset", ref: row.id, metadata: { document: parseJson(row.document), byActor: row.by_actor }, createdAt: row.created_at,
    }));
  }

  legacyTalk(): TalkMessage[] {
    const source = this.legacy ?? this.db;
    if (!this.hasLegacyTable("talk_messages")) return [];
    return source.query("SELECT id,role,body,card,created_at FROM talk_messages ORDER BY created_at, id").all().map((row: any) => ({
      id: row.id, role: row.role, body: row.body, metadata: { card: parseJson(row.card) }, createdAt: row.created_at,
    }));
  }

  private hasLegacyTable(name: string) {
    if (this.legacy) {
      const statement = this.legacy.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?") as any;
      const result = Boolean(statement.get(name));
      statement.finalize?.();
      return result;
    }
    const statement = this.db.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?") as any;
    const result = Boolean(statement.get(name));
    statement.finalize?.();
    return result;
  }

  close() {
    // Bun may report a transient WAL lock while a just-finished read statement
    // is being finalized. Checkpoint before closing and keep shutdown idempotent.
    try { this.db.exec("PRAGMA wal_checkpoint(PASSIVE)"); } catch { /* closing already in progress */ }
    const bun = (globalThis as { Bun?: { gc?: (force?: boolean) => void } }).Bun;
    bun?.gc?.(true);
    try { this.db.close(true); } catch (error) {
      if (!(error instanceof Error) || !error.message.toLowerCase().includes("locked")) throw error;
      try { this.db.close(false); } catch { /* the process will release the handle */ }
    }
    try { this.legacy?.close(true); } catch { /* read-only legacy handles are best effort */ }
  }
}

