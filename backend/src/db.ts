import { constants } from "bun:sqlite";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";
import { ManagedDatabase } from "./sqlite";
import type { StateDatabase } from "./state-database";
import { randomUuid, SystemClock, type Clock } from "./legacy/foundation";
import { defaultSessionTimeoutSeconds, validSessionTimeout } from "./harness-turn";
import type { OperatorBinding } from "./operator-binding";
import type { UnitRunOwner } from "./unit-run-owner";
import type {
  Artifact,
  BusinessSystem,
  BusinessConversion,
  BusinessRun,
  BusinessUnit,
  Event,
  ForemanEnvelope,
  HarnessProfile,
  MachineAttempt,
  Project,
  ProjectSource,
  SecretRequest,
  TalkMessage,
  UnitRun,
} from "./models";

export const BUSINESS_RECORD_SCHEMA = "mao.business-record/1";

const defaultFactoryDatabase = () => process.env.FACTORY_DB_PATH ?? join(process.env.FACTORY_ROOT ?? ".factory", "factory.db");
const defaultContextTokens = (role: string): number => role === "foreman" ? 500_000 : 256_000;
const json = (value: unknown) => JSON.stringify(value ?? {});
const secretField = /(secret|token|password|api[_-]?key|authorization|cookie|local[_-]?folder|host[_-]?path)/i;
const secretText = /(api[_-]?key|secret|token|password|authorization|cookie)\s*[:=]\s*\S+/gi;
function redactStoredValue(value: unknown, key = ""): unknown {
  if (secretField.test(key)) return "[REDACTED]";
  if (typeof value === "string") return value.replace(secretText, (match) => `${match.split(/[:=]/, 1)[0]}=[REDACTED]`);
  if (Array.isArray(value)) return value.map((item) => redactStoredValue(item));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, redactStoredValue(item, name)]));
  return value;
}
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

function elapsedMilliseconds(start: string, end: string): number {
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  return Number.isFinite(startMs) && Number.isFinite(endMs) ? Math.max(0, endMs - startMs) : 0;
}

function mergedConversionStatus(current: BusinessConversion["status"], incoming: BusinessConversion["status"]): BusinessConversion["status"] {
  const rank: Record<BusinessConversion["status"], number> = { planned: 0, running: 1, failed: 2, completed: 3 };
  return rank[incoming] > rank[current] ? incoming : current;
}

export interface IntakeInput {
  name?: string;
  project_name?: string;
  business_system_id?: string;
  repo_url?: string;
  uploaded_file_refs?: string[];
  files?: Array<{ name?: string; ref?: string; digest?: string }>;
  local_folder?: string;
  portable_sources?: Array<{ kind: ProjectSource["kind"]; ref?: string; metadata?: Record<string, unknown> }>;
  skip_default_units?: boolean;
  metadata?: Record<string, unknown>;
  secret_requests?: Array<{ name: string; metadata?: Record<string, unknown> }>;
}

type BusinessRunEdge = { source: string; target: string };
export type ArtifactFileGrant = { artifactId: string; position: number; digest: string; size: number };
type BusinessAssetCursor = { source: "business"; createdAt: string; id: string };
type LegacyAssetCursor = { source: "legacy"; createdAt: string; rowId: string };
export type AssetCursor = BusinessAssetCursor | LegacyAssetCursor;
type LegacyAssetRow = { id: string; document: string; created_at: string; by_actor: string; row_id: string };
type LegacyBridgeRow = { seq: number; id: string; kind: string; at: string; data: string; job_id?: string | null; run_id?: string | null };

export class SqliteStore {
  readonly db: StateDatabase;
  readonly legacy?: StateDatabase;
  private readonly bridgeLegacy: boolean;

  readonly clock: Clock;
  readonly uuid: () => string;

  constructor(
    readonly dbPath = process.env.BUSINESS_DB_PATH ?? defaultFactoryDatabase(),
    readonly legacyPath = defaultFactoryDatabase(),
    options: { clock?: Clock; uuid?: () => string } = {},
  ) {
    this.clock = options.clock ?? new SystemClock();
    this.uuid = options.uuid ?? randomUuid;
    if (dbPath !== ":memory:") mkdirSync(dirname(dbPath), { recursive: true });
    this.db = new ManagedDatabase(dbPath, { create: true });
    this.db.exec("PRAGMA busy_timeout = 5000");
    this.db.exec("PRAGMA journal_mode = WAL");
    this.init();
    this.bridgeLegacy = dbPath === legacyPath && /factory\.db$/i.test(dbPath.replaceAll("\\", "/"));
    if (dbPath === legacyPath) return;
    try {
      const uri = pathToFileURL(legacyPath);
      uri.searchParams.set("immutable", "1");
      this.legacy = new ManagedDatabase(uri.href, constants.SQLITE_OPEN_READONLY | constants.SQLITE_OPEN_URI);
    } catch {
      // A missing legacy database is normal for a new installation.
    }
  }

  timestamp(): string { return this.clock.now().toISOString(); }

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
      CREATE TABLE IF NOT EXISTS intake_uploads (digest TEXT PRIMARY KEY, size INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS artifacts (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_id TEXT, kind TEXT NOT NULL,
        ref TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS artifact_file_grants (
        artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
        position INTEGER NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL,
        PRIMARY KEY (artifact_id, position)
      );
      CREATE TABLE IF NOT EXISTS secret_requests (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, vault_ref TEXT NOT NULL,
        status TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS secret_audit (
        id TEXT PRIMARY KEY, request_id TEXT NOT NULL, action TEXT NOT NULL,
        unit_run_id TEXT, actor TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS secret_audit_request ON secret_audit(request_id, created_at, id);
      CREATE TABLE IF NOT EXISTS harness_profiles (
        id TEXT PRIMARY KEY, runtime TEXT NOT NULL, role TEXT NOT NULL, base_url TEXT NOT NULL,
        model TEXT NOT NULL, mode TEXT NOT NULL, execution_mode TEXT, image TEXT, network TEXT, secret_ref TEXT,
        verification TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
        session_timeout_s INTEGER NOT NULL DEFAULT ${defaultSessionTimeoutSeconds},
        thinking_effort TEXT NOT NULL DEFAULT 'medium', context_tokens INTEGER NOT NULL DEFAULT 256000,
        session_budget_usd REAL NOT NULL DEFAULT 1, daily_budget_usd REAL NOT NULL DEFAULT 10,
        max_turns INTEGER NOT NULL DEFAULT 20, max_attempts INTEGER NOT NULL DEFAULT 2,
        backoff_s INTEGER NOT NULL DEFAULT 60
      );
      CREATE TABLE IF NOT EXISTS business_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, type TEXT NOT NULL,
        data TEXT NOT NULL, created_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS legacy_event_bridge (
        source_key TEXT NOT NULL, legacy_seq INTEGER NOT NULL, legacy_id TEXT NOT NULL,
        business_seq INTEGER NOT NULL, bridged_at TEXT NOT NULL,
        PRIMARY KEY (source_key, legacy_seq), UNIQUE (source_key, legacy_id)
      );
      CREATE TABLE IF NOT EXISTS business_talk_messages (
        id TEXT PRIMARY KEY, role TEXT NOT NULL, body TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS foreman_outbox (
        id TEXT PRIMARY KEY, message_id TEXT NOT NULL, text TEXT NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL, queued_at TEXT, claimed_at TEXT,
        started_at TEXT, last_event_at TEXT, completed_at TEXT, elapsed_ms INTEGER,
        business_status TEXT NOT NULL DEFAULT 'not_started', business_reason TEXT
      );
      -- The legacy factory database already owns foreman_sessions with a
      -- different schema. Keep that table available to the legacy status and
      -- retention readers and store Bun Foreman resume cursors separately.
      CREATE TABLE IF NOT EXISTS business_foreman_sessions (
        profile_id TEXT PRIMARY KEY, session_id TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS unit_secret_requests (
        unit_run_id TEXT NOT NULL, request_id TEXT NOT NULL, PRIMARY KEY (unit_run_id, request_id)
      );
      CREATE INDEX IF NOT EXISTS unit_secret_by_request ON unit_secret_requests(request_id);
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
      CREATE TABLE IF NOT EXISTS business_run_dispatch (run_id TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS business_run_graphs (run_id TEXT PRIMARY KEY, edges TEXT NOT NULL, nodes TEXT, edges_indexed INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS business_run_edges (
        run_id TEXT NOT NULL, source_id TEXT NOT NULL, target_id TEXT NOT NULL, position INTEGER NOT NULL,
        PRIMARY KEY (run_id, source_id, target_id)
      ) WITHOUT ROWID;
      CREATE INDEX IF NOT EXISTS business_run_edge_predecessors ON business_run_edges(run_id, target_id, position);
      CREATE TABLE IF NOT EXISTS unit_runs (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, unit_id TEXT NOT NULL,
        status TEXT NOT NULL, session_id TEXT, inputs TEXT NOT NULL DEFAULT '[]',
        outputs TEXT NOT NULL DEFAULT '[]', error TEXT, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, UNIQUE(run_id, unit_id)
      );
      CREATE TABLE IF NOT EXISTS machine_attempts (
        unit_run_id TEXT PRIMARY KEY REFERENCES unit_runs(id), attempt INTEGER NOT NULL,
        session_id TEXT NOT NULL, owner_token TEXT, status TEXT NOT NULL, error TEXT,
        started_at TEXT NOT NULL, finished_at TEXT
      );
      CREATE TABLE IF NOT EXISTS unit_run_owners (
        unit_run_id TEXT PRIMARY KEY REFERENCES unit_runs(id), token TEXT NOT NULL, scope TEXT, pid INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS unit_run_owners_scope ON unit_run_owners(scope);
      CREATE TABLE IF NOT EXISTS business_conversions (
        id TEXT PRIMARY KEY, business_system_id TEXT NOT NULL, kind TEXT NOT NULL,
        summary TEXT NOT NULL, status TEXT NOT NULL, inputs TEXT NOT NULL DEFAULT '[]',
        outputs TEXT NOT NULL DEFAULT '[]', producer TEXT, metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS business_talk_messages_created ON business_talk_messages(created_at, id);
      CREATE INDEX IF NOT EXISTS business_talk_foreman_reply ON business_talk_messages (
        role, CASE WHEN json_valid(metadata) THEN json_extract(metadata,'$.inbox_id') END
      );
      CREATE INDEX IF NOT EXISTS projects_system_created ON projects(business_system_id, created_at, id);
      CREATE INDEX IF NOT EXISTS project_sources_project_created ON project_sources(project_id, created_at, id);
      CREATE INDEX IF NOT EXISTS secret_requests_project_created ON secret_requests(project_id, created_at, id);
      CREATE INDEX IF NOT EXISTS artifacts_project_created ON artifacts(project_id, created_at, id);
      CREATE INDEX IF NOT EXISTS artifacts_created ON artifacts(created_at, id);
      CREATE INDEX IF NOT EXISTS artifacts_ref_created ON artifacts(ref, created_at DESC, id DESC);
      CREATE INDEX IF NOT EXISTS secret_requests_project ON secret_requests(project_id, created_at);
      CREATE INDEX IF NOT EXISTS foreman_outbox_status ON foreman_outbox(status, created_at, id);
      CREATE INDEX IF NOT EXISTS foreman_outbox_message ON foreman_outbox(message_id);
      CREATE INDEX IF NOT EXISTS business_units_system ON business_units(business_system_id, kind, created_at, id);
      CREATE INDEX IF NOT EXISTS business_runs_system ON business_runs(business_system_id, created_at, id);
      CREATE INDEX IF NOT EXISTS unit_runs_run ON unit_runs(run_id, status, created_at, id);
      CREATE INDEX IF NOT EXISTS unit_runs_history ON unit_runs(run_id, created_at, id);
      CREATE INDEX IF NOT EXISTS unit_runs_pending_dispatch ON unit_runs(run_id) WHERE status='queued';
      CREATE INDEX IF NOT EXISTS unit_runs_waiting_resources ON unit_runs(run_id) WHERE status='waiting_resources';
      CREATE INDEX IF NOT EXISTS unit_runs_unfinished ON unit_runs(run_id) WHERE status!='completed';
      CREATE INDEX IF NOT EXISTS business_conversions_system ON business_conversions(business_system_id, created_at, id);
    `);
    this.db.transaction(() => {
      const duplicates = this.db.query<{ duplicate: string }>(`
        SELECT duplicate.id AS duplicate
        FROM business_conversions AS keep
        JOIN business_conversions AS duplicate
          ON duplicate.business_system_id=keep.business_system_id
         AND duplicate.kind=keep.kind
         AND duplicate.summary=keep.summary
         AND duplicate.inputs=keep.inputs
         AND duplicate.outputs=keep.outputs
         AND duplicate.id > keep.id
      `).all();
      for (const { duplicate } of duplicates) this.db.query("DELETE FROM business_conversions WHERE id=?").run(duplicate);
      this.db.exec("CREATE UNIQUE INDEX IF NOT EXISTS business_conversions_identity ON business_conversions(business_system_id, kind, summary, inputs, outputs)");
    }).immediate();
    this.addColumnIfMissing("harness_profiles", "session_timeout_s", `INTEGER NOT NULL DEFAULT ${defaultSessionTimeoutSeconds}`);
    this.addColumnIfMissing("harness_profiles", "thinking_effort", "TEXT NOT NULL DEFAULT 'medium'");
    const hadContextTokens = this.db.query<{ name: string }>("PRAGMA table_info(harness_profiles)").all().some(({ name }) => name === "context_tokens");
    this.addColumnIfMissing("harness_profiles", "context_tokens", "INTEGER NOT NULL DEFAULT 256000");
    if (!hadContextTokens) this.db.query("UPDATE harness_profiles SET context_tokens=500000 WHERE role='foreman'").run();
    this.addColumnIfMissing("harness_profiles", "session_budget_usd", "REAL NOT NULL DEFAULT 1");
    this.addColumnIfMissing("harness_profiles", "daily_budget_usd", "REAL NOT NULL DEFAULT 10");
    this.addColumnIfMissing("harness_profiles", "max_turns", "INTEGER NOT NULL DEFAULT 20");
    this.addColumnIfMissing("harness_profiles", "max_attempts", "INTEGER NOT NULL DEFAULT 2");
    this.addColumnIfMissing("harness_profiles", "backoff_s", "INTEGER NOT NULL DEFAULT 60");
    this.addColumnIfMissing("harness_profiles", "execution_mode", "TEXT");
    this.db.query("UPDATE harness_profiles SET execution_mode=CASE WHEN role='foreman' THEN 'trusted_container' ELSE 'native_host' END WHERE execution_mode IS NULL").run();
    this.addColumnIfMissing("business_run_graphs", "edges_indexed", "INTEGER NOT NULL DEFAULT 0");
    this.addColumnIfMissing("business_run_graphs", "nodes", "TEXT");
    this.addColumnIfMissing("machine_attempts", "owner_token", "TEXT");
    this.addColumnIfMissing("foreman_outbox", "started_at", "TEXT");
    this.addColumnIfMissing("foreman_outbox", "queued_at", "TEXT");
    this.addColumnIfMissing("foreman_outbox", "last_event_at", "TEXT");
    this.addColumnIfMissing("foreman_outbox", "completed_at", "TEXT");
    this.addColumnIfMissing("foreman_outbox", "elapsed_ms", "INTEGER");
    this.addColumnIfMissing("foreman_outbox", "business_status", "TEXT NOT NULL DEFAULT 'not_started'");
    this.addColumnIfMissing("foreman_outbox", "business_reason", "TEXT");
    this.db.query("UPDATE foreman_outbox SET queued_at=created_at WHERE queued_at IS NULL").run();
    this.db.exec(`
      UPDATE business_talk_messages
      SET metadata=json_set(CASE WHEN json_valid(metadata) THEN metadata ELSE '{}' END,
        '$.historical_orphan', 1,
        '$.historical_reason', 'missing_foreman_outbox')
      WHERE role IN ('user','human')
        AND json_valid(metadata)
        AND json_extract(metadata, '$.source')='project-intake'
        AND NOT EXISTS (SELECT 1 FROM foreman_outbox WHERE foreman_outbox.message_id=business_talk_messages.id)
        AND (json_extract(metadata, '$.historical_orphan') IS NULL OR json_extract(metadata, '$.historical_orphan') != 1)
    `);
  }

  private addColumnIfMissing(table: string, column: string, definition: string): void {
    const hasColumn = () => this.db.query<{ name: string }>(`PRAGMA table_info(${table})`).all().some(({ name }) => name === column);
    if (hasColumn()) return;
    this.db.transaction(() => {
      if (!hasColumn()) this.db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
    }).immediate();
  }

  listBusinessSystems(): BusinessSystem[] {
    return this.db.query("SELECT * FROM business_systems ORDER BY created_at").all().map((row) => this.businessSystemFromRow(row));
  }

  listBusinessSystemSummaries() {
    type Summary = Pick<BusinessSystem, "id" | "name" | "status">;
    const rows = this.db.query<Summary & { description: string | null }>(
      "SELECT id, name, description, status FROM business_systems ORDER BY created_at",
    ).all();
    return rows.map(({ description, ...summary }) => ({ ...summary, description: description ?? undefined }));
  }

  getBusinessSystem(id: string): BusinessSystem | undefined {
    const row = this.db.query("SELECT * FROM business_systems WHERE id = ?").get(id);
    return row ? this.businessSystemFromRow(row) : undefined;
  }

  countProjectsByBusinessSystem(): Map<string, number> {
    const rows = this.db.query<{ id: string; count: number }>(`
      SELECT business_system_id AS id, COUNT(*) AS count FROM projects
      WHERE business_system_id IS NOT NULL GROUP BY business_system_id
    `).all();
    return new Map(rows.map(({ id, count }) => [id, count]));
  }

  private businessSystemFromRow(row: any): BusinessSystem {
    return {
      id: row.id, name: row.name, description: row.description ?? undefined, status: row.status,
      metadata: parseJson(row.metadata), createdAt: row.created_at, updatedAt: row.updated_at,
    };
  }

  listProjects(businessSystemId?: string): Project[] {
    const db = this.db;
    const rows = businessSystemId === undefined
      ? db.query("SELECT * FROM projects ORDER BY created_at, id").all()
      : db.query("SELECT * FROM projects WHERE business_system_id = ? ORDER BY created_at, id").all(businessSystemId);
    const sourceRows = businessSystemId === undefined
      ? db.query("SELECT * FROM project_sources ORDER BY created_at, id").all()
      : db.query(`SELECT source.* FROM projects AS project JOIN project_sources AS source ON source.project_id = project.id
          WHERE project.business_system_id = ? ORDER BY source.created_at, source.id`).all(businessSystemId);
    const requestRows = businessSystemId === undefined
      ? db.query("SELECT * FROM secret_requests ORDER BY created_at, id").all()
      : db.query(`SELECT request.* FROM projects AS project JOIN secret_requests AS request ON request.project_id = project.id
          WHERE project.business_system_id = ? ORDER BY request.created_at, request.id`).all(businessSystemId);
    const sources = new Map<string, any[]>();
    for (const row of sourceRows as any[]) {
      const values = sources.get(row.project_id) ?? [];
      values.push(row);
      sources.set(row.project_id, values);
    }
    const secretRequests = new Map<string, any[]>();
    for (const row of requestRows as any[]) {
      const values = secretRequests.get(row.project_id) ?? [];
      values.push(row);
      secretRequests.set(row.project_id, values);
    }
    return (rows as any[]).map((row) => this.projectFromRow(row, sources.get(row.id) ?? [], secretRequests.get(row.id) ?? []));
  }

  getProject(id: string): Project | undefined {
    const row = this.db.query("SELECT * FROM projects WHERE id = ?").get(id) as any;
    return row ? this.projectFromRow(row) : undefined;
  }

  createBusinessSystem(input: Partial<BusinessSystem> & { name: string }): BusinessSystem {
    const timestamp = this.timestamp();
    const metadata = redactStoredValue(input.metadata ?? {}) as Record<string, unknown>;
    const value: BusinessSystem = { id: input.id ?? this.uuid(), name: input.name, description: input.description,
      status: input.status ?? "active", metadata, createdAt: timestamp, updatedAt: timestamp };
    this.db.query("INSERT INTO business_systems (id,name,description,status,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?)")
      .run(value.id, value.name, value.description ?? null, value.status, json(value.metadata), timestamp, timestamp);
    return value;
  }

  updateBusinessSystemMetadata(id: string, metadata: Record<string, unknown>): BusinessSystem | undefined {
    const current = this.db.query("SELECT * FROM business_systems WHERE id = ?").get(id) as any;
    if (!current) return undefined;
    const merged = redactStoredValue({ ...parseJson(current.metadata), ...metadata }) as Record<string, unknown>;
    const updatedAt = this.timestamp();
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
      executionMode: row.execution_mode === "native_host" ? "native_host" : row.role === "foreman" ? "trusted_container" : "native_host",
      sessionTimeoutSeconds: row.session_timeout_s,
      thinkingEffort: row.thinking_effort ?? (row.role === "foreman" ? "max" : "medium"),
      contextTokens: row.context_tokens ?? defaultContextTokens(row.role),
      sessionBudgetUsd: row.session_budget_usd ?? 1,
      dailyBudgetUsd: row.daily_budget_usd ?? 10,
      maxTurns: row.max_turns ?? 20,
      maxAttempts: row.max_attempts ?? 2,
      backoffSeconds: row.backoff_s ?? 60,
      image: row.image ?? undefined,
      network: row.network ?? undefined,
      secretRef: row.secret_ref ?? undefined,
      verification: parseJson(row.verification) as HarnessProfile["verification"],
      updatedAt: row.updated_at,
    }));
  }

  saveHarnessProfile(input: Omit<HarnessProfile, "updatedAt" | "id" | "sessionTimeoutSeconds"> & { id?: string; sessionTimeoutSeconds?: number }): HarnessProfile {
    const timeout = input.sessionTimeoutSeconds;
    if (timeout !== undefined && !validSessionTimeout(timeout)) throw new RangeError("invalid Harness session timeout");
    const updatedAt = this.timestamp();
    const value = { ...input, id: input.id ?? `${input.role}:${input.runtime}`, updatedAt };
    const row = this.db.query<{ session_timeout_s: number }>(`INSERT INTO harness_profiles (
      id,runtime,role,base_url,model,mode,execution_mode,image,network,secret_ref,verification,updated_at,session_timeout_s,
        thinking_effort,context_tokens,session_budget_usd,daily_budget_usd,max_turns,max_attempts,backoff_s
      ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
        runtime=excluded.runtime, role=excluded.role, base_url=excluded.base_url, model=excluded.model,
        mode=excluded.mode, execution_mode=excluded.execution_mode, image=excluded.image, network=excluded.network, secret_ref=excluded.secret_ref,
        verification=excluded.verification, updated_at=excluded.updated_at,
        session_timeout_s=COALESCE(?, harness_profiles.session_timeout_s),
        thinking_effort=COALESCE(?, harness_profiles.thinking_effort),
        context_tokens=COALESCE(?, harness_profiles.context_tokens),
        session_budget_usd=COALESCE(?, harness_profiles.session_budget_usd),
        daily_budget_usd=COALESCE(?, harness_profiles.daily_budget_usd),
        max_turns=COALESCE(?, harness_profiles.max_turns),
        max_attempts=COALESCE(?, harness_profiles.max_attempts),
        backoff_s=COALESCE(?, harness_profiles.backoff_s)
      RETURNING session_timeout_s`)
      .get(value.id, value.runtime, value.role, value.baseUrl, value.model, value.mode, value.executionMode ?? "native_host", value.image ?? null, value.network ?? null,
        value.secretRef ?? null, json(value.verification ?? {}), updatedAt, timeout ?? defaultSessionTimeoutSeconds,
        value.thinkingEffort ?? (value.role === "foreman" ? "max" : "medium"), value.contextTokens ?? defaultContextTokens(value.role),
        value.sessionBudgetUsd ?? 1, value.dailyBudgetUsd ?? 10, value.maxTurns ?? 20, value.maxAttempts ?? 2,
        value.backoffSeconds ?? 60, timeout ?? null, value.thinkingEffort ?? null, value.contextTokens ?? null,
        value.sessionBudgetUsd ?? null, value.dailyBudgetUsd ?? null, value.maxTurns ?? null, value.maxAttempts ?? null,
        value.backoffSeconds ?? null)!;
    return { ...value, sessionTimeoutSeconds: row.session_timeout_s,
      thinkingEffort: value.thinkingEffort ?? (value.role === "foreman" ? "max" : "medium"), contextTokens: value.contextTokens ?? defaultContextTokens(value.role),
      sessionBudgetUsd: value.sessionBudgetUsd ?? 1, dailyBudgetUsd: value.dailyBudgetUsd ?? 10,
      maxTurns: value.maxTurns ?? 20, maxAttempts: value.maxAttempts ?? 2, backoffSeconds: value.backoffSeconds ?? 60 };
  }

  markHarnessVerification(id: string, state: string, reason?: string): HarnessProfile | undefined {
    const current = this.listHarnessProfiles().find((profile) => profile.id === id);
    if (!current) return undefined;
    return this.saveHarnessProfile({ ...current, verification: { state, reason, checkedAt: this.timestamp() } });
  }

  createIntake(input: IntakeInput, identity?: { projectId: string; sourceIds: readonly string[] }): Project {
    const timestamp = this.timestamp();
    const projectMetadata = redactStoredValue(input.metadata ?? {}) as Record<string, unknown>;
    const project: Project = { id: identity?.projectId ?? this.uuid(), businessSystemId: input.business_system_id,
      name: input.name ?? input.project_name ?? "Untitled project", metadata: projectMetadata,
      createdAt: timestamp, updatedAt: timestamp, sources: [], secretRequests: [] };
    this.db.query("INSERT INTO projects (id,business_system_id,name,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?)")
      .run(project.id, project.businessSystemId ?? null, project.name, json(project.metadata), timestamp, timestamp);
    const addSource = (kind: ProjectSource["kind"], fields: { repoUrl?: string; fileRef?: string; localFolder?: string }, metadata: Record<string, unknown> = {}) => {
      const source: ProjectSource = { id: identity?.sourceIds[project.sources.length] ?? this.uuid(), projectId: project.id, kind, ...fields, metadata, createdAt: timestamp };
      const safeMetadata = redactStoredValue(metadata) as Record<string, unknown>;
      this.db.query("INSERT INTO project_sources (id,project_id,kind,repo_url,file_ref,local_folder,metadata,created_at) VALUES (?,?,?,?,?,?,?,?)")
        .run(source.id, source.projectId, source.kind, source.repoUrl ?? null, source.fileRef ?? null, source.localFolder ?? null, json(safeMetadata), timestamp);
      project.sources.push(source);
    };
    if (input.repo_url) addSource("repo_url", { repoUrl: input.repo_url });
    for (const fileRef of input.uploaded_file_refs ?? []) addSource("uploaded_file", { fileRef });
    for (const file of input.files ?? []) {
      const fileRef = file.ref ?? file.name;
      if (fileRef) addSource("uploaded_file", { fileRef }, {
        ...(file.name ? { name: file.name } : {}), ...(file.digest ? { digest: file.digest } : {}),
      });
    }
    if (input.local_folder) addSource("local_folder", { localFolder: input.local_folder });
    for (const source of input.portable_sources ?? []) {
      if (source.kind === "repo_url" && source.ref) addSource("repo_url", { repoUrl: source.ref }, source.metadata);
      else if (source.kind === "uploaded_file" && source.ref) addSource("uploaded_file", { fileRef: source.ref }, source.metadata);
      else if (source.kind === "local_folder") addSource("local_folder", {}, source.metadata);
    }
    for (const request of input.secret_requests ?? []) {
      const secret = this.createSecretRequest({ projectId: project.id, name: request.name, vaultRef: `vault://secret/${this.uuid()}`, metadata: redactStoredValue(request.metadata ?? {}) as Record<string, unknown> });
      if (!project.secretRequests.some(({ id }) => id === secret.id)) project.secretRequests.push(secret);
    }
    if (project.businessSystemId && !input.skip_default_units) {
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

  createBusinessUnit(input: Omit<BusinessUnit, "createdAt" | "updatedAt">): BusinessUnit {
    const timestamp = this.timestamp();
    const metadata = redactStoredValue(input.metadata ?? {}) as Record<string, unknown>;
    this.db.query("INSERT INTO business_units (id,business_system_id,project_id,kind,name,harness_runtime,model,machine_ref,status,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
      .run(input.id, input.businessSystemId, input.projectId ?? null, input.kind, input.name, input.harnessRuntime ?? null,
        input.model ?? null, input.machineRef ?? null, input.status, json(metadata), timestamp, timestamp);
    return { ...input, metadata, createdAt: timestamp, updatedAt: timestamp };
  }

  getBusinessUnit(id: string): BusinessUnit | undefined {
    const row = this.db.query("SELECT * FROM business_units WHERE id = ?").get(id) as any;
    return row ? this.businessUnitFromRow(row) : undefined;
  }

  updateOperatorBinding(businessSystemId: string, id: string, binding: OperatorBinding): BusinessUnit | undefined {
    const row = this.db.query(`
      UPDATE business_units SET harness_runtime = ?, model = ?, updated_at = ?
      WHERE id = ? AND business_system_id = ? AND kind = 'operator' RETURNING *
    `).get(binding.harnessRuntime, binding.model ?? null, this.timestamp(), id, businessSystemId);
    return row ? this.businessUnitFromRow(row) : undefined;
  }

  createBusinessRun(businessSystemId: string, trigger = "manual", metadata: Record<string, unknown> = {}, inputs: string[] = [], dispatch = false): BusinessRun | undefined {
    return this.db.transaction(() => {
      if (!this.db.query("SELECT 1 FROM business_systems WHERE id = ?").get(businessSystemId)) return undefined;
      const units = this.listBusinessUnits(businessSystemId).filter((unit) => unit.status !== "archived");
      const graph = this.businessGraph(businessSystemId);
      const executionUnits = graph.nodes ? units.filter((unit) => graph.nodes!.has(unit.id)) : units;
      const timestamp = this.timestamp();
      const safeMetadata = redactStoredValue(metadata) as Record<string, unknown>;
      const run: BusinessRun = { id: this.uuid(), businessSystemId, status: executionUnits.length ? "queued" : "failed", trigger, metadata: safeMetadata, createdAt: timestamp, updatedAt: timestamp };
      this.db.query("INSERT INTO business_runs (id,business_system_id,status,trigger,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?)")
        .run(run.id, businessSystemId, run.status, trigger, json(safeMetadata), timestamp, timestamp);
      if (dispatch && run.status === "queued") this.authorizeBusinessRunDispatch(run.id);
      const edges = graph.edges;
      this.db.query("INSERT INTO business_run_graphs (run_id, edges, nodes) VALUES (?,?,?)")
        .run(run.id, json(edges), graph.nodes ? json([...graph.nodes]) : null);
      this.indexRunEdges(run.id, edges);
      const insert = this.db.query("INSERT INTO unit_runs (id,run_id,unit_id,status,session_id,inputs,outputs,error,created_at,updated_at) VALUES (?,?,?,'queued',NULL,?,'[]',NULL,?,?)");
      const serializedInputs = json(inputs);
      for (const unit of executionUnits) insert.run(this.uuid(), run.id, unit.id, serializedInputs, timestamp, timestamp);
      return run;
    }).immediate();
  }

  /**
   * Validate references that claim to address a business record. External
   * ingress references such as cas:// URLs are allowed until a Source binds
   * them to a record. Explicit business references must resolve in this
   * business system so a run cannot silently consume another system's data.
   */
  invalidBusinessInputRefs(businessSystemId: string, refs: readonly string[]): string[] {
    const invalid: string[] = [];
    for (const ref of new Set(refs)) {
      if (!/^(?:artifact|material|record):\/\//.test(ref)) continue;
      if (!this.resolveBusinessMaterialRefs(businessSystemId, [ref])[0]?.record) invalid.push(ref);
    }
    return invalid;
  }

  authorizeBusinessRunDispatch(runId: string): void {
    this.db.query("INSERT OR IGNORE INTO business_run_dispatch (run_id) SELECT id FROM business_runs WHERE id=? AND status IN ('queued','running','failed')").run(runId);
  }

  cancelBusinessRun(runId: string): BusinessRun | undefined {
    return this.db.transaction(() => {
      const run = this.getBusinessRun(runId);
      if (!run || ["completed", "failed", "cancelled"].includes(run.status)) return run;
      const timestamp = this.timestamp();
      this.db.query("UPDATE business_runs SET status='cancelled', updated_at=? WHERE id=?").run(timestamp, runId);
      this.db.query("UPDATE unit_runs SET status='cancelled', error='run-cancelled', updated_at=? WHERE run_id=? AND status IN ('queued','running','waiting_resources')").run(timestamp, runId);
      this.db.query("UPDATE machine_attempts SET status='failed', error='run-cancelled', finished_at=? WHERE unit_run_id IN (SELECT id FROM unit_runs WHERE run_id=?) AND status='running'").run(timestamp, runId);
      this.db.query("DELETE FROM unit_run_owners WHERE unit_run_id IN (SELECT id FROM unit_runs WHERE run_id=?)").run(runId);
      this.db.query("DELETE FROM business_run_dispatch WHERE run_id=?").run(runId);
      return this.getBusinessRun(runId);
    }).immediate();
  }

  retryBusinessRun(runId: string): BusinessRun | undefined {
    return this.db.transaction(() => {
      const run = this.getBusinessRun(runId);
      if (!run || !["failed", "cancelled"].includes(run.status)) return run;
      const timestamp = this.timestamp();
      this.db.query("DELETE FROM unit_run_owners WHERE unit_run_id IN (SELECT id FROM unit_runs WHERE run_id=?)").run(runId);
      this.db.query("UPDATE machine_attempts SET status='failed', error=coalesce(error,'run-retried'), finished_at=? WHERE unit_run_id IN (SELECT id FROM unit_runs WHERE run_id=?) AND status='running'").run(timestamp, runId);
      this.db.query("UPDATE unit_runs SET status='queued', session_id=NULL, outputs='[]', error=NULL, updated_at=? WHERE run_id=? AND status!='completed'").run(timestamp, runId);
      this.db.query("UPDATE business_runs SET status='queued', updated_at=? WHERE id=?").run(timestamp, runId);
      this.authorizeBusinessRunDispatch(runId);
      return this.getBusinessRun(runId);
    }).immediate();
  }

  listRecoverableBusinessRunIds(): string[] {
    return this.db.query<{ id: string }>(`
      SELECT run.id FROM (SELECT DISTINCT run_id FROM unit_runs WHERE status='queued') AS pending
      JOIN business_runs AS run ON run.id=pending.run_id
      WHERE run.status IN ('queued','running','failed')
        AND NOT EXISTS (SELECT 1 FROM unit_runs WHERE run_id=run.id AND status='running')
        AND (
          EXISTS (SELECT 1 FROM business_run_dispatch WHERE run_id=run.id)
          OR EXISTS (SELECT 1 FROM unit_runs WHERE run_id=run.id AND status='completed')
        )
      ORDER BY run.created_at, run.id
    `).all().map(({ id }) => id);
  }

  listBusinessRuns(businessSystemId: string): BusinessRun[] {
    return this.db.query("SELECT * FROM business_runs WHERE business_system_id = ? ORDER BY created_at, id").all(businessSystemId).map((row: any) => this.businessRunFromRow(row));
  }

  listBusinessRunsWithUnits(businessSystemId: string): Array<BusinessRun & { units: UnitRun[] }> {
    return this.db.transaction(() => {
      const history = this.listBusinessRuns(businessSystemId).map((run) => ({ ...run, units: [] as UnitRun[] }));
      const byRun = new Map(history.map((run) => [run.id, run]));
      const rows = this.db.query(`
        SELECT unit_run.* FROM unit_runs AS unit_run
        JOIN business_units AS unit ON unit.id=unit_run.unit_id
        WHERE unit_run.run_id IN (SELECT id FROM business_runs WHERE business_system_id = ?)
        ORDER BY unit_run.run_id, CASE unit.kind WHEN 'operator' THEN 0 ELSE 1 END, unit_run.created_at, unit_run.id
      `).all(businessSystemId);
      for (const row of rows) {
        const unit = this.unitRunFromRow(row);
        byRun.get(unit.runId)!.units.push(unit);
      }
      return history;
    })();
  }

  getBusinessRun(id: string): BusinessRun | undefined {
    const row = this.db.query("SELECT * FROM business_runs WHERE id = ?").get(id) as any;
    return row ? this.businessRunFromRow(row) : undefined;
  }

  updateBusinessRunMetadata(id: string, patch: Record<string, unknown>): BusinessRun | undefined {
    return this.db.transaction(() => {
      const current = this.getBusinessRun(id);
      if (!current) return undefined;
      const metadata = redactStoredValue({ ...current.metadata, ...patch }) as Record<string, unknown>;
      this.db.query("UPDATE business_runs SET metadata=?, updated_at=? WHERE id=?").run(json(metadata), this.timestamp(), id);
      return this.getBusinessRun(id);
    }).immediate();
  }

  listUnitRuns(runId: string): UnitRun[] {
    return this.db.query(`
      SELECT unit_run.* FROM unit_runs AS unit_run
      JOIN business_units AS unit ON unit.id=unit_run.unit_id
      WHERE unit_run.run_id = ?
      ORDER BY CASE unit.kind WHEN 'operator' THEN 0 ELSE 1 END, unit_run.created_at, unit_run.id
    `).all(runId).map((row: any) => this.unitRunFromRow(row));
  }

  getUnitRun(runId: string, id: string): UnitRun | undefined {
    const row = this.db.query("SELECT * FROM unit_runs WHERE id = ? AND run_id = ?").get(id, runId);
    return row ? this.unitRunFromRow(row) : undefined;
  }

  nextQueuedUnitRun(runId: string): UnitRun | undefined {
    const row = this.db.query(`
      SELECT unit_run.* FROM unit_runs AS unit_run
      JOIN business_units AS unit ON unit.id=unit_run.unit_id
      WHERE unit_run.run_id = ? AND unit_run.status = 'queued' AND unit.kind='operator'
      ORDER BY unit_run.created_at, unit_run.id LIMIT 1
    `).get(runId);
    return row ? this.unitRunFromRow(row) : undefined;
  }

  listReadyUnitRuns(runId: string, completedUnitRunId?: string): UnitRun[] {
    return this.db.transaction(() => {
      const run = this.getBusinessRun(runId);
      if (!run || run.status === "cancelled") return [];
      if (completedUnitRunId && run.status === "completed") return [];
      if (completedUnitRunId && run.status === "running") {
        const completed = this.getUnitRun(runId, completedUnitRunId);
        const indexed = this.db.query("SELECT 1 FROM business_run_graphs WHERE run_id=? AND edges_indexed=1").get(runId);
        if (completed?.status === "completed" && indexed) return this.listReadySuccessorUnitRuns(completed);
      }
      const unitRuns = this.listUnitRuns(runId);
      const byUnit = new Map(unitRuns.map((item) => [item.unitId, item]));
      const predecessors = new Map<string, UnitRun[]>();
      const successors = new Map<string, UnitRun[]>();
      for (const { source, target } of this.runEdges(run)) {
        const before = byUnit.get(source);
        const after = byUnit.get(target);
        if (!before || !after) continue;
        if (!predecessors.has(target)) predecessors.set(target, []);
        if (!successors.has(source)) successors.set(source, []);
        predecessors.get(target)!.push(before);
        successors.get(source)!.push(after);
      }
      this.activateReadyMachineRuns(run, unitRuns, predecessors);
      this.reconcileBlockedUnitRuns(run, unitRuns, successors);
      const ready: UnitRun[] = [];
      for (const item of unitRuns) {
        if (item.status !== "queued") continue;
        const unit = this.getBusinessUnit(item.unitId);
        if (!unit || unit.kind !== "operator") continue;
        const required = predecessors.get(item.unitId) ?? [];
        if (required.some((predecessor) => predecessor.status !== "completed")) continue;
        this.mergeUnitRunInputs(item, required.flatMap((predecessor) => predecessor.outputs));
        ready.push(item);
      }
      return ready;
    }).immediate();
  }

  /** Returns legacy MAO machine executions that were activated for a run. */
  listReadyKernelMachineRuns(runId: string): UnitRun[] {
    const run = this.getBusinessRun(runId);
    const kernel = run?.metadata.kernel;
    if (!run || run.status === "cancelled" || !kernel || typeof kernel !== "object" || Array.isArray(kernel) || (kernel as Record<string, unknown>).execution !== "legacy") return [];
    return this.db.query(`
      SELECT unit_run.* FROM unit_runs AS unit_run
      JOIN business_units AS unit ON unit.id=unit_run.unit_id
      WHERE unit_run.run_id=? AND unit_run.status='running' AND unit.kind='machine'
        AND unit_run.session_id LIKE 'legacy:%'
      ORDER BY unit_run.created_at, unit_run.id
    `).all(runId).map((row: any) => this.unitRunFromRow(row));
  }

  listReadySourceMachineRuns(runId: string): UnitRun[] {
    return this.db.query(`
      SELECT unit_run.* FROM unit_runs AS unit_run
      JOIN business_units AS unit ON unit.id=unit_run.unit_id
      WHERE unit_run.run_id=? AND unit_run.status='running' AND unit.kind='machine'
        AND unit_run.session_id LIKE 'machine:%'
      ORDER BY unit_run.created_at, unit_run.id
    `).all(runId).map((row: any) => this.unitRunFromRow(row));
  }

  listRunningKernelMachineRuns(): Array<{ runId: string; unitRunId: string }> {
    return this.db.query<{ runId: string; unitRunId: string }>(`
      SELECT unit_run.run_id AS runId, unit_run.id AS unitRunId
      FROM unit_runs AS unit_run
      JOIN business_runs AS run ON run.id=unit_run.run_id
      JOIN business_units AS unit ON unit.id=unit_run.unit_id
      WHERE run.status IN ('queued','running') AND unit_run.status='running'
        AND unit_run.session_id LIKE 'legacy:%' AND unit.kind='machine'
      ORDER BY unit_run.created_at, unit_run.id
    `).all();
  }

  listRunningSourceMachineRuns(): Array<{ runId: string; unitRunId: string }> {
    return this.db.query<{ runId: string; unitRunId: string }>(`
      SELECT unit_run.run_id AS runId, unit_run.id AS unitRunId
      FROM unit_runs AS unit_run
      JOIN business_runs AS run ON run.id=unit_run.run_id
      JOIN business_units AS unit ON unit.id=unit_run.unit_id
      WHERE run.status IN ('queued','running') AND unit_run.status='running'
        AND unit_run.session_id LIKE 'machine:%' AND unit.kind='machine'
      ORDER BY unit_run.created_at, unit_run.id
    `).all();
  }

  startMachineAttempt(unitRunId: string, sessionId: string, _recovering = false): MachineAttempt | undefined {
    return this.db.transaction(() => {
      const unit = this.db.query<{ status: string; session_id: string | null }>("SELECT status, session_id FROM unit_runs WHERE id=? AND status='running'").get(unitRunId);
      if (!unit) return undefined;
      const existing = this.db.query("SELECT * FROM machine_attempts WHERE unit_run_id=?").get(unitRunId) as any;
      const timestamp = this.timestamp();
      if (existing?.status === "running") {
        const ownerToken = existing.owner_token ?? `machine:${unitRunId}:${existing.attempt}`;
        const currentOwner = this.db.query<{ token: string }>("SELECT token FROM unit_run_owners WHERE unit_run_id=?").get(unitRunId);
        if (currentOwner && currentOwner.token !== ownerToken) return undefined;
        if (existing.session_id !== sessionId || !existing.owner_token) this.db.query("UPDATE machine_attempts SET session_id=?, owner_token=? WHERE unit_run_id=?").run(sessionId, ownerToken, unitRunId);
        this.db.query("INSERT OR IGNORE INTO unit_run_owners (unit_run_id, token, scope, pid) VALUES (?,?,?,?)")
          .run(unitRunId, ownerToken, "machine", typeof process.pid === "number" ? process.pid : 0);
        return this.machineAttemptFromRow({ ...existing, session_id: sessionId, owner_token: ownerToken });
      }
      const attempt = Number(existing?.attempt ?? 0) + 1;
      const ownerToken = `machine:${unitRunId}:${attempt}`;
      const currentOwner = this.db.query<{ token: string }>("SELECT token FROM unit_run_owners WHERE unit_run_id=?").get(unitRunId);
      if (currentOwner && currentOwner.token !== ownerToken) return undefined;
      this.db.query(`
        INSERT INTO machine_attempts (unit_run_id,attempt,session_id,owner_token,status,error,started_at,finished_at)
        VALUES (?,?,?,?,'running',NULL,?,NULL)
        ON CONFLICT(unit_run_id) DO UPDATE SET attempt=excluded.attempt, session_id=excluded.session_id,
          owner_token=excluded.owner_token, status='running', error=NULL, started_at=excluded.started_at, finished_at=NULL
      `).run(unitRunId, attempt, sessionId, ownerToken, timestamp);
      this.db.query("INSERT OR IGNORE INTO unit_run_owners (unit_run_id, token, scope, pid) VALUES (?,?,?,?)")
        .run(unitRunId, ownerToken, "machine", typeof process.pid === "number" ? process.pid : 0);
      return this.machineAttemptFromRow({ unit_run_id: unitRunId, attempt, session_id: sessionId, owner_token: ownerToken, status: "running", error: null, started_at: timestamp, finished_at: null });
    }).immediate();
  }

  finishMachineAttempt(unitRunId: string, attempt: number, status: "completed" | "failed", error?: string): MachineAttempt | undefined {
    const row = this.db.query(`
      UPDATE machine_attempts SET status=?, error=?, finished_at=?
      WHERE unit_run_id=? AND attempt=? AND status='running' RETURNING *
    `).get(status, error ?? null, this.timestamp(), unitRunId, attempt) as any;
    return row ? this.machineAttemptFromRow(row) : undefined;
  }

  getMachineAttempt(unitRunId: string): MachineAttempt | undefined {
    const row = this.db.query("SELECT * FROM machine_attempts WHERE unit_run_id=?").get(unitRunId) as any;
    return row ? this.machineAttemptFromRow(row) : undefined;
  }

  /**
   * Machines are business units with a durable lifecycle. Kernel-bound
   * machines are claimed as running and dispatched to the legacy LineRunner.
   * Unbound machines retain the deterministic source activation used by the
   * portable business path.
   */
  private activateReadyMachineRuns(run: BusinessRun, unitRuns: UnitRun[], predecessors: Map<string, UnitRun[]>): void {
    const timestamp = this.timestamp();
    const activate = this.db.query("UPDATE unit_runs SET status=?, session_id=?, error=?, updated_at=? WHERE id=? AND status='queued'");
    const claimLegacy = this.db.query("UPDATE unit_runs SET status='running', session_id=?, error=NULL, updated_at=? WHERE id=? AND status='queued'");
    const updateUnit = this.db.query("UPDATE business_units SET status=?, updated_at=? WHERE id=?");
    const kernel = run.metadata.kernel;
    const legacy = Boolean(kernel && typeof kernel === "object" && !Array.isArray(kernel) && (kernel as Record<string, unknown>).execution === "legacy");
    const graph = this.runGraph(run);
    const graphUnits = graph.nodes ?? new Set(graph.edges.flatMap(({ source, target }) => [source, target]));
    for (const item of unitRuns) {
      if (item.status !== "queued") continue;
      const unit = this.getBusinessUnit(item.unitId);
      if (!unit || unit.kind !== "machine") continue;
      const required = predecessors.get(item.unitId) ?? [];
      if (required.some((predecessor) => predecessor.status !== "completed")) continue;
      const error = unit.projectId ? undefined : "machine-project-unavailable";
      if (!legacy && !graphUnits.has(unit.id)) {
        activate.run("completed", null, null, timestamp, item.id);
        updateUnit.run("ready", timestamp, unit.id);
        item.status = "completed";
        item.sessionId = undefined;
        item.error = undefined;
        item.updatedAt = timestamp;
        continue;
      }
      if (legacy && !error) {
        claimLegacy.run(`legacy:${item.id}`, timestamp, item.id);
        updateUnit.run("running", timestamp, unit.id);
        item.status = "running";
        item.sessionId = `legacy:${item.id}`;
        item.error = undefined;
      } else if (!error) {
        const status: UnitRun["status"] = "running";
        activate.run(status, `machine:${item.id}`, null, timestamp, item.id);
        updateUnit.run("running", timestamp, unit.id);
        item.status = status;
        item.sessionId = `machine:${item.id}`;
        item.error = undefined;
      } else {
        activate.run("blocked", null, error, timestamp, item.id);
        updateUnit.run("blocked", timestamp, unit.id);
        item.status = "blocked";
        item.sessionId = undefined;
        item.error = error;
      }
      item.updatedAt = timestamp;
    }
    if (unitRuns.some((item) => item.status === "running" || item.status === "completed" || item.status === "blocked")) {
      this.db.query("UPDATE business_runs SET status='running', updated_at=? WHERE id=? AND status='queued'").run(timestamp, run.id);
    }
    if (unitRuns.some((item) => item.status === "completed" || item.status === "blocked")) {
      this.db.query("UPDATE business_runs SET updated_at=? WHERE id=?").run(timestamp, run.id);
    }
  }

  private listReadySuccessorUnitRuns(completed: UnitRun): UnitRun[] {
    // Keep the indexed outgoing edges as the outer loop, even when many units are queued.
    const rows = this.db.query(`
      SELECT successor.* FROM business_run_edges AS outbound
      CROSS JOIN unit_runs AS successor ON successor.run_id=outbound.run_id AND successor.unit_id=outbound.target_id
      WHERE outbound.run_id=? AND outbound.source_id=? AND successor.status='queued'
        AND NOT EXISTS (
          SELECT 1 FROM business_run_edges AS inbound
          JOIN unit_runs AS predecessor ON predecessor.run_id=inbound.run_id AND predecessor.unit_id=inbound.source_id
          WHERE inbound.run_id=outbound.run_id AND inbound.target_id=outbound.target_id AND predecessor.status!='completed'
        )
      ORDER BY successor.created_at, successor.id
    `).all(completed.runId, completed.unitId);
    const predecessors = this.db.query<{ outputs: string }>(`
      SELECT predecessor.outputs FROM business_run_edges AS edge
      JOIN unit_runs AS predecessor ON predecessor.run_id=edge.run_id AND predecessor.unit_id=edge.source_id
      WHERE edge.run_id=? AND edge.target_id=? ORDER BY edge.position
    `);
    return rows.map((row) => {
      const successor = this.unitRunFromRow(row);
      const outputs = predecessors.all(successor.runId, successor.unitId).flatMap(({ outputs }) => parseList(outputs));
      this.mergeUnitRunInputs(successor, outputs);
      return successor;
    });
  }

  private mergeUnitRunInputs(unit: UnitRun, outputs: string[]): void {
    const inputs = [...new Set([...unit.inputs, ...outputs])];
    if (inputs.length === unit.inputs.length && inputs.every((value, index) => value === unit.inputs[index])) return;
    this.db.query("UPDATE unit_runs SET inputs=?, updated_at=? WHERE id=? AND status='queued'").run(json(inputs), this.timestamp(), unit.id);
    unit.inputs = inputs;
  }

  /** Propagate terminal failures through the DAG so a run cannot stay running forever. */
  private reconcileBlockedUnitRuns(run: BusinessRun, unitRuns: UnitRun[], successors: Map<string, UnitRun[]>): void {
    const blocked = unitRuns.filter((item) => item.status === "failed" || item.status === "blocked");
    const timestamp = this.timestamp();
    const update = this.db.query("UPDATE unit_runs SET status='blocked', error=?, updated_at=? WHERE id=? AND status='queued'");
    for (let index = 0; index < blocked.length; index += 1) {
      for (const item of successors.get(blocked[index].unitId) ?? []) {
        if (item.status !== "queued") continue;
        update.run("predecessor-failed", timestamp, item.id);
        item.status = "blocked";
        item.error = "predecessor-failed";
        item.updatedAt = timestamp;
        blocked.push(item);
      }
    }
    const finalStatuses = unitRuns.map(({ status }) => status);
    const nextStatus: BusinessRun["status"] = finalStatuses.some((status) => status === "failed" || status === "blocked")
      ? "failed"
      : finalStatuses.length > 0 && finalStatuses.every((status) => status === "completed")
        ? "completed"
        : finalStatuses.some((status) => status === "running" || status === "completed" || status === "waiting_resources")
          ? "running"
          : finalStatuses.length ? "queued" : "failed";
    if (nextStatus !== run.status) this.db.query("UPDATE business_runs SET status=?, updated_at=? WHERE id=?").run(nextStatus, timestamp, run.id);
  }

  private runEdges(run: BusinessRun): BusinessRunEdge[] {
    return this.runGraph(run).edges;
  }

  private indexRunEdges(runId: string, edges: BusinessRunEdge[]): void {
    const insert = this.db.query("INSERT OR IGNORE INTO business_run_edges (run_id,source_id,target_id,position) VALUES (?,?,?,?)");
    for (const [position, { source, target }] of edges.entries()) insert.run(runId, source, target, position);
    this.db.query("UPDATE business_run_graphs SET edges_indexed=1 WHERE run_id=?").run(runId);
  }

  claimUnitRun(runId: string, unitId: string, sessionId: string, owner?: UnitRunOwner): UnitRun | undefined {
    const timestamp = this.timestamp();
    return this.db.transaction(() => {
      const row = this.db.query("UPDATE unit_runs SET status='running', session_id=?, updated_at=? WHERE run_id=? AND unit_id=? AND status='queued' RETURNING *")
        .get(sessionId, timestamp, runId, unitId);
      if (!row) return undefined;
      if (owner) {
        this.db.query("INSERT INTO unit_run_owners (unit_run_id, token, scope, pid) VALUES (?,?,?,?)")
          .run(row.id, owner.token, owner.scope, owner.pid);
      }
      this.db.query("UPDATE business_units SET status='running', updated_at=? WHERE id=?").run(timestamp, unitId);
      this.db.query("UPDATE business_runs SET status='running', updated_at=? WHERE id=? AND status='queued'").run(timestamp, runId);
      return this.unitRunFromRow(row);
    }).immediate();
  }

  noteUnitSession(id: string, sessionId: string, ownerToken: string): void {
    this.db.query(`UPDATE unit_runs SET session_id=? WHERE id=? AND status='running'
      AND EXISTS (SELECT 1 FROM unit_run_owners WHERE unit_run_id=unit_runs.id AND token=?)`)
      .run(sessionId, id, ownerToken);
  }

  runningUnitRunOwners(scope: string): Array<UnitRunOwner & { unitRunId: string }> {
    return this.db.query<UnitRunOwner & { unitRunId: string }>(`
      SELECT owner.unit_run_id AS unitRunId, owner.token, owner.scope, owner.pid
      FROM unit_run_owners AS owner JOIN unit_runs AS unit ON unit.id=owner.unit_run_id
      WHERE owner.scope=? AND unit.status='running'
      ORDER BY owner.unit_run_id
    `).all(scope);
  }

  ownsRunningUnitRun(id: string, token: string): boolean {
    return Boolean(this.db.query(`
      SELECT 1 FROM unit_runs AS unit JOIN unit_run_owners AS owner ON owner.unit_run_id=unit.id
      WHERE unit.id=? AND unit.status='running' AND owner.token=?
    `).get(id, token));
  }

  completeUnitRun(id: string, status: UnitRun["status"], outputs?: string[], error?: string, ownerToken?: string): UnitRun | undefined {
    return this.db.transaction(() => this.writeUnitRunOutcome(id, status, outputs ?? [], error, ownerToken)).immediate();
  }

  interruptUnitRun(id: string, ownerToken: string): Event | undefined {
    return this.db.transaction(() => {
      const interrupted = this.writeUnitRunOutcome(id, "failed", undefined, "CONTROL_PLANE_RESTART", ownerToken);
      if (interrupted) return this.appendEvent("operator.run.failed", interrupted);
    }).immediate();
  }

  private writeUnitRunOutcome(id: string, status: UnitRun["status"], outputs: string[] | undefined, error?: string, token?: string): UnitRun | undefined {
    const timestamp = this.timestamp();
    const row = this.db.query(`
      UPDATE unit_runs SET status=?, outputs=coalesce(?, outputs), error=?, updated_at=? WHERE id=? AND (
        (? IS NULL AND status IN ('queued','running') AND NOT EXISTS (SELECT 1 FROM unit_run_owners WHERE unit_run_id=unit_runs.id))
        OR (status='running' AND EXISTS (SELECT 1 FROM unit_run_owners WHERE unit_run_id=unit_runs.id AND token=?))
      ) RETURNING *
    `).get(status, outputs === undefined ? null : json(outputs), error ?? null, timestamp, id, token ?? null, token ?? null);
    if (!row) return undefined;
    this.db.query("DELETE FROM unit_run_owners WHERE unit_run_id=?").run(id);
    const unit = this.unitRunFromRow(row);
    if (status === "completed" && this.getBusinessUnit(unit.unitId)?.kind === "operator") {
      const sourceMachines = this.db.query<{ id: string }>(`
        UPDATE unit_runs SET status='completed', session_id=NULL, error=NULL, updated_at=?
        WHERE run_id=? AND status='running' AND session_id LIKE 'machine:%' AND id<>? RETURNING id
      `).all(timestamp, unit.runId, id);
      if (sourceMachines.length) this.db.query(`
        UPDATE business_units SET status='ready', updated_at=?
        WHERE id IN (SELECT unit_id FROM unit_runs WHERE id IN (SELECT value FROM json_each(?)))
      `).run(timestamp, json(sourceMachines.map(({ id }) => id)));
    }
    this.db.query(`
      UPDATE business_runs SET status = CASE
        WHEN EXISTS (SELECT 1 FROM unit_runs WHERE run_id = ? AND status IN ('failed', 'blocked')) THEN 'failed'
        WHEN EXISTS (SELECT 1 FROM unit_runs INDEXED BY unit_runs_unfinished WHERE run_id = ? AND status != 'completed') THEN 'running'
        ELSE 'completed'
      END, updated_at = ? WHERE id = ?
    `).run(unit.runId, unit.runId, timestamp, unit.runId);
    this.db.query("UPDATE business_units SET status=?, updated_at=? WHERE id=?")
      .run(status === "completed" ? "ready" : status, timestamp, unit.unitId);
    return unit;
  }

  private businessGraph(businessSystemId: string): { edges: BusinessRunEdge[]; nodes?: Set<string> } {
    const row = this.db.query("SELECT metadata FROM business_systems WHERE id=?").get(businessSystemId) as { metadata?: string } | undefined;
    const metadata = parseJson(row?.metadata);
    const graph = metadata.graph && typeof metadata.graph === "object" ? metadata.graph as { nodes?: unknown; edges?: unknown } : {};
    const units = this.listBusinessUnits(businessSystemId).filter((unit) => unit.status !== "archived");
    const unitIds = new Set(units.map((unit) => unit.id));
    const aliases = new Map<string, string>();
    for (const unit of units) if ((unit.kind === "operator" || unit.kind === "machine") && !aliases.has(unit.kind)) aliases.set(unit.kind, unit.id);
    const authored = Array.isArray(graph.edges) ? graph.edges.flatMap((edge): BusinessRunEdge[] => {
      if (!edge || typeof edge !== "object" || typeof (edge as any).source !== "string" || typeof (edge as any).target !== "string") return [];
      const source = aliases.get((edge as any).source) ?? (edge as any).source;
      const target = aliases.get((edge as any).target) ?? (edge as any).target;
      return unitIds.has(source) && unitIds.has(target) ? [{ source, target }] : [];
    }) : [];
    const authoredNodes = Array.isArray(graph.nodes)
      ? new Set(graph.nodes.flatMap((node): string[] => {
        if (!node || typeof node !== "object" || typeof (node as any).id !== "string") return [];
        const id = aliases.get((node as any).id) ?? (node as any).id;
        return unitIds.has(id) ? [id] : [];
      }))
      : undefined;
    if (Array.isArray(graph.edges)) return { edges: authored, ...(authoredNodes ? { nodes: authoredNodes } : {}) };
    const fallback = units.filter((unit) => unit.kind === "operator").flatMap((operator) => {
      const machine = units.find((candidate) => candidate.kind === "machine" && candidate.projectId === operator.projectId);
      return machine ? [{ source: machine.id, target: operator.id }] : [];
    });
    return { edges: fallback };
  }

  private runGraph(run: BusinessRun): { edges: BusinessRunEdge[]; nodes?: Set<string> } {
    const stored = this.db.query<{ edges: string; nodes: string | null; edges_indexed: number }>("SELECT edges, nodes, edges_indexed FROM business_run_graphs WHERE run_id=?").get(run.id);
    const graph = stored
      ? { edges: JSON.parse(stored.edges) as BusinessRunEdge[], ...(stored.nodes ? { nodes: new Set(parseList(stored.nodes)) } : {}) }
      : this.businessGraph(run.businessSystemId);
    if (!stored) this.db.query("INSERT INTO business_run_graphs (run_id,edges,nodes) VALUES (?,?,?)").run(run.id, json(graph.edges), graph.nodes ? json([...graph.nodes]) : null);
    if (!stored?.edges_indexed) this.indexRunEdges(run.id, graph.edges);
    return graph;
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

  private machineAttemptFromRow(row: any): MachineAttempt {
    return {
      unitRunId: row.unit_run_id,
      attempt: Number(row.attempt),
      sessionId: row.session_id,
      ownerToken: row.owner_token ?? undefined,
      status: row.status,
      error: row.error ?? undefined,
      startedAt: row.started_at,
      finishedAt: row.finished_at ?? undefined,
    };
  }

  private projectFromRow(row: any, sourceRows?: any[], secretRows?: any[]): Project {
    const sources = (sourceRows ?? this.db.query("SELECT * FROM project_sources WHERE project_id = ? ORDER BY created_at, id").all(row.id)).map((item: any) => ({
      id: item.id,
      projectId: item.project_id,
      kind: item.kind,
      repoUrl: item.repo_url ?? undefined,
      fileRef: item.file_ref ?? undefined,
      localFolder: item.local_folder ?? undefined,
      metadata: redactStoredValue(parseJson(item.metadata)) as Record<string, unknown>,
      createdAt: item.created_at,
    }));
    const secretRequests = (secretRows ?? this.db.query("SELECT * FROM secret_requests WHERE project_id = ? ORDER BY created_at, id").all(row.id)).map((item: any) => ({
      id: item.id,
      projectId: item.project_id,
      name: item.name,
      vaultRef: item.vault_ref,
      status: item.status,
      metadata: redactStoredValue(parseJson(item.metadata)) as Record<string, unknown>,
      createdAt: item.created_at,
      updatedAt: item.updated_at,
    }));
    return {
      id: row.id,
      businessSystemId: row.business_system_id ?? undefined,
      name: row.name,
      metadata: redactStoredValue(parseJson(row.metadata)) as Record<string, unknown>,
      createdAt: row.created_at,
      updatedAt: row.updated_at,
      sources,
      secretRequests,
    };
  }

  addArtifact(input: Omit<Artifact, "id" | "createdAt">, id = this.uuid()): Artifact {
    const metadata = redactStoredValue({
      ...(input.metadata ?? {}),
      schema: typeof input.metadata?.schema === "string" ? input.metadata.schema : BUSINESS_RECORD_SCHEMA,
      record_type: input.recordType ?? (input.metadata?.record_type === "material" ? "material" : "artifact"),
      title: input.title ?? input.metadata?.title,
      description: input.description ?? input.metadata?.description,
      status: input.status ?? input.metadata?.status,
      producer: input.producer ?? input.metadata?.producer,
      summary: input.summary ?? input.metadata?.summary,
      purpose: input.purpose ?? input.metadata?.purpose,
      files: input.files ?? input.metadata?.files,
      inputs: input.inputs ?? input.metadata?.inputs,
      outputs: input.outputs ?? input.metadata?.outputs,
      lineage: input.lineage ?? input.metadata?.lineage,
    }) as Record<string, unknown>;
    const value: Artifact = {
      ...input,
      id,
      kind: typeof input.kind === "string" ? input.kind.replace(secretText, (match) => `${match.split(/[:=]/, 1)[0]}=[REDACTED]`) : input.kind,
      ref: typeof input.ref === "string" ? input.ref.replace(secretText, (match) => `${match.split(/[:=]/, 1)[0]}=[REDACTED]`) : input.ref,
      metadata,
      createdAt: this.timestamp(),
      recordType: metadata.record_type === "material" ? "material" : "artifact",
      title: typeof metadata.title === "string" ? metadata.title : undefined,
      description: typeof metadata.description === "string" ? metadata.description : undefined,
      summary: typeof metadata.summary === "string" ? metadata.summary : undefined,
      purpose: typeof metadata.purpose === "string" ? metadata.purpose : undefined,
      status: typeof metadata.status === "string" ? metadata.status : undefined,
      producer: metadata.producer && typeof metadata.producer === "object" ? metadata.producer as Artifact["producer"] : undefined,
      files: Array.isArray(metadata.files) ? metadata.files as Artifact["files"] : undefined,
      inputs: Array.isArray(metadata.inputs) ? metadata.inputs as string[] : undefined,
      outputs: Array.isArray(metadata.outputs) ? metadata.outputs as string[] : undefined,
      lineage: Array.isArray(metadata.lineage) ? metadata.lineage as string[] : undefined,
    };
    this.db.query("INSERT INTO artifacts (id,project_id,source_id,kind,ref,metadata,created_at) VALUES (?,?,?,?,?,?,?)")
      .run(value.id, value.projectId, value.sourceId ?? null, value.kind, value.ref, json(value.metadata), value.createdAt);
    return value;
  }

  listArtifacts(projectId: string): Artifact[] {
    return this.db.query("SELECT * FROM artifacts WHERE project_id = ? ORDER BY created_at, id").all(projectId).map((row: any) => this.artifactFromRow(row));
  }

  updateArtifactMetadata(id: string, patch: Record<string, unknown>): Artifact | undefined {
    return this.db.transaction(() => {
      const row = this.db.query("SELECT * FROM artifacts WHERE id=?").get(id) as any;
      if (!row) return undefined;
      const metadata = redactStoredValue({ ...parseJson(row.metadata), ...patch }) as Record<string, unknown>;
      this.db.query("UPDATE artifacts SET metadata=? WHERE id=?").run(json(metadata), id);
      return this.artifactFromRow(this.db.query("SELECT * FROM artifacts WHERE id=?").get(id));
    }).immediate();
  }

  registerArtifactFiles(artifactId: string, files: ReadonlyArray<Omit<ArtifactFileGrant, "artifactId">>): void {
    const insert = this.db.query("INSERT INTO artifact_file_grants (artifact_id,position,digest,size) VALUES (?,?,?,?)");
    for (const { position, digest, size } of files) insert.run(artifactId, position, digest, size);
  }

  artifactFileGrants(businessSystemId: string | null, artifactIds: readonly string[]): ArtifactFileGrant[] {
    if (!artifactIds.length) return [];
    return this.db.query<ArtifactFileGrant>(`
      SELECT grant.artifact_id AS artifactId, grant.position, grant.digest, grant.size
      FROM artifact_file_grants AS grant
      JOIN artifacts AS artifact ON artifact.id=grant.artifact_id
      JOIN projects AS project ON project.id=artifact.project_id
      WHERE grant.artifact_id IN (SELECT value FROM json_each(?))
      ${businessSystemId === null ? "" : "AND project.business_system_id=?"}
      ORDER BY grant.artifact_id, grant.position
    `).all(json(artifactIds), ...(businessSystemId === null ? [] : [businessSystemId]));
  }

  projectArtifactFile(projectId: string, artifactId: string, digest: string): { size: number; path?: string } | undefined {
    const row = this.db.query<{ size: number; path: unknown }>(`
      SELECT grant.size, json_extract(artifact.metadata, '$.files[' || grant.position || '].path') AS path
      FROM artifact_file_grants AS grant
      JOIN artifacts AS artifact ON artifact.id=grant.artifact_id
      WHERE artifact.project_id=? AND grant.artifact_id=? AND grant.digest=?
      ORDER BY grant.position LIMIT 1
    `).get(projectId, artifactId, digest);
    if (row) return { size: row.size, path: typeof row.path === "string" ? row.path : undefined };
  }

  listAllArtifacts(limit?: number, older?: BusinessAssetCursor): Artifact[] {
    const rows = older
      ? this.db.query(`
        SELECT * FROM artifacts WHERE (created_at, id) < (?, ?)
        ORDER BY created_at DESC, id DESC LIMIT ?
      `).all(older.createdAt, older.id, limit ?? -1).reverse()
      : limit === undefined
      ? this.db.query("SELECT * FROM artifacts ORDER BY created_at, id").all()
      : this.db.query("SELECT * FROM artifacts ORDER BY created_at DESC, id DESC LIMIT ?").all(limit).reverse();
    return rows.map((row) => this.artifactFromRow(row));
  }

  assetPage(limit: number, older?: AssetCursor): { business: Artifact[]; legacy: Artifact[]; total: number; older: AssetCursor | null } {
    const source = this.legacyAssetSource();
    const business = older?.source === "legacy" ? [] : this.listAllArtifacts(limit + 1, older);
    const legacy = source ? this.legacyAssetRows(source, limit + 1 - business.length, older?.source === "legacy" ? older : undefined) : [];
    const total = this.db.query<{ count: number }>("SELECT COUNT(*) AS count FROM artifacts").get()!.count
      + (source?.query<{ count: number }>("SELECT COUNT(*) AS count FROM asset_records").get()?.count ?? 0);
    let next: AssetCursor | null = null;
    if (business.length + legacy.length > limit) {
      // Each source is already in display order. The extra row precedes the page.
      if (legacy.length > 0) legacy.shift();
      else business.shift();
      const firstLegacy = legacy[0];
      const firstBusiness = business[0];
      next = firstLegacy
        ? { source: "legacy", createdAt: firstLegacy.created_at, rowId: firstLegacy.row_id }
        : { source: "business", createdAt: firstBusiness.createdAt, id: firstBusiness.id };
    }
    return { business, legacy: legacy.map((row) => this.legacyAssetFromRow(row)), total, older: next };
  }

  listBusinessSystemArtifacts(businessSystemId: string): Artifact[] {
    const rows = this.db.query("SELECT artifacts.* FROM artifacts JOIN projects ON projects.id = artifacts.project_id WHERE projects.business_system_id = ? ORDER BY artifacts.created_at, artifacts.id").all(businessSystemId);
    return rows.map((row: any) => this.artifactFromRow(row));
  }

  resolveBusinessMaterialRefs(businessSystemId: string, refs: readonly string[]): Array<{ ref: string; record?: Artifact }> {
    const byId = this.db.query(`
      SELECT artifact.* FROM artifacts AS artifact JOIN projects AS project ON project.id = artifact.project_id
      WHERE artifact.id = ? AND project.business_system_id = ?
    `);
    const byRef = this.db.query(`
      SELECT artifact.* FROM artifacts AS artifact JOIN projects AS project ON project.id = artifact.project_id
      WHERE artifact.ref = ? AND project.business_system_id = ? ORDER BY artifact.created_at DESC, artifact.id DESC LIMIT 1
    `);
    return [...new Set(refs)].map((ref) => {
      const row = byId.get(ref.replace(/^(?:artifact|record|material):\/\//, ""), businessSystemId) ?? byRef.get(ref, businessSystemId);
      return { ref, record: row ? this.artifactFromRow(row) : undefined };
    });
  }

  createBusinessConversion(input: Omit<BusinessConversion, "id" | "createdAt" | "updatedAt">): BusinessConversion {
    const safe = redactStoredValue(input) as Omit<BusinessConversion, "id" | "createdAt" | "updatedAt">;
    const invalid = this.invalidBusinessInputRefs(safe.businessSystemId, [...safe.inputs, ...safe.outputs]);
    if (invalid.length) throw new Error("conversion input references are outside the business system");
    return this.db.transaction(() => {
      const inputJson = json(safe.inputs);
      const outputJson = json(safe.outputs);
      const existing = this.db.query<any>(`
        SELECT * FROM business_conversions
        WHERE business_system_id=? AND kind=? AND summary=? AND inputs=? AND outputs=?
        ORDER BY created_at DESC, id DESC LIMIT 1
      `).get(input.businessSystemId, input.kind, input.summary, inputJson, outputJson);
      if (existing) {
        const status = mergedConversionStatus(existing.status, safe.status);
        const producer = safe.producer ?? (existing.producer ? parseJson(existing.producer) as BusinessConversion["producer"] : undefined);
        const metadata = { ...parseJson(existing.metadata), ...(safe.metadata ?? {}) };
        const updatedAt = this.timestamp();
        this.db.query("UPDATE business_conversions SET status=?, producer=?, metadata=?, updated_at=? WHERE id=?")
          .run(status, producer ? json(producer) : null, json(metadata), updatedAt, existing.id);
        return {
          id: existing.id, businessSystemId: existing.business_system_id, kind: existing.kind,
          summary: existing.summary, status, inputs: parseList(existing.inputs), outputs: parseList(existing.outputs),
          producer, metadata, createdAt: existing.created_at, updatedAt,
        };
      }
      const timestamp = this.timestamp();
      const value: BusinessConversion = { ...safe, id: this.uuid(), createdAt: timestamp, updatedAt: timestamp };
      this.db.query("INSERT INTO business_conversions (id,business_system_id,kind,summary,status,inputs,outputs,producer,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
        .run(value.id, value.businessSystemId, value.kind, value.summary, value.status, inputJson, outputJson, value.producer ? json(value.producer) : null, json(value.metadata), timestamp, timestamp);
      return value;
    }).immediate();
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

  hasBusinessConversion(businessSystemId: string, kind: string, input: string, output: string): boolean {
    return Boolean(this.db.query(`
      SELECT 1 FROM business_conversions
      WHERE business_system_id=? AND kind=?
        AND json_valid(inputs) AND json_valid(outputs)
        AND json_extract(inputs, '$[0]')=? AND json_extract(outputs, '$[0]')=?
      LIMIT 1
    `).get(businessSystemId, kind, input, output));
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
    return this.db.transaction(() => {
      const existing = this.db.query<{ id: string }>("SELECT id FROM secret_requests WHERE project_id=? AND name=? AND status IN ('requested','available') ORDER BY status='available' DESC, created_at DESC, id DESC LIMIT 1")
        .get(input.projectId, input.name);
      if (existing) return this.getSecretRequest(existing.id)!;
      const timestamp = this.timestamp();
      const metadata = redactStoredValue(input.metadata ?? {}) as Record<string, unknown>;
      const value: SecretRequest = { id: this.uuid(), projectId: input.projectId, name: input.name, vaultRef: input.vaultRef,
        status: input.status ?? "requested", metadata, createdAt: timestamp, updatedAt: timestamp };
      this.db.query("INSERT INTO secret_requests (id,project_id,name,vault_ref,status,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)")
        .run(value.id, value.projectId, value.name, value.vaultRef, value.status, json(value.metadata), timestamp, timestamp);
      this.recordSecretAudit(value.id, "requested", "control", undefined, { name: value.name });
      return value;
    }).immediate();
  }

  createSourceToolRequest(input: { unitRunId: string; projectId: string; tool: string; source: string }): SecretRequest {
    const normalizedTool = input.tool.trim().toLowerCase().replace(/[^a-z0-9_.-]+/g, "-");
    if (!normalizedTool) throw new Error("source-tool-invalid");
    const request = this.createSecretRequest({
      projectId: input.projectId,
      name: `MAO_SOURCE_TOOL_${normalizedTool.replace(/[^A-Z0-9]+/gi, "_").toUpperCase()}`,
      vaultRef: `vault://resource/source-tool/${this.uuid()}`,
      metadata: { kind: "source-tool", tool: normalizedTool, source: input.source, purpose: `项目需要 ${normalizedTool} 准备来源` },
    });
    this.linkUnitSecret(input.unitRunId, request.id);
    return request;
  }

  linkUnitSecret(unitRunId: string, requestId: string): void {
    this.db.query("INSERT OR IGNORE INTO unit_secret_requests (unit_run_id,request_id) VALUES (?,?)").run(unitRunId, requestId);
  }

  unitSecretRequests(unitRunId: string): SecretRequest[] {
    const ids = this.db.query<{ request_id: string }>("SELECT request_id FROM unit_secret_requests WHERE unit_run_id=? ORDER BY request_id").all(unitRunId);
    return ids.flatMap(({ request_id }) => {
      const request = this.getSecretRequest(request_id);
      if (request?.status === "available") this.recordSecretAudit(request.id, "used", "operator", unitRunId);
      return request ? [request] : [];
    });
  }

  resumeResourceRuns(requestId?: string): string[] {
    const candidates = requestId !== undefined
      ? "id IN (SELECT unit_run_id FROM unit_secret_requests WHERE request_id=?)"
      : `EXISTS (SELECT 1 FROM unit_secret_requests WHERE unit_run_id=unit_runs.id)
         AND EXISTS (
           SELECT 1 FROM business_runs AS run WHERE run.id=unit_runs.run_id
             AND run.status IN ('queued','running','failed')
             AND (
               EXISTS (SELECT 1 FROM business_run_dispatch WHERE run_id=run.id)
               OR EXISTS (SELECT 1 FROM unit_runs AS prior WHERE prior.run_id=run.id AND prior.status='completed')
             )
         )`;
    const rows = this.db.query<{ run_id: string }>(`
      UPDATE unit_runs SET status='queued', session_id=NULL, error=NULL, updated_at=?
      WHERE status='waiting_resources'
        AND ${candidates}
        AND NOT EXISTS (
          SELECT 1 FROM unit_secret_requests AS link
          LEFT JOIN secret_requests AS request
            ON request.id=link.request_id
            AND request.project_id=(SELECT project_id FROM business_units WHERE id=unit_runs.unit_id AND kind='operator')
          WHERE link.unit_run_id=unit_runs.id AND (request.status IS NULL OR request.status!='available')
        )
      RETURNING run_id
    `).all(this.timestamp(), ...(requestId === undefined ? [] : [requestId]));
    return [...new Set(rows.map(({ run_id }) => run_id))];
  }

  private foremanEnvelopeFromRow(row: any): ForemanEnvelope {
    return {
      id: row.id,
      messageId: row.message_id ?? row.messageId,
      text: row.text,
      status: row.status,
      createdAt: row.created_at ?? row.createdAt,
      queuedAt: row.queued_at ?? row.queuedAt ?? row.created_at ?? row.createdAt ?? undefined,
      claimedAt: row.claimed_at ?? row.claimedAt ?? undefined,
      startedAt: row.started_at ?? row.startedAt ?? undefined,
      lastEventAt: row.last_event_at ?? row.lastEventAt ?? undefined,
      completedAt: row.completed_at ?? row.completedAt ?? undefined,
      elapsedMs: row.elapsed_ms ?? row.elapsedMs ?? undefined,
      businessStatus: row.business_status ?? row.businessStatus ?? "not_started",
      businessReason: row.business_reason ?? row.businessReason ?? undefined,
    };
  }

  addTalkMessage(input: { role?: string; body: string; metadata?: Record<string, unknown> }): TalkMessage {
    const value: TalkMessage = { id: this.uuid(), role: input.role ?? "user", body: input.body, metadata: input.metadata ?? {}, createdAt: this.timestamp() };
    this.db.query("INSERT INTO business_talk_messages (id,role,body,metadata,created_at) VALUES (?,?,?,?,?)").run(value.id, value.role, value.body, json(value.metadata), value.createdAt);
    return value;
  }

  enqueueForemanTalk(input: Parameters<SqliteStore["addTalkMessage"]>[0]): { message: TalkMessage; envelope: ForemanEnvelope; events: Event[] } {
    return this.db.transaction(() => {
      const message = this.addTalkMessage(input);
      this.bridgeHumanTalk(message.id, message.body);
      const envelope = this.queueForemanMessage(message.id, message.body);
      message.metadata = { ...message.metadata, inbox_id: envelope.id };
      this.db.query("UPDATE business_talk_messages SET metadata=? WHERE id=?").run(json(message.metadata), message.id);
      const events = [
        this.appendEvent("talk.message.created", message),
        this.appendEvent("foreman.message.queued", envelope),
      ];
      return { message, envelope, events };
    }).immediate();
  }

  listTalkMessages(limit?: number): TalkMessage[] {
    const rows = limit === undefined
      ? this.db.query("SELECT * FROM business_talk_messages ORDER BY created_at, id").all()
      : this.db.query("SELECT * FROM business_talk_messages ORDER BY created_at DESC, id DESC LIMIT ?").all(limit).reverse();
    return rows.map((row: any) => ({
      id: row.id, role: row.role, body: row.body, metadata: parseJson(row.metadata), createdAt: row.created_at,
    }));
  }

  talkMessageMetadata(id: string): Record<string, unknown> {
    const row = this.db.query<{ metadata: string }>("SELECT metadata FROM business_talk_messages WHERE id=?").get(id);
    return parseJson(row?.metadata);
  }

  previousForemanTurns(inboxId: string): Array<{ user: string; foreman?: string }> {
    const previous = this.db.query<{ id: string; user: string }>(`
      SELECT prior.id, substr(message.body, 1, 1000) AS user
      FROM foreman_outbox AS prior
      JOIN business_talk_messages AS message ON message.id = prior.message_id
      WHERE prior.rowid < (SELECT rowid FROM foreman_outbox WHERE id = ?)
        AND prior.status = 'done' AND message.role IN ('user','human')
      ORDER BY prior.rowid DESC LIMIT 4
    `).all(inboxId);
    const reply = this.db.query<{ text: string }>(`
      SELECT substr(body, 1, 1000) AS text FROM business_talk_messages
      WHERE role = 'foreman' AND CASE WHEN json_valid(metadata) THEN json_extract(metadata,'$.inbox_id') END = ?
      ORDER BY rowid DESC LIMIT 1
    `);
    return previous.reverse().map(({ id, user }) => ({ user, foreman: reply.get(id)?.text }));
  }

  queueForemanMessage(messageId: string, text: string): ForemanEnvelope {
    const queuedAt = this.timestamp();
    const value: ForemanEnvelope = { id: this.uuid(), messageId, text, status: "queued", createdAt: queuedAt, queuedAt, businessStatus: "not_started" };
    this.db.query("INSERT INTO foreman_outbox (id,message_id,text,status,created_at,queued_at,claimed_at,business_status) VALUES (?,?,?,?,?,?,NULL,?)").run(value.id, value.messageId, value.text, value.status, value.createdAt, value.queuedAt, value.businessStatus);
    return value;
  }

  bridgeHumanTalk(messageId: string, text: string): void {
    if (!this.bridgeLegacy || this.legacy || !this.hasLegacyTable("talk_messages") || !this.hasLegacyTable("agent_messages")) return;
    const timestamp = this.timestamp();
    const mailboxId = `bun-am-${this.uuid()}`;
    this.db.query("INSERT OR IGNORE INTO talk_messages (id,role,kind,by_name,body,reply_state,in_reply_to,card_type,ref_kind,ref_id,card,mailbox_id,created_at) VALUES (?,?,?,?,?,'open',NULL,NULL,NULL,NULL,'{}',?,?)")
      .run(messageId, "human", "text", "bun", text, mailboxId, timestamp);
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
    return this.db.query("SELECT * FROM foreman_outbox WHERE status IN ('queued','claimed') ORDER BY rowid LIMIT ?").all(bounded).map((row: any) => this.foremanEnvelopeFromRow(row));
  }

  listQueuedForemanMessageIds(): string[] {
    return this.db.query<{ id: string }>(
      "SELECT id FROM foreman_outbox WHERE status='queued' ORDER BY rowid",
    ).all().map(({ id }) => id);
  }

  getForemanMessage(id: string): ForemanEnvelope | undefined {
    const row = this.db.query("SELECT * FROM foreman_outbox WHERE id=?").get(id);
    return row ? this.foremanEnvelopeFromRow(row) : undefined;
  }

  listForemanTurns(messageIds: readonly string[]): ForemanEnvelope[] {
    if (!messageIds.length) return [];
    return this.db.query<any>(`
      SELECT id, message_id, text, status, created_at, queued_at, claimed_at, started_at, last_event_at, completed_at, elapsed_ms, business_status, business_reason
      FROM foreman_outbox
      WHERE message_id IN (SELECT value FROM json_each(?)) ORDER BY rowid
    `).all(json(messageIds)).map((row: any) => this.foremanEnvelopeFromRow(row));
  }

  claimForemanMessage(id: string): ForemanEnvelope | undefined {
    const claimedAt = this.timestamp();
    const row = this.db.query("UPDATE foreman_outbox SET status='claimed', claimed_at=?, started_at=COALESCE(started_at,?), last_event_at=?, business_status='in_progress', business_reason=NULL WHERE id=? AND status='queued' RETURNING *").get(claimedAt, claimedAt, claimedAt, id) as any;
    if (!row) return undefined;
    return this.foremanEnvelopeFromRow(row);
  }

  touchForemanMessage(id: string): void {
    this.db.query("UPDATE foreman_outbox SET last_event_at=? WHERE id=? AND status='claimed'").run(this.timestamp(), id);
  }

  setForemanBusinessStatus(id: string, status: ForemanEnvelope["businessStatus"], reason?: string): void {
    this.db.query("UPDATE foreman_outbox SET business_status=?, business_reason=? WHERE id=?").run(status ?? "not_started", reason ?? null, id);
  }

  foremanSession(profileId: string): string | undefined {
    return this.db.query<{ session_id: string }>("SELECT session_id FROM business_foreman_sessions WHERE profile_id=?").get(profileId)?.session_id;
  }

  saveForemanSession(profileId: string, sessionId: string): void {
    this.db.query("INSERT INTO business_foreman_sessions (profile_id,session_id) VALUES (?,?) ON CONFLICT (profile_id) DO UPDATE SET session_id=excluded.session_id")
      .run(profileId, sessionId);
  }

  clearForemanSession(profileId: string, sessionId?: string): void {
    if (sessionId === undefined) this.db.query("DELETE FROM business_foreman_sessions WHERE profile_id=?").run(profileId);
    else this.db.query("DELETE FROM business_foreman_sessions WHERE profile_id=? AND session_id=?").run(profileId, sessionId);
  }

  finishForemanTurn(id: string, result: {
    reply?: Pick<TalkMessage, "body" | "metadata">;
    session?: { profileId: string; sessionId: string };
    businessStatus?: ForemanEnvelope["businessStatus"];
    businessReason?: string;
  }): Event[] {
    return this.db.transaction(() => {
      const completedAt = this.timestamp();
      const current = this.db.query<{ started_at: string | null; queued_at: string | null; created_at: string }>("SELECT started_at,queued_at,created_at FROM foreman_outbox WHERE id=? AND status='claimed'").get(id);
      if (!current) return [];
      const elapsedMs = elapsedMilliseconds(current.started_at ?? current.queued_at ?? current.created_at, completedAt);
      const completed = this.db.query("UPDATE foreman_outbox SET status='done', completed_at=?, last_event_at=?, elapsed_ms=?, business_status=?, business_reason=? WHERE id=? AND status='claimed' RETURNING id").get(completedAt, completedAt, elapsedMs, result.businessStatus ?? "no_change", result.businessReason ?? null, id);
      if (!completed) return [];
      const events: Event[] = [];
      if (result.session) this.saveForemanSession(result.session.profileId, result.session.sessionId);
      if (result.reply) {
        const message = this.addTalkMessage({ role: "foreman", body: result.reply.body, metadata: { ...result.reply.metadata, inbox_id: id } });
        events.push(this.appendEvent("talk.foreman.replied", { ...message, inboxId: id }));
      }
      events.push(this.appendEvent("foreman.message.done", this.getForemanMessage(id)!));
      return events;
    }).immediate();
  }

  cancelForemanTurn(id: string, expected: "queued" | "claimed", resume?: { profileId: string; sessionId: string }) {
    return this.db.transaction(() => {
      const completedAt = this.timestamp();
      const row = this.db.query<Pick<ForemanEnvelope, "id" | "messageId" | "status">>(`
        UPDATE foreman_outbox SET status='cancelled', completed_at=?, last_event_at=?, business_status='failed', business_reason='cancelled',
          elapsed_ms=CAST((julianday(?) - julianday(COALESCE(started_at,queued_at,created_at))) * 86400000 AS INTEGER)
        WHERE id=? AND status=? RETURNING id, message_id AS messageId, status
      `).get(completedAt, completedAt, completedAt, id, expected);
      if (!row) return undefined;
      if (resume) this.db.query("DELETE FROM business_foreman_sessions WHERE profile_id=? AND session_id=?").run(resume.profileId, resume.sessionId);
      const envelope = this.foremanEnvelopeFromRow(this.db.query("SELECT * FROM foreman_outbox WHERE id=?").get(id));
      const { text: _text, ...state } = envelope;
      const foremanTurn = { ...state, cancellable: false };
      const event = this.appendEvent("foreman.message.cancelled", foremanTurn);
      return { foremanTurn, event };
    }).immediate();
  }

  completeForemanMessage(id: string): ForemanEnvelope | undefined {
    const completedAt = this.timestamp();
    this.db.query("UPDATE foreman_outbox SET status='done', completed_at=?, last_event_at=?, elapsed_ms=CAST((julianday(?) - julianday(COALESCE(started_at,queued_at,created_at))) * 86400000 AS INTEGER), business_status='no_change' WHERE id=? AND status IN ('queued','claimed')").run(completedAt, completedAt, completedAt, id);
    const row = this.db.query("SELECT * FROM foreman_outbox WHERE id=?").get(id) as any;
    return row ? this.foremanEnvelopeFromRow(row) : undefined;
  }

  failForemanMessage(id: string, failure?: { sessionId: string; harness: string; reason: string }): Event | undefined {
    return this.db.transaction(() => {
      const completedAt = this.timestamp();
      const row = this.db.query<{ message_id: string }>("UPDATE foreman_outbox SET status='failed', completed_at=?, last_event_at=?, elapsed_ms=CAST((julianday(?) - julianday(COALESCE(started_at,queued_at,created_at))) * 86400000 AS INTEGER), business_status='failed', business_reason=? WHERE id=? AND status='claimed' RETURNING message_id").get(completedAt, completedAt, completedAt, failure?.reason ?? "official-harness-failed", id);
      if (!row || !failure) return undefined;
      const envelope = this.foremanEnvelopeFromRow(this.db.query("SELECT * FROM foreman_outbox WHERE id=?").get(id));
      return this.appendEvent("foreman.session.failed", {
        ...failure, ...envelope, inboxId: id, id, messageId: row.message_id, status: "failed", cancellable: false,
      });
    }).immediate();
  }

  listSecretRequests(): Array<SecretRequest & { projectName?: string }> {
    return this.db.query(`SELECT request.*, project.name AS project_name FROM secret_requests AS request
      LEFT JOIN projects AS project ON project.id = request.project_id ORDER BY request.created_at, request.id`).all().map((row: any) => ({
      id: row.id, projectId: row.project_id, name: row.name, vaultRef: row.vault_ref, status: row.status,
      projectName: row.project_name ?? undefined, metadata: redactStoredValue(parseJson(row.metadata)) as Record<string, unknown>, createdAt: row.created_at, updatedAt: row.updated_at,
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
      metadata: redactStoredValue(parseJson(row.metadata)) as Record<string, unknown>,
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    };
  }

  markSecretAvailable(id: string): SecretRequest | undefined {
    const timestamp = this.timestamp();
    const changed = this.db.query("UPDATE secret_requests SET status = 'available', updated_at = ? WHERE id = ? AND status = 'requested'").run(timestamp, id) as { changes?: number };
    if ((changed.changes ?? 0) === 0) return undefined;
    this.recordSecretAudit(id, "available", "user");
    return this.getSecretRequest(id);
  }

  setSecretStatus(id: string, status: SecretRequest["status"], actor = "user", detail: Record<string, unknown> = {}): SecretRequest | undefined {
    const current = this.getSecretRequest(id);
    if (!current || current.status === status) return current;
    const timestamp = this.timestamp();
    const changed = this.db.query("UPDATE secret_requests SET status=?, updated_at=? WHERE id=?").run(status, timestamp, id) as { changes?: number };
    if ((changed.changes ?? 0) === 0) return undefined;
    this.recordSecretAudit(id, status, actor, undefined, detail);
    return this.getSecretRequest(id);
  }

  recordSecretAudit(requestId: string, action: string, actor: string, unitRunId?: string, detail: Record<string, unknown> = {}): void {
    const safeDetail = redactStoredValue(detail) as Record<string, unknown>;
    this.db.query("INSERT INTO secret_audit (id,request_id,action,unit_run_id,actor,detail,created_at) VALUES (?,?,?,?,?,?,?)")
      .run(this.uuid(), requestId, action, unitRunId ?? null, actor, json(safeDetail), this.timestamp());
  }

  listSecretAudit(requestId: string): Array<{ id: string; requestId: string; action: string; unitRunId?: string; actor: string; detail: Record<string, unknown>; createdAt: string }> {
    return this.db.query("SELECT * FROM secret_audit WHERE request_id=? ORDER BY created_at,id").all(requestId).map((row: any) => ({
      id: row.id, requestId: row.request_id, action: row.action, unitRunId: row.unit_run_id ?? undefined,
      actor: row.actor, detail: redactStoredValue(parseJson(row.detail)) as Record<string, unknown>, createdAt: row.created_at,
    }));
  }

  appendEvent(type: string, data: unknown): Event {
    const value = { id: this.uuid(), type, data, createdAt: this.timestamp() };
    const result = this.db.query("INSERT INTO business_events (id,type,data,created_at) VALUES (?,?,?,?)").run(value.id, value.type, json(value.data), value.createdAt);
    return { ...value, seq: Number(result.lastInsertRowid) };
  }

  /** Copies append-only legacy events into the business event log exactly once. */
  syncLegacyEvents(limit = 128): Event[] {
    if (!Number.isSafeInteger(limit) || limit < 1) return [];
    const source = this.legacy ?? this.db;
    const columns = source.query<{ name: string }>("PRAGMA table_info(events)").all().map(({ name }) => name);
    if (!columns.includes("seq") || !columns.includes("id") || !columns.includes("kind") || !columns.includes("at") || !columns.includes("data")) return [];
    const sourceKey = this.legacy ? `legacy:${this.legacyPath}` : "legacy:local";
    const last = this.db.query<{ seq: number }>("SELECT COALESCE(MAX(legacy_seq), 0) AS seq FROM legacy_event_bridge WHERE source_key=?").get(sourceKey)?.seq ?? 0;
    const selected = source.query<LegacyBridgeRow>(`
      SELECT seq,id,kind,at,data${columns.includes("job_id") ? ",job_id" : ""}${columns.includes("run_id") ? ",run_id" : ""}
      FROM events WHERE seq > ? ORDER BY seq LIMIT ?
    `).all(last, limit);
    if (!selected.length) return [];
    return this.db.transaction(() => selected.flatMap((row) => {
      const inserted = this.db.query(`
        INSERT OR IGNORE INTO legacy_event_bridge (source_key,legacy_seq,legacy_id,business_seq,bridged_at)
        VALUES (?,?,?,?,?)
      `).run(sourceKey, row.seq, row.id, 0, this.timestamp()) as { changes?: number };
      if ((inserted.changes ?? 0) === 0) return [];
      const payload = parseJson(row.data);
      const data: Record<string, unknown> = {
        ...payload,
        source: "legacy",
        legacy_seq: row.seq,
        legacy_id: row.id,
        ...(row.job_id ? { job_id: row.job_id } : {}),
        ...(row.run_id ? { run_id: row.run_id } : {}),
      };
      const event = this.appendEvent(`legacy.${row.kind}`, data);
      this.db.query("UPDATE legacy_event_bridge SET business_seq=? WHERE source_key=? AND legacy_seq=?")
        .run(event.seq, sourceKey, row.seq);
      return [event];
    })).immediate();
  }

  eventsAfter(seq = 0, limit = -1): Event[] {
    return this.db.query("SELECT * FROM business_events WHERE seq > ? ORDER BY seq LIMIT ?").all(seq, limit).map((row: any) => ({
      id: row.id, seq: Number(row.seq), type: row.type, data: JSON.parse(row.data), createdAt: row.created_at,
    }));
  }

  recentEvents(limit: number): Event[] {
    return this.db.query("SELECT * FROM business_events ORDER BY seq DESC LIMIT ?").all(limit).reverse().map((row: any) => ({
      id: row.id, seq: Number(row.seq), type: row.type, data: JSON.parse(row.data), createdAt: row.created_at,
    }));
  }

  latestEventSequence(): number {
    this.syncLegacyEvents();
    return Number(this.db.query<{ seq: number }>("SELECT COALESCE(MAX(seq), 0) AS seq FROM business_events").get()?.seq ?? 0);
  }

  legacyEvents(limit?: number): Event[] {
    const source = this.legacy ?? this.db;
    if (!this.hasLegacyTable("events")) return [];
    const rows = limit === undefined
      ? source.query("SELECT seq,id,kind,data,at FROM events ORDER BY seq").all()
      : source.query("SELECT seq,id,kind,data,at FROM events ORDER BY seq DESC LIMIT ?").all(limit).reverse();
    return rows.map((row: any) => ({
      seq: Number(row.seq), id: row.id, type: row.kind, data: parseJson(row.data), createdAt: row.at,
    }));
  }

  legacyArtifacts(limit?: number): Artifact[] {
    if (limit === 0) return [];
    const source = this.legacyAssetSource();
    return source ? this.legacyAssetRows(source, limit).map((row) => this.legacyAssetFromRow(row)) : [];
  }

  private legacyAssetSource(): StateDatabase | undefined {
    if (this.hasLegacyTable("asset_records")) return this.legacy ?? this.db;
  }

  private legacyAssetRows(source: StateDatabase, limit?: number, older?: LegacyAssetCursor): LegacyAssetRow[] {
    if (limit === 0) return [];
    if (limit === undefined) {
      return source.query<LegacyAssetRow>("SELECT id,document,created_at,by_actor,CAST(rowid AS TEXT) AS row_id FROM asset_records ORDER BY created_at").all();
    }
    return source.query<LegacyAssetRow>(`
      SELECT id, document, created_at, by_actor, CAST(rowid AS TEXT) AS row_id FROM asset_records
      WHERE rowid IN (
        SELECT rowid FROM asset_records
        ${older ? "WHERE (created_at, rowid) < (?, CAST(? AS INTEGER))" : ""}
        ORDER BY created_at DESC, rowid DESC LIMIT ?
      )
      ORDER BY created_at, rowid
    `).all(...(older ? [older.createdAt, older.rowId, limit] : [limit]));
  }

  private legacyAssetFromRow(row: LegacyAssetRow): Artifact {
    return {
      id: row.id, projectId: "legacy", kind: "asset", ref: row.id, metadata: { document: parseJson(row.document), byActor: row.by_actor }, createdAt: row.created_at,
    };
  }

  legacyTalk(limit?: number): TalkMessage[] {
    const source = this.legacy ?? this.db;
    if (!this.hasLegacyTable("talk_messages")) return [];
    const rows = limit === undefined
      ? source.query("SELECT id,role,body,card,created_at FROM talk_messages ORDER BY created_at, id").all()
      : source.query("SELECT id,role,body,card,created_at FROM talk_messages ORDER BY created_at DESC, id DESC LIMIT ?").all(limit).reverse();
    return rows.map((row: any) => ({
      id: row.id, role: row.role, body: row.body, metadata: { card: parseJson(row.card) }, createdAt: row.created_at,
    }));
  }

  recentTalk(limit: number): TalkMessage[] {
    const bounded = Math.max(1, Math.min(200, Math.trunc(limit)));
    const merged = new Map<string, TalkMessage>();
    for (const message of this.legacyTalk(bounded)) merged.set(message.id, message);
    for (const message of this.listTalkMessages(bounded)) merged.set(message.id, message);
    return [...merged.values()]
      .sort((left, right) => left.createdAt.localeCompare(right.createdAt) || left.id.localeCompare(right.id))
      .slice(-bounded);
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
    try {
      this.db.close();
    } finally {
      this.legacy?.close();
    }
  }
}

