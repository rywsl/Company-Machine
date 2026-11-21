import { EventBus } from "./event-bus";
import { SqliteStore, type AssetCursor } from "./db";
import { VaultStore } from "./vault";
import { exportBusinessSystem, importBusinessSystem } from "./business-portability";
import { existsSync, lstatSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { dirname, extname, join, normalize, resolve } from "node:path";
import { RandomIds, canonicalBytes, digestBytes, type Clock } from "./legacy/foundation";
import { Warehouse } from "./legacy/warehouse";
import type { Artifact, BusinessConversion, BusinessSystem, Event, HarnessProfile, TalkMessage } from "./models";
import type { RuntimeHealth } from "../runtime/podman";
import { selectedContainerProvider } from "../runtime/selected";
import { LocalSecretProvider, ProviderError, SqliteStateProvider, type StorageProviders } from "./storage";
import { createStorageProviders, probeStorageStatus, storageStatus } from "./storage-factory";
import { resolveStorageProviderConfig } from "./providers";
import { ForemanCoordinator } from "./foreman";
import { prepareProjectSources, type PrepareProjectSources } from "./project-source-mounts";
import { repositoryReference } from "./project-sources";
import { createMaterialFileStage } from "./material-files";
import type { RegisterRepository } from "./project-registration";
import { foremanCredential } from "./foreman-credentials";
import { verifyHarness } from "./harness-verification";
import { OperatorCoordinator, projectCredentialName, type OperatorCoordinatorOptions } from "./operator";
import { operatorBindingView, parseOperatorBinding } from "./operator-binding";
import { harnessEndpointEnv, officialHarness, officialHarnesses as harnessDefinitions, resolveOfficialHarness, supportsLocalProjectMounts, type OfficialSpawn } from "./harness";
import type { AcpSpawn } from "./acp";
import { validSessionTimeout } from "./harness-turn";
import { captureBusinessKernel, kernelRunMetadata, loadBusinessKernel, materializeBusinessKernel } from "./business-kernel-bridge";
import { createBusinessKernelExecution } from "./business-kernel-bridge";
import { MachineExecutor } from "./machine-executor";
import { Context } from "./legacy/context";
import type { ContainerProvider } from "../runtime/podman";
import { EpochStore } from "./legacy/registry-epochs";
import { readGovernedRegistry } from "./legacy/registry-config";
import { loadSnapshot, type Snapshot } from "./legacy/snapshot";

function defaultHeaders(): Record<string, string> {
  return { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };
}
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: defaultHeaders() });
const SECRET_TEXT = /(api[_-]?key|secret|token|password|authorization|cookie)\s*[:=]\s*\S+/gi;
const redactText = (value: string) => value.replace(SECRET_TEXT, (match) => `${match.split(/[:=]/, 1)[0]}=[REDACTED]`);

function isLoopbackHost(host: string): boolean {
  const value = host.replace(/^\[|\]$/g, "").split(":")[0].toLowerCase();
  return value === "localhost" || value === "127.0.0.1" || value === "::1";
}

type AuthScope = {
  role?: "admin" | "foreman" | "operator" | "viewer";
  business_systems?: string[];
  projects?: string[];
};

type AuthPrincipal = {
  role: "admin" | "foreman" | "operator" | "viewer";
  businessSystems?: ReadonlySet<string>;
  projects?: ReadonlySet<string>;
  scoped: boolean;
};

function configuredAuthScopes(): Record<string, AuthScope> | undefined {
  const raw = process.env.MAO_AUTH_SCOPES_JSON?.trim();
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(Object.entries(parsed).flatMap(([token, value]) => {
      if (!token || !value || typeof value !== "object" || Array.isArray(value)) return [];
      const scope = value as Record<string, unknown>;
      const role = scope.role === "admin" || scope.role === "foreman" || scope.role === "operator" || scope.role === "viewer" ? scope.role : "viewer";
      const systems = Array.isArray(scope.business_systems) ? scope.business_systems.filter((item): item is string => typeof item === "string" && item.length > 0) : [];
      const projects = Array.isArray(scope.projects) ? scope.projects.filter((item): item is string => typeof item === "string" && item.length > 0) : [];
      return [[token, { role, business_systems: systems, projects }]];
    })) as Record<string, AuthScope>;
  } catch {
    return {};
  }
}

function authenticateRequest(request: Request): AuthPrincipal | undefined {
  const scopes = configuredAuthScopes();
  const configuredToken = process.env.MAO_AUTH_TOKEN?.trim();
  const authorization = request.headers.get("authorization") ?? "";
  const presented = authorization.startsWith("Bearer ") ? authorization.slice("Bearer ".length).trim() : "";
  if (scopes !== undefined) {
    const scope = presented ? scopes[presented] : undefined;
    if (!scope) return undefined;
    return {
      role: scope.role ?? "viewer",
      businessSystems: new Set(scope.business_systems ?? []),
      projects: new Set(scope.projects ?? []),
      scoped: true,
    };
  }
  if (configuredToken) {
    if (presented !== configuredToken) return undefined;
    return { role: "admin", scoped: false };
  }
  if (isLoopbackHost(new URL(request.url).hostname)) return { role: "admin", scoped: false };
  return undefined;
}

function canAccessSystem(principal: AuthPrincipal, businessSystemId: string): boolean {
  return !principal.scoped || principal.role === "admin" || Boolean(principal.businessSystems?.has("*") || principal.businessSystems?.has(businessSystemId));
}

function canAccessProject(principal: AuthPrincipal, store: SqliteStore, projectId: string): boolean {
  if (!principal.scoped || principal.role === "admin") return true;
  if (principal.projects?.has("*") || principal.projects?.has(projectId)) return true;
  const project = store.getProject(projectId);
  return Boolean(project?.businessSystemId && (principal.businessSystems?.has("*") || principal.businessSystems?.has(project.businessSystemId)));
}

function canAccessBusinessSystem(principal: AuthPrincipal, store: SqliteStore, businessSystemId: string): boolean {
  return canAccessSystem(principal, businessSystemId)
    || store.listProjects(businessSystemId).some((project) => canAccessProject(principal, store, project.id));
}

function canMutate(principal: AuthPrincipal, role: "foreman" | "operator" = "operator"): boolean {
  return !principal.scoped || principal.role === "admin" || principal.role === role || principal.role === "foreman";
}

function visibleBusinessSystemIds(principal: AuthPrincipal | undefined, store: SqliteStore): Set<string> | undefined {
  if (!principal?.scoped || principal.role === "admin") return undefined;
  const systems = new Set(store.listBusinessSystems()
    .filter((system) => canAccessBusinessSystem(principal, store, system.id))
    .map((system) => system.id));
  for (const project of store.listProjects()) {
    if (project.businessSystemId && canAccessProject(principal, store, project.id)) systems.add(project.businessSystemId);
  }
  return systems;
}

function visibleProjectIds(store: SqliteStore, systems: Set<string> | undefined): Set<string> | undefined {
  if (!systems) return undefined;
  return new Set(store.listProjects()
    .filter((project) => project.businessSystemId && systems.has(project.businessSystemId))
    .map((project) => project.id));
}

function scopedFactoryGraph<T extends { nodes?: Array<{ id: string }>; edges?: Array<{ source: string; target: string }> }>(graph: T, systems: Set<string> | undefined): T {
  if (!systems) return graph;
  const nodes = (graph.nodes ?? []).filter((node) => systems.has(node.id));
  const allowed = new Set(nodes.map((node) => node.id));
  const edges = (graph.edges ?? []).filter((edge) => allowed.has(edge.source) && allowed.has(edge.target));
  return { ...graph, nodes, edges };
}

function scopedTalkMessages(store: SqliteStore, limit: number, principal: AuthPrincipal | undefined, systems: Set<string> | undefined) {
  const messages = store.recentTalk(Math.max(limit * 4, limit));
  if (!systems) return messages.slice(-limit);
  return messages.filter((message) => {
    const metadata = store.talkMessageMetadata(message.id);
    const projectId = typeof metadata.project_id === "string" ? metadata.project_id : undefined;
    if (projectId) return canAccessProject(principal!, store, projectId);
    const systemId = typeof metadata.business_system_id === "string" ? metadata.business_system_id : undefined;
    return Boolean(systemId && systems.has(systemId));
  }).slice(-limit);
}

function scopedAssets(store: SqliteStore, limit: number, projectIds: Set<string> | undefined) {
  if (!projectIds) return webAssets(store, limit);
  const records = store.listAllArtifacts().filter((record) => projectIds.has(record.projectId));
  return projectWebAssets(store, records.slice(-limit), []);
}

function scopedAssetPage(store: SqliteStore, limit: number, older: AssetCursor | undefined, projectIds: Set<string> | undefined) {
  if (!projectIds) return undefined;
  const records = store.listAllArtifacts()
    .filter((record) => projectIds.has(record.projectId))
    .sort((left, right) => left.createdAt.localeCompare(right.createdAt) || left.id.localeCompare(right.id));
  const eligible = older?.source === "business"
    ? records.filter((record) => record.createdAt < older.createdAt || (record.createdAt === older.createdAt && record.id < older.id))
    : records;
  const selected = eligible.slice(-limit);
  const first = selected[0];
  const next = first && eligible.length > selected.length ? { source: "business" as const, createdAt: first.createdAt, id: first.id } : null;
  return { items: projectWebAssets(store, selected, []), total: records.length, older: next };
}

function talkScopeMetadata(body: Record<string, unknown>, principal: AuthPrincipal | undefined, store: SqliteStore): {
  metadata?: Record<string, unknown>;
  error?: Record<string, unknown>;
  status?: number;
} {
  const requestedSystem = typeof body.business_system_id === "string" && body.business_system_id.length > 0 ? body.business_system_id : undefined;
  const requestedProject = typeof body.project_id === "string" && body.project_id.length > 0 ? body.project_id : undefined;
  const project = requestedProject ? store.getProject(requestedProject) : undefined;
  if (requestedProject && !project) return { error: { error: "project was not found" }, status: 404 };
  if (requestedSystem && !store.getBusinessSystem(requestedSystem)) return { error: { error: "business system was not found" }, status: 404 };
  if (requestedSystem && project?.businessSystemId && project.businessSystemId !== requestedSystem) {
    return { error: { code: "SCOPE_INVALID", message: "项目不属于指定业务系统" }, status: 400 };
  }
  const systemId = requestedSystem ?? project?.businessSystemId;
  if (principal?.scoped) {
    if (systemId && !canAccessBusinessSystem(principal, store, systemId)) return { error: { code: "FORBIDDEN_SCOPE", message: "当前凭据没有该业务系统权限" }, status: 403 };
    if (requestedProject && !canAccessProject(principal, store, requestedProject)) return { error: { code: "FORBIDDEN_SCOPE", message: "当前凭据没有该项目权限" }, status: 403 };
    if (principal.role !== "admin" && !systemId && !requestedProject) {
      return { error: { code: "SCOPE_REQUIRED", message: "范围凭据的厂长消息需要业务系统或项目上下文" }, status: 400 };
    }
  }
  return { metadata: { ...(systemId ? { business_system_id: systemId } : {}), ...(requestedProject ? { project_id: requestedProject } : {}) } };
}

function talkEnvelopeVisible(principal: AuthPrincipal, store: SqliteStore, envelope: { messageId: string }): boolean {
  if (!principal.scoped || principal.role === "admin") return true;
  const metadata = store.talkMessageMetadata(envelope.messageId);
  const projectId = typeof metadata.project_id === "string" ? metadata.project_id : undefined;
  if (projectId) return canAccessProject(principal, store, projectId);
  const systemId = typeof metadata.business_system_id === "string" ? metadata.business_system_id : undefined;
  return Boolean(systemId && canAccessBusinessSystem(principal, store, systemId));
}

function corsHeaders(request: Request): Record<string, string> {
  const configured = process.env.MAO_CORS_ORIGIN?.trim();
  const origin = request.headers.get("origin");
  return configured && origin === configured ? { "access-control-allow-origin": configured, "vary": "Origin" } : {};
}

function authorizedRequest(request: Request): boolean {
  const principal = authenticateRequest(request);
  if (!principal) return false;
  if (["GET", "HEAD", "OPTIONS"].includes(request.method)) return true;
  const csrf = process.env.MAO_CSRF_TOKEN?.trim();
  if ((process.env.MAO_AUTH_TOKEN?.trim() || configuredAuthScopes() !== undefined) && (!csrf || request.headers.get("x-mao-csrf") !== csrf)) return false;
  const origin = request.headers.get("origin");
  const configuredOrigin = process.env.MAO_CORS_ORIGIN?.trim();
  return !origin || !configuredOrigin || origin === configuredOrigin;
}

export interface BackendApp {
  store: SqliteStore;
  events: EventBus;
  vault: VaultStore;
  storage: StorageProviders;
  foreman: ForemanCoordinator;
  operator: OperatorCoordinator;
  registerRepository: RegisterRepository;
  recoverQueuedWork(): void;
  fetch(request: Request): Response | Promise<Response>;
  stop(): void;
  shutdown: () => Promise<void>;
}

export interface RuntimeProbe {
  readonly name?: string;
  health(): Promise<RuntimeHealth>;
  ensureReady?(): Promise<RuntimeHealth>;
}

function runtimeProvider(runtime: RuntimeProbe): string {
  return typeof runtime.name === "string" && runtime.name.length > 0 ? runtime.name : "podman";
}

function runtimeDependency(health: RuntimeHealth, provider = "podman") {
  const label = provider === "kiri" ? "Kiri" : "Podman";
  return {
    state: health.available ? "online" : "offline",
    version: health.available ? "available" : null,
    message: health.available ? `${label} 运行时在线` : `${label} 运行时不可用：${health.reason ?? "unknown"}`,
    provider,
    machine: health.machine,
    rootless: health.rootless,
  };
}

interface BackendAppOptions {
  dbPath?: string;
  legacyPath?: string;
  runtime?: RuntimeProbe;
  vault?: VaultStore;
  storageRoot?: string;
  foreman?: { cwd?: string; spawn?: OfficialSpawn; acpSpawn?: AcpSpawn; prepareSources?: PrepareProjectSources };
  operator?: Pick<OperatorCoordinatorOptions, "cwd" | "spawn" | "acpSpawn" | "materialStage" | "prepareSources">;
  harnessVerificationTimeoutMs?: number;
  dispatchWork?: boolean;
  clock?: Clock;
  uuid?: () => string;
  storage?: StorageProviders;
}

export function createApp(options: BackendAppOptions = {}): BackendApp {
  const storageRoot = resolve(options.storageRoot ?? process.env.FACTORY_ROOT ?? dirname(process.env.FACTORY_DB_PATH ?? ".factory/factory.db"));
  const storageConfig = options.storage?.config ?? resolveStorageProviderConfig();
  if (storageConfig.state !== "sqlite" || storageConfig.blob !== "local" || storageConfig.secret !== "local") {
    throw new ProviderError("PROVIDER_UNAVAILABLE", "当前 Bun 控制面尚未将非本地 provider 接入业务事务", {
      reason: "run provider migrate and verify before switching the active control plane",
      state: storageConfig.state,
      blob: storageConfig.blob,
      secret: storageConfig.secret,
    });
  }
  const store = new SqliteStore(options.dbPath, options.legacyPath, {
    clock: options.clock,
    uuid: options.uuid,
    ...(options.storage?.state.database ? { database: options.storage.state.database } : {}),
  });
  const events = new EventBus(store);
  const eventStreams = new AbortController();
  const vault = options.vault ?? new VaultStore(undefined, undefined, store.clock);
  const storage = options.storage ?? createStorageProviders({
    root: storageRoot,
    state: new SqliteStateProvider(store.db, false),
    secret: new LocalSecretProvider(vault),
  });
  let uploadWarehouse: Warehouse | undefined;
  const uploads = () => uploadWarehouse ??= new Warehouse(storageRoot, store.db, store.clock, new RandomIds(), storage.blob);
  const runtime = options.runtime ?? selectedContainerProvider();
  const provider = runtimeProvider(runtime);
  let runtimeCache: { at: number; health: RuntimeHealth } | undefined;
  let runtimeHealthPromise: Promise<RuntimeHealth> | undefined;
  const runtimeHealth = async (refresh = false) => {
    if (!refresh && runtimeCache && store.clock.monotonic() - runtimeCache.at < 5) return runtimeCache.health;
    const previous = runtimeCache;
    const pending = runtimeHealthPromise ?? (runtimeHealthPromise = (async () => {
      const health = await runtime.health();
      // A readiness request can finish while an earlier passive probe is still running.
      if (runtimeCache && runtimeCache !== previous) return runtimeCache.health;
      runtimeCache = { at: store.clock.monotonic(), health };
      return health;
    })());
    try {
      return await pending;
    } finally {
      if (runtimeHealthPromise === pending) runtimeHealthPromise = undefined;
    }
  };
  let runtimeReadyPromise: Promise<RuntimeHealth> | undefined;
  const ensureRuntimeHealth = async (): Promise<RuntimeHealth> => {
    if (!runtime.ensureReady) return runtimeHealth(true);
    const pending = runtimeReadyPromise ?? (runtimeReadyPromise = (async () => {
      const health = await runtime.ensureReady!();
      runtimeCache = { at: store.clock.monotonic(), health };
      return health;
    })());
    try {
      const health = await pending;
      if (!health.available) throw new Error(`${provider} runtime unavailable: ${health.reason ?? "unknown"}`);
      return health;
    } finally {
      if (runtimeReadyPromise === pending) runtimeReadyPromise = undefined;
    }
  };
  const ensureRuntime = async () => {
    if (runtimeCache?.health.available && store.clock.monotonic() - runtimeCache.at < 5) return;
    const health = await ensureRuntimeHealth();
    if (!health.available) throw new Error(`${provider} runtime unavailable: ${health.reason ?? "unknown"}`);
  };
  const verificationJobs = new Map<string, AbortController>();
  const databasePath = (path: string) => path === ":memory:" ? path : resolve(path);
  const registerRepository: RegisterRepository = ({ name, repoUrl, systemId }) => {
    const recorded = store.db.transaction(() => {
      const system = systemId === undefined
        ? store.createBusinessSystem({ name, metadata: { intake: true } })
        : store.getBusinessSystem(systemId);
      if (!system) return undefined;
      const project = store.createIntake({ name, repo_url: repoUrl, business_system_id: system.id });
      return { system, project };
    }).immediate();
    if (!recorded) return undefined;
    const { system, project } = recorded;
    if (systemId === undefined) events.publish("business-system.created", system);
    events.publish("project.intake.created", publicProject(project));
    return { projectId: project.id, systemId: system.id };
  };
  const prepareSources: PrepareProjectSources = (project, sourceOptions) => prepareProjectSources(project, async (digest) => {
    const uploaded = store.db.query<{ size: number }>("SELECT size FROM intake_uploads WHERE digest=?").get(digest);
    if (uploaded) return { path: await uploads().ensureLocal(digest), size: uploaded.size };
  }, [storageRoot, databasePath(store.dbPath), databasePath(store.legacyPath)], { sourceCacheRoot: join(storageRoot, "source-cache"), ...sourceOptions });
  const prepareMachineSources: PrepareProjectSources = (project, sourceOptions) => prepareProjectSources(project, async (digest) => {
    const uploaded = store.db.query<{ size: number }>("SELECT size FROM intake_uploads WHERE digest=?").get(digest);
    if (uploaded) return { path: await uploads().ensureLocal(digest), size: uploaded.size };
  }, [storageRoot, databasePath(store.dbPath), databasePath(store.legacyPath)], { cloneRepositories: false, strict: true, ...sourceOptions });
  const foreman = new ForemanCoordinator({
    store, events, ...options.foreman, ensureRuntime,
    readSecret: (ref) => vault.get(ref),
    factoryEnv: { FACTORY_ROOT: storageRoot, FACTORY_DB_PATH: databasePath(store.legacyPath), BUSINESS_DB_PATH: databasePath(store.dbPath) },
    fetchBusiness: (request) => fetch(request),
    prepareSources: options.foreman?.prepareSources ?? prepareSources,
    foremanWorkspaceRoot: join(storageRoot, "foreman", "workspaces"),
    registerRepository,
  });
  let stopped = false;
  const runInBackground = (work: Promise<void>) => {
    void work.catch((error) => {
      if (!stopped) console.error("Background dispatch failed", redactText(error instanceof Error ? error.message : String(error)));
    });
  };
  const operator = new OperatorCoordinator({
    store, events, materialStage: () => supportsLocalProjectMounts() ? createMaterialFileStage(uploads()) : undefined,
    ...options.operator, onProgress: dispatchOperatorRun, ensureRuntime, readSecret: (ref) => vault.get(ref),
    prepareSources: options.operator?.prepareSources ?? (async (project) => supportsLocalProjectMounts() ? prepareSources(project) : undefined),
  });
  const hasForeman = () => store.listHarnessProfiles().some((profile) => profile.role === "foreman");
  const foremanQueueState = (): { state: "accepted" | "waiting_foreman" | "waiting_foreman_runtime"; setup_required: boolean } => {
    const profile = store.listHarnessProfiles().find((item) => item.role === "foreman");
    if (!profile) return { state: "waiting_foreman", setup_required: true };
    const executionMode = profile.executionMode ?? (profile.image ? "trusted_container" : "native_host");
    if (executionMode === "trusted_container" && !profile.image) return { state: "waiting_foreman_runtime", setup_required: false };
    return { state: "accepted", setup_required: false };
  };
  const dispatchForemanIfConfigured = (id: string) => {
    if (options.dispatchWork !== false && hasForeman()) runInBackground(foreman.dispatch(id));
  };
  const recoverStaleForemanClaims = () => {
    const configured = Number(process.env.MAO_FOREMAN_CLAIM_TIMEOUT_S ?? 900);
    const timeoutSeconds = Number.isFinite(configured) && configured >= 30 && configured <= 86_400 ? Math.trunc(configured) : 900;
    const cutoff = new Date(store.clock.now().getTime() - timeoutSeconds * 1_000).toISOString();
    const recovered = store.db.transaction(() => {
      const rows = store.db.query<{ id: string; message_id: string }>(`
        UPDATE foreman_outbox
        SET status='queued', claimed_at=NULL
        WHERE status='claimed' AND claimed_at IS NOT NULL AND claimed_at < ?
        RETURNING id, message_id
      `).all(cutoff);
      return rows.map(({ id, message_id: messageId }) => store.appendEvent("foreman.message.requeued", {
        id, messageId, reason: "STALE_CLAIM_RECOVERED", cutoff,
      }));
    }).immediate();
    for (const event of recovered) events.notifyCommitted(event);
    return recovered.length;
  };
  const activeKernelMachines = new Map<string, Promise<void>>();
  const activeKernelContexts = new Map<string, Context>();
  const ensureMaterialToBeanConversion = (businessSystemId: string, artifact: Artifact, beanId: string): void => {
    const input = `material://${artifact.id}`;
    const output = `legacy://bean/${beanId}`;
    if (store.hasBusinessConversion(businessSystemId, "material-to-legacy-bean", input, output)) return;
    const conversion = store.createBusinessConversion({
      businessSystemId,
      kind: "material-to-legacy-bean",
      summary: `将业务物料 ${artifact.summary ?? artifact.kind} 接入 MAO Bean`,
      status: "completed",
      inputs: [input],
      outputs: [output],
      metadata: { schema: "mao.business-record/1", artifact_id: artifact.id, legacy_bean_id: beanId },
    });
    events.publish("business-conversion.created", conversion);
  };
  const activeBusinessBeanConversions = new Map<string, Promise<string>>();
  const materializeBusinessArtifactAsBean = async (businessSystemId: string, context: Context, artifact: Artifact): Promise<string> => {
    const legacyBean = artifact.metadata.legacy_bean_id;
    if (typeof legacyBean === "string" && legacyBean.length > 0) {
      ensureMaterialToBeanConversion(businessSystemId, artifact, legacyBean);
      return legacyBean;
    }
    const type = typeof artifact.metadata.legacy_type === "string" ? artifact.metadata.legacy_type : artifact.kind;
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*@[0-9]+\.[0-9]+$/.test(type)) {
      throw new Error(`business material ${artifact.ref} has no legacy Bean type`);
    }
    const files = artifact.files ?? [];
    const grants = new Map(store.artifactFileGrants(businessSystemId, [artifact.id]).map((grant) => [grant.position, grant]));
    const stage = join(context.root, "tmp", `business-input-${store.uuid()}`);
    mkdirSync(stage, { recursive: true });
    try {
      const paths: Record<string, string> = {};
      for (const [position, file] of files.entries()) {
        const grant = grants.get(position);
        if (!grant) throw new Error(`business material ${artifact.ref} has no file grant`);
        const source = uploads().blobPath(grant.digest);
        if (!existsSync(source)) throw new Error(`business material ${artifact.ref} file is missing`);
        const relativePath = typeof file.path === "string" && file.path.length > 0 ? file.path : `file-${position}`;
        const destination = join(stage, String(position));
        await Bun.write(destination, Bun.file(source));
        paths[relativePath] = destination;
      }
      const meta = artifact.metadata.legacy_meta && typeof artifact.metadata.legacy_meta === "object" && !Array.isArray(artifact.metadata.legacy_meta)
        ? artifact.metadata.legacy_meta as Record<string, unknown> : {};
      const facts = artifact.metadata.legacy_facts && typeof artifact.metadata.legacy_facts === "object" && !Array.isArray(artifact.metadata.legacy_facts)
        ? artifact.metadata.legacy_facts as Record<string, unknown> : {};
      const bean = await context.warehouse.add({ type, meta, files: paths });
      context.warehouse.sealMany([[bean.id, facts]], null, digestBytes(canonicalBytes({ artifact: artifact.id, type, facts })));
      store.updateArtifactMetadata(artifact.id, { legacy_bean_id: bean.id, legacy_type: type, legacy_facts: facts });
      ensureMaterialToBeanConversion(businessSystemId, artifact, bean.id);
      return bean.id;
    } finally {
      rmSync(stage, { recursive: true, force: true });
    }
  };
  const resolveKernelInputs = async (businessSystemId: string, context: Context, inputs: Record<string, string | string[]> | undefined) => {
    if (!inputs) return undefined;
    const resolveOne = async (value: string) => {
      if (!/^(?:artifact|material|record):\/\//.test(value)) return value;
      const record = store.resolveBusinessMaterialRefs(businessSystemId, [value])[0]?.record;
      if (!record) throw new Error(`business material ${value} was not found`);
      const conversionKey = `${context.root}:${record.id}`;
      const existing = activeBusinessBeanConversions.get(conversionKey);
      if (existing) return existing;
      const pending = materializeBusinessArtifactAsBean(businessSystemId, context, record)
        .finally(() => activeBusinessBeanConversions.delete(conversionKey));
      activeBusinessBeanConversions.set(conversionKey, pending);
      return pending;
    };
    const resolved: Record<string, string | string[]> = {};
    for (const [port, value] of Object.entries(inputs)) resolved[port] = Array.isArray(value)
      ? await Promise.all(value.map(resolveOne)) : await resolveOne(value);
    return resolved;
  };
  const materializeLegacyOutputs = async (runId: string, unitRunId: string, context: Context, beanIds: readonly string[]): Promise<string[]> => {
    const run = store.getBusinessRun(runId);
    const unitRun = run ? store.getUnitRun(runId, unitRunId) : undefined;
    const unit = unitRun ? store.getBusinessUnit(unitRun.unitId) : undefined;
    if (!run || !unitRun || !unit?.projectId) throw new Error("legacy machine output has no business project");
    const outputs: string[] = [];
    const artifactsByLegacy = new Map<string, Artifact>();
    const legacyRefs = new Map<string, string>();
    for (const artifact of store.listArtifacts(unit.projectId)) {
      const legacyId = artifact.metadata.legacy_bean_id;
      if (typeof legacyId === "string") {
        artifactsByLegacy.set(legacyId, artifact);
        legacyRefs.set(legacyId, `${artifact.recordType ?? "material"}://${artifact.id}`);
      }
    }
    for (const beanId of [...new Set(beanIds)]) {
      const bean = context.warehouse.get(beanId);
      const existing = artifactsByLegacy.get(bean.id);
      if (existing) {
        legacyRefs.set(bean.id, `${existing.recordType ?? "material"}://${existing.id}`);
        outputs.push(`${existing.recordType ?? "material"}://${existing.id}`);
        continue;
      }
      const lineage = context.warehouse.lineage(bean.id, "up").beans.map((id) => legacyRefs.get(id) ?? `legacy://bean/${id}`);
      const inputs = [...new Set([...unitRun.inputs, ...lineage])];
      const files = bean.payload.map((file) => ({
        ref: `cas://${file.digest}`,
        path: file.path,
        mediaType: file.mediaType,
        digest: file.digest,
      }));
      for (const file of bean.payload) {
        const source = context.warehouse.blobPath(file.digest);
        const destination = uploads().blobPath(file.digest);
        if (resolve(source) !== resolve(destination)) {
          mkdirSync(dirname(destination), { recursive: true });
          if (!existsSync(destination)) await Bun.write(destination, Bun.file(source));
        }
      }
      const artifact = store.addArtifact({
        projectId: unit.projectId,
        recordType: "material",
        kind: bean.type,
        ref: `material://legacy/${bean.id}`,
        title: `MAO Bean ${bean.type}`,
        summary: `Legacy MAO Runner 产出 ${bean.type}`,
        status: bean.sealed ? "sealed" : "produced",
        files,
        inputs: inputs.length ? inputs : undefined,
        lineage: lineage.length ? lineage : undefined,
        metadata: {
          schema: "mao.business-record/1",
          legacy_bean_id: bean.id,
          legacy_type: bean.type,
          legacy_meta: bean.meta,
          legacy_facts: bean.facts,
          ...(typeof (run.metadata.kernel as Record<string, unknown> | undefined)?.epoch === "string"
            ? { validation_epoch: (run.metadata.kernel as Record<string, unknown>).epoch } : {}),
        },
        producer: { machine: unit.id },
      });
      store.registerArtifactFiles(artifact.id, bean.payload.map((file, position) => ({ position, digest: file.digest, size: file.size })));
      events.publish("artifact.created", artifact);
      const conversion = store.createBusinessConversion({
        businessSystemId: unit.businessSystemId,
        kind: "legacy-run-output",
        summary: `MAO Runner 将 ${bean.type} 产出为业务物料`,
        status: "completed",
        inputs: inputs.length ? inputs : beanIds.map((id) => `legacy://bean/${id}`),
        outputs: [artifact.ref],
        producer: { machine: unit.id },
        metadata: { schema: "mao.business-record/1", artifact_id: artifact.id, legacy_run_id: run.metadata.legacy_run_id },
      });
      events.publish("business-conversion.created", conversion);
      artifactsByLegacy.set(bean.id, artifact);
      legacyRefs.set(bean.id, `material://${artifact.id}`);
      outputs.push(`material://${artifact.id}`);
    }
    return outputs;
  };
  const dispatchKernelMachine = (runId: string, unitRunId: string, recovering = false): Promise<void> => {
    const existing = activeKernelMachines.get(unitRunId);
    if (existing) return existing;
    const work = (async () => {
      const unitRun = store.getUnitRun(runId, unitRunId);
      const run = store.getBusinessRun(runId);
      const system = run ? store.getBusinessSystem(run.businessSystemId) : undefined;
      if (!unitRun || !run || !system || unitRun.status !== "running") return;
      let context: Context | undefined;
      try {
        const rawKernel = system.metadata.kernel && typeof system.metadata.kernel === "object" && !Array.isArray(system.metadata.kernel)
          ? system.metadata.kernel as Record<string, unknown> : {};
        const root = typeof rawKernel.factory_root === "string" && /^[A-Za-z]:[\\/]|^\//.test(rawKernel.factory_root)
          ? resolve(rawKernel.factory_root) : storageRoot;
        const frozenEpoch = run.metadata.kernel && typeof run.metadata.kernel === "object" && !Array.isArray(run.metadata.kernel)
          && typeof (run.metadata.kernel as Record<string, unknown>).epoch === "string"
          ? (run.metadata.kernel as Record<string, unknown>).epoch as string : undefined;
        const runKernel = run.metadata.kernel && typeof run.metadata.kernel === "object" && !Array.isArray(run.metadata.kernel)
          ? run.metadata.kernel as Record<string, unknown> : {};
        const frozenLine = typeof runKernel.line === "string" ? runKernel.line : undefined;
        const kernel = frozenEpoch
          ? await materializeBusinessKernel(system, root, frozenEpoch, frozenLine)
          : await loadBusinessKernel(system);
        const frozenInputs = runKernel.inputs && typeof runKernel.inputs === "object" && !Array.isArray(runKernel.inputs)
          ? runKernel.inputs as Record<string, string | string[]> : kernel?.inputs;
        if (!kernel || !frozenInputs) throw new Error("business kernel execution requires kernel.inputs grouped by Line port");
        const provider = runtime as unknown as ContainerProvider;
        if (typeof provider.open !== "function") throw new Error(`${runtimeProvider(runtime)} does not expose a ContainerProvider for MAO Runner`);
        const legacyRunId = typeof run.metadata.legacy_run_id === "string" ? run.metadata.legacy_run_id : undefined;
        if (recovering && !legacyRunId) throw new Error("CONTROL_PLANE_RESTART: legacy Run ID was not persisted");
        context = Context.open(root, { clock: store.clock, blob: storage.blob });
        activeKernelContexts.set(unitRunId, context);
        const executableKernel = { ...kernel, inputs: await resolveKernelInputs(system.id, context, frozenInputs) };
        const execution = createBusinessKernelExecution(context, provider);
        const result = legacyRunId
          ? await execution.resume(executableKernel, legacyRunId)
          : await execution.start(executableKernel, (started) => {
            const updated = store.updateBusinessRunMetadata(run.id, { legacy_run_id: started.id, legacy_state: started.state });
            if (updated) events.publish("business-run.legacy.bound", { runId, unitRunId, legacyRunId: started.id, state: started.state });
          });
        const beanIds = Object.values(result.outputs).flatMap((value) => Array.isArray(value) ? value : [value]).filter((value): value is string => typeof value === "string");
        const outputs = await materializeLegacyOutputs(runId, unitRunId, context, beanIds);
        store.updateBusinessRunMetadata(run.id, { legacy_run_id: result.id, legacy_state: result.state });
        const failure = result.state === "SUCCEEDED" ? undefined : (typeof result.error?.message === "string" ? result.error.message : `legacy-run-${result.state.toLowerCase()}`);
        const completed = store.completeUnitRun(unitRun.id, result.state === "SUCCEEDED" ? "completed" : "failed", outputs, failure);
        if (completed) {
          events.publish(result.state === "SUCCEEDED" ? "machine.run.completed" : "machine.run.failed", { runId, unitRunId, legacyRunId: result.id, state: result.state, outputs, error: result.error });
          dispatchOperatorRun(runId, completed.id);
        }
      } catch (error) {
        const failed = store.completeUnitRun(unitRun.id, "failed", [], error instanceof Error ? error.message : String(error));
        if (failed) {
          events.publish("machine.run.failed", { runId, unitRunId, error: failed.error });
          dispatchOperatorRun(runId, failed.id);
        }
      } finally {
        activeKernelContexts.delete(unitRunId);
        context?.close();
      }
    })().finally(() => { activeKernelMachines.delete(unitRunId); });
    activeKernelMachines.set(unitRunId, work);
    return work;
  };
  const machineExecutor = new MachineExecutor({
    store,
    events,
    prepareSources: prepareMachineSources,
    onActivated: (project, facts, machineId) => {
      const ref = `material://project-source/${project.id}`;
      const existing = store.listArtifacts(project.id).find((artifact) => artifact.ref === ref);
      if (existing) return [existing.ref];
      const artifact = store.addArtifact({
        projectId: project.id,
        recordType: "material",
        kind: "project.source",
        ref,
        title: `${project.name} 项目来源`,
        summary: "Machine 已验证并激活项目来源",
        purpose: "交给 Operator 继续构建业务系统",
        status: "sealed",
        producer: { machine: machineId },
        metadata: { schema: "mao.business-record/1", source_facts: portableValue(facts), activation: "machine" },
      });
      events.publish("artifact.created", artifact);
      return [artifact.ref];
    },
    onProgress: dispatchOperatorRun,
  });
  const dispatchSourceMachine = (runId: string, unitRunId: string, recovering = false): Promise<void> => machineExecutor.dispatch(runId, unitRunId, recovering);
  function dispatchOperatorRun(runId: string, completedUnitRunId?: string) {
    if (stopped || options.dispatchWork === false) return;
    for (const unitRun of store.listReadyUnitRuns(runId, completedUnitRunId)) runInBackground(operator.dispatch(runId, unitRun.id));
    for (const unitRun of store.listReadySourceMachineRuns(runId)) runInBackground(dispatchSourceMachine(runId, unitRun.id));
    for (const unitRun of store.listReadyKernelMachineRuns(runId)) runInBackground(dispatchKernelMachine(runId, unitRun.id));
  }
  const recoverQueuedWork = () => {
    if (stopped || options.dispatchWork === false) return;
    recoverStaleForemanClaims();
    if (hasForeman()) {
      for (const id of store.listQueuedForemanMessageIds()) runInBackground(foreman.dispatch(id));
    }
    store.resumeResourceRuns();
    for (const id of store.listRecoverableBusinessRunIds()) dispatchOperatorRun(id);
    for (const { runId, unitRunId } of store.listRunningSourceMachineRuns()) runInBackground(dispatchSourceMachine(runId, unitRunId, true));
    for (const { runId, unitRunId } of store.listRunningKernelMachineRuns()) runInBackground(dispatchKernelMachine(runId, unitRunId, true));
  };
  const handleRequest = async (request: Request): Promise<Response> => {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: { ...defaultHeaders(), "access-control-allow-methods": "GET,POST,OPTIONS", "access-control-allow-headers": "authorization,content-type,last-event-id,x-mao-csrf" } });
    const principal = authenticateRequest(request);
    if (url.pathname.startsWith("/api/") && (!principal || !authorizedRequest(request))) return response({ code: "AUTH_REQUIRED", message: "需要本地身份或 Bearer token" }, 401);
    if (principal?.scoped) {
      const decodeSegment = (value: string): string | undefined => {
        try { return decodeURIComponent(value); } catch { return undefined; }
      };
      const systemPath = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)/);
      const projectPath = url.pathname.match(/^\/api\/v1\/projects\/([^/]+)/);
      const runPath = url.pathname.match(/^\/api\/v1\/runs\/([^/]+)/);
      const systemId = systemPath && systemPath[1] !== "import" ? decodeSegment(systemPath[1]) : undefined;
      const projectId = projectPath && projectPath[1] !== "intake" ? decodeSegment(projectPath[1]) : undefined;
      if (systemId && !canAccessSystem(principal, systemId)) return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据没有该业务系统权限" }, 403);
      if (projectId && !canAccessProject(principal, store, projectId)) return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据没有该项目权限" }, 403);
      if (runPath) {
        const run = decodeSegment(runPath[1]) ? store.getBusinessRun(decodeSegment(runPath[1])!) : undefined;
        if (run && !canAccessSystem(principal, run.businessSystemId)) return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据没有该运行权限" }, 403);
      }
      if ((url.pathname === "/api/v1/business-systems" || url.pathname === "/api/v1/projects" || url.pathname === "/api/web/constitution") && principal.role !== "admin") {
        return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据需要明确的业务系统范围" }, 403);
      }
      if (url.pathname === "/api/web/graph" && principal.role !== "admin") {
        const requestedScope = url.searchParams.get("scope");
        const requestedWorkarea = url.searchParams.get("workarea");
        if (requestedScope !== "workarea" || !requestedWorkarea || !canAccessBusinessSystem(principal, store, requestedWorkarea)) {
          return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据只能读取已授权厂区图" }, 403);
        }
      }
      if ((url.pathname.startsWith("/api/v1/foreman/") || url.pathname.startsWith("/api/v1/talk") || url.pathname === "/api/web/talk") && !canMutate(principal, "foreman")) {
        return response({ code: "FORBIDDEN_ROLE", message: "当前凭据没有厂长控制权限" }, 403);
      }
      if (principal.role === "foreman" && (url.pathname === "/api/v1/talk" || url.pathname === "/api/v1/foreman/inbox") && request.method === "GET") {
        return response({ code: "FORBIDDEN_SCOPE", message: "范围厂长需要通过具体业务系统上下文读取对话" }, 403);
      }
    }
    if (url.pathname === "/healthz" && request.method === "GET") return response({ ok: true, service: "mao-control-plane" });
    if (url.pathname === "/readyz" && request.method === "GET") {
      try {
        store.db.query("SELECT 1").get();
        const health = await runtimeHealth();
        const ready = health.available;
        return response({ ok: ready, service: "mao-control-plane", runtime: health, foreman_configured: hasForeman() }, ready ? 200 : 503);
      } catch (error) {
        return response({ ok: false, service: "mao-control-plane", error: redactText(error instanceof Error ? error.message : "readiness-check-failed") }, 503);
      }
    }
    if (url.pathname === "/api/v1/runtime" && request.method === "GET") {
      if (principal?.scoped && principal.role !== "admin") return response({ code: "FORBIDDEN_SCOPE", message: "只有管理员凭据可以读取平台运行时信息" }, 403);
      const health = await runtimeHealth();
      return response({ provider, health, dependency: runtimeDependency(health, provider) });
    }
    if (url.pathname === "/api/v1/storage" && request.method === "GET") {
      if (principal?.scoped && principal.role !== "admin") return response({ code: "FORBIDDEN_ROLE", message: "只有管理员凭据可以读取存储 provider 状态" }, 403);
      return response(await probeStorageStatus(storage));
    }
    if (url.pathname === "/api/v1/runtime/ensure-ready" && request.method === "POST") {
      if (principal?.scoped && principal.role !== "admin") return response({ code: "FORBIDDEN_ROLE", message: "只有管理员凭据可以启动平台运行时" }, 403);
      if (!runtime.ensureReady) return response({ error: "runtime readiness is unavailable" }, 501);
      let health: RuntimeHealth;
      try {
        health = await ensureRuntimeHealth();
      } catch (error) {
        if (stopped) return response({ error: "server stopping" }, 503);
        health = runtimeCache?.health ?? await runtime.health();
        if (stopped) return response({ error: "server stopping" }, 503);
        runtimeCache = { at: store.clock.monotonic(), health };
        const dependency = runtimeDependency(health, provider);
        events.publish(`runtime.${provider}.checked`, dependency);
        return response({ provider, health, dependency, message: dependency.message, error: error instanceof Error ? error.message : "runtime unavailable" }, 503);
      }
      if (stopped) return response({ error: "server stopping" }, 503);
      events.publish(`runtime.${provider}.checked`, runtimeDependency(health, provider));
      recoverQueuedWork();
      runInBackground(operator.takeRuntimeDeferredRuns().then((runIds) => {
        for (const id of runIds) dispatchOperatorRun(id);
      }));
      return response({ provider, health, dependency: runtimeDependency(health, provider) }, 200);
    }
    if (url.pathname === "/api/v1/business-systems/import" && request.method === "POST") {
      if (principal?.scoped && principal.role !== "admin") return response({ code: "FORBIDDEN_ROLE", message: "只有管理员凭据可以导入业务系统" }, 403);
      const document = await readJson(request);
      try {
        const imported = importBusinessSystem(store, document, validateDag);
        events.publish("business-system.imported", { id: imported.id, schema: document.schema });
        return response(exportBusinessSystem(store, imported.id, () => graphProjection(store, "workarea", imported.id)), 201);
      } catch (error) {
        return response({ error: error instanceof Error ? error.message : "business system document is invalid" }, 400);
      }
    }
    if (url.pathname === "/api/v1/business-systems" && request.method === "GET") return response(store.listBusinessSystems().map(publicBusinessSystem));
    if (url.pathname === "/api/v1/business-systems" && request.method === "POST") {
      if (principal?.scoped && !canMutate(principal, "foreman")) return response({ code: "FORBIDDEN_ROLE", message: "当前凭据没有创建业务系统权限" }, 403);
      const body = await readJson(request);
      if (!body.name || typeof body.name !== "string") return response({ error: "name is required" }, 400);
      try {
        const candidate = { id: "kernel-check", name: body.name, status: "active", metadata: body.metadata ?? {}, createdAt: "", updatedAt: "" } as BusinessSystem;
        await loadBusinessKernel(candidate);
      } catch (error) {
        return response({ code: "BUSINESS_KERNEL_INVALID", message: error instanceof Error ? error.message : "business kernel is invalid" }, 400);
      }
      const system = store.createBusinessSystem(body as Partial<BusinessSystem> & { name: string });
      events.publish("business-system.created", system);
      return response(publicBusinessSystem(system), 201);
    }
    const businessSystemMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)$/);
    if (businessSystemMatch && request.method === "GET") {
      const system = store.getBusinessSystem(businessSystemMatch[1]);
      if (!system) return response({ error: "business system was not found" }, 404);
      const projects = store.listProjects(system.id);
      const artifacts = store.listBusinessSystemArtifacts(system.id);
      return response({ ...publicBusinessSystem(system), projects: projects.map(publicProject), artifacts: artifacts.map(publicArtifact), materials: artifacts.filter((item) => item.recordType === "material").map(publicArtifact) });
    }
    const graphMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)\/graph$/);
    if (graphMatch && request.method === "GET") {
      const system = store.getBusinessSystem(graphMatch[1]);
      if (!system) return response({ error: "business system was not found" }, 404);
      return response(portableValue(system.metadata.graph ?? graphProjection(store, "workarea", system.id)));
    }
    if (graphMatch && request.method === "POST") {
      if (principal?.scoped && !canMutate(principal, "foreman")) return response({ code: "FORBIDDEN_ROLE", message: "只有厂长或管理员可以更新业务 DAG" }, 403);
      const body = await readJson(request);
      if (!body.graph || typeof body.graph !== "object") return response({ error: "graph is required" }, 400);
      const graphError = validateBusinessGraph(store, graphMatch[1], body.graph);
      if (graphError) return response({ error: graphError }, 400);
      const system = store.updateBusinessSystemMetadata(graphMatch[1], { graph: body.graph, graph_updated_by: body.by ?? "foreman" });
      if (!system) return response({ error: "business system was not found" }, 404);
      events.publish("business-system.graph.updated", { id: system.id, graph: portableValue(body.graph) });
      return response(publicBusinessSystem(system), 200);
    }
    const systemArtifactsMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)\/(artifacts|materials)$/);
    if (systemArtifactsMatch && request.method === "GET") {
      const system = store.getBusinessSystem(systemArtifactsMatch[1]);
      if (!system) return response({ error: "business system was not found" }, 404);
      const artifacts = store.listBusinessSystemArtifacts(system.id);
      return response((systemArtifactsMatch[2] === "materials" ? artifacts.filter((item) => item.recordType === "material") : artifacts).map(publicArtifact));
    }
    const conversionMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)\/conversions$/);
    if (conversionMatch && request.method === "GET") {
      if (!store.getBusinessSystem(conversionMatch[1])) return response({ error: "business system was not found" }, 404);
      return response(store.listBusinessConversions(conversionMatch[1]).map(publicConversion));
    }
    if (conversionMatch && request.method === "POST") {
      if (principal?.scoped && !canMutate(principal, "foreman")) return response({ code: "FORBIDDEN_ROLE", message: "只有厂长或管理员可以登记 Conversion" }, 403);
      const body = await readJson(request);
      if (typeof body.kind !== "string" || typeof body.summary !== "string") return response({ error: "kind and summary are required" }, 400);
      if (body.inputs !== undefined && (!Array.isArray(body.inputs) || body.inputs.some((item: unknown) => typeof item !== "string"))) return response({ error: "inputs must be string references" }, 400);
      if (body.outputs !== undefined && (!Array.isArray(body.outputs) || body.outputs.some((item: unknown) => typeof item !== "string"))) return response({ error: "outputs must be string references" }, 400);
      if (!store.getBusinessSystem(conversionMatch[1])) return response({ error: "business system was not found" }, 404);
      const refs = [...(Array.isArray(body.inputs) ? body.inputs : []), ...(Array.isArray(body.outputs) ? body.outputs : [])];
      const invalid = store.invalidBusinessInputRefs(conversionMatch[1], refs);
      if (invalid.length) return response({ error: "conversion references must belong to the current business system", detail: { refs: invalid } }, 400);
      const conversion = store.createBusinessConversion({ businessSystemId: conversionMatch[1], kind: redactText(body.kind), summary: redactText(body.summary), status: body.status === "running" || body.status === "completed" || body.status === "failed" ? body.status : "planned", inputs: portableValue(body.inputs ?? []) as string[], outputs: portableValue(body.outputs ?? []) as string[], producer: body.producer && typeof body.producer === "object" ? portableValue(body.producer) : undefined, metadata: body.metadata && typeof body.metadata === "object" ? portableValue(body.metadata) : {} });
      events.publish("business-conversion.created", conversion);
      return response(conversion, 201);
    }
    const exportMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)\/export$/);
    if (exportMatch && request.method === "GET") {
      const id = exportMatch[1];
      const document = exportBusinessSystem(store, id, () => graphProjection(store, "workarea", id), (ref) => vault.get(ref));
      return document ? response(document) : response({ error: "business system was not found" }, 404);
    }
    const unitsMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)\/units$/);
    if (unitsMatch && request.method === "GET") {
      if (!store.getBusinessSystem(unitsMatch[1])) return response({ error: "business system was not found" }, 404);
      return response(store.listBusinessUnits(unitsMatch[1]).map(publicUnit));
    }
    const bindingMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)\/units\/([^/]+)\/binding$/);
    if (bindingMatch && request.method === "PATCH") {
      if (principal?.scoped && !canMutate(principal, "foreman")) return response({ code: "FORBIDDEN_ROLE", message: "只有厂长或管理员可以绑定 Operator" }, 403);
      const input = parseOperatorBinding(await readJson(request));
      if (!input) return response({ error: "需要有效的官方 Harness ID 和模型 ID" }, 400);
      let systemId: string;
      let unitId: string;
      try {
        systemId = decodeURIComponent(bindingMatch[1]);
        unitId = decodeURIComponent(bindingMatch[2]);
      } catch { return response({ error: "业务系统或 Operator 标识无效" }, 400); }
      const updated = store.updateOperatorBinding(systemId, unitId, input);
      if (!updated) return response({ error: "找不到此业务系统的 Operator" }, 404);
      const binding = operatorBindingView(updated, store.listHarnessProfiles());
      events.publish("business-unit.binding.changed", { unitId, binding });
      return response({ unit: publicUnit(updated), binding });
    }
    const runsMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)\/runs$/);
    if (runsMatch && request.method === "GET") {
      if (!store.getBusinessSystem(runsMatch[1])) return response({ error: "business system was not found" }, 404);
      return response(store.listBusinessRunsWithUnits(runsMatch[1]).map((run) => ({ ...publicBusinessRun(run), units: run.units.map(publicUnitRun) })));
    }
    if (runsMatch && request.method === "POST") {
      if (principal?.scoped && !canMutate(principal, "operator")) return response({ code: "FORBIDDEN_ROLE", message: "当前凭据没有启动业务运行权限" }, 403);
      const body = await readJson(request);
      const inputs = Array.isArray(body.inputs) ? body.inputs.filter((item: unknown): item is string => typeof item === "string") : [];
      const invalidInputs = store.invalidBusinessInputRefs(runsMatch[1], inputs);
      if (invalidInputs.length) return response({ error: "business run inputs must belong to the current business system", detail: { refs: invalidInputs } }, 400);
      const system = store.getBusinessSystem(runsMatch[1]);
      if (!system) return response({ error: "business system was not found" }, 404);
      let kernel;
      try {
        kernel = await loadBusinessKernel(system);
        if (kernel) kernel = await captureBusinessKernel(kernel, kernel.factoryRoot ?? storageRoot, store.clock);
      }
      catch (error) { return response({ code: "BUSINESS_KERNEL_INVALID", message: error instanceof Error ? error.message : "business kernel is invalid" }, 400); }
      const dispatch = body.dispatch !== false;
      const metadata = { ...(body.metadata && typeof body.metadata === "object" ? body.metadata : {}), ...(kernel ? kernelRunMetadata(kernel) : {}) };
      const run = store.createBusinessRun(runsMatch[1], typeof body.trigger === "string" ? body.trigger : "manual", metadata, inputs, dispatch);
      if (!run) return response({ error: "business system was not found" }, 404);
      events.publish("business-run.created", { ...run, units: store.listUnitRuns(run.id) });
      if (kernel) events.publish("business-run.kernel.bound", { runId: run.id, ...kernelRunMetadata(kernel).kernel });
      if (run.status === "queued" && dispatch) dispatchOperatorRun(run.id);
      return response({ ...publicBusinessRun(run), units: store.listUnitRuns(run.id).map(publicUnitRun) }, 201);
    }
    const runMatch = url.pathname.match(/^\/api\/v1\/runs\/([^/]+)$/);
    if (runMatch && request.method === "GET") {
      const run = store.getBusinessRun(runMatch[1]);
      if (!run) return response({ error: "business run was not found" }, 404);
      return response({ ...publicBusinessRun(run), units: store.listUnitRuns(run.id).map(publicUnitRun) });
    }
    const dispatchRunMatch = url.pathname.match(/^\/api\/v1\/runs\/([^/]+)\/dispatch$/);
    if (dispatchRunMatch && request.method === "POST") {
      if (principal?.scoped && !canMutate(principal, "operator")) return response({ code: "FORBIDDEN_ROLE", message: "当前凭据没有派发业务运行权限" }, 403);
      const run = store.getBusinessRun(dispatchRunMatch[1]);
      if (!run) return response({ error: "business run was not found" }, 404);
      store.authorizeBusinessRunDispatch(run.id);
      dispatchOperatorRun(run.id);
      return response({ state: "dispatching", run_id: run.id }, 202);
    }
    const cancelRunMatch = url.pathname.match(/^\/api\/v1\/runs\/([^/]+)\/(cancel|retry)$/);
    if (cancelRunMatch && request.method === "POST") {
      if (principal?.scoped && !canMutate(principal, "operator")) return response({ code: "FORBIDDEN_ROLE", message: "当前凭据没有控制业务运行权限" }, 403);
      let run;
      if (cancelRunMatch[2] === "cancel") {
        run = store.cancelBusinessRun(cancelRunMatch[1]);
        await Promise.all([operator.cancelRun(cancelRunMatch[1]), machineExecutor.cancelRun(cancelRunMatch[1])]);
      } else {
        run = store.retryBusinessRun(cancelRunMatch[1]);
      }
      if (!run) return response({ error: "business run was not found" }, 404);
      events.publish(cancelRunMatch[2] === "cancel" ? "business-run.cancelled" : "business-run.retried", { ...run, units: store.listUnitRuns(run.id) });
      if (cancelRunMatch[2] === "retry") dispatchOperatorRun(run.id);
      return response({ ...publicBusinessRun(run), units: store.listUnitRuns(run.id).map(publicUnitRun) }, 200);
    }
    if (url.pathname === "/api/v1/projects" && request.method === "GET") return response(store.listProjects().map(publicProject));
    if (url.pathname === "/api/v1/projects/intake" && request.method === "POST") {
      if (principal?.scoped && !canMutate(principal, "foreman")) return response({ code: "FORBIDDEN_ROLE", message: "只有厂长或管理员可以接收新项目" }, 403);
      let body: Record<string, any>;
      let files: File[] = [];
      if (request.headers.get("content-type")?.startsWith("multipart/form-data")) {
        let form: FormData;
        try { form = await request.formData(); }
        catch { return response({ error: "invalid project upload" }, 400); }
        body = {};
        for (const field of ["name", "repo_url", "local_folder", "business_system_id"]) {
          const value = form.get(field);
          if (typeof value === "string" && value.trim()) body[field] = value.trim();
        }
        files = form.getAll("files").filter((file): file is File => typeof file !== "string" && Boolean(file.name));
        body.uploaded_file_refs = form.getAll("uploaded_file_refs").filter((ref): ref is string => typeof ref === "string" && Boolean(ref.trim()));
      } else body = await readJson(request);
      if (principal?.scoped) {
        if (typeof body.business_system_id === "string" && !canAccessBusinessSystem(principal, store, body.business_system_id)) {
          return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据没有该业务系统权限" }, 403);
        }
        if (typeof body.business_system_id !== "string" && !canMutate(principal, "foreman")) {
          return response({ code: "FORBIDDEN_ROLE", message: "当前凭据没有创建业务系统权限" }, 403);
        }
      }
      if (body.repo_url !== undefined && body.repo_url !== "") {
        const reference = repositoryReference(body.repo_url, "intake");
        if (!reference) return response({ error: "请提交不含凭证、查询参数或片段的仓库链接" }, 400);
        body.repo_url = reference;
      }
      if (body.secret_requests !== undefined && (!Array.isArray(body.secret_requests)
        || body.secret_requests.some((item: any) => !item || !projectCredentialName(item.name)))) {
        return response({ error: "project credential requests are invalid" }, 400);
      }
      if (files.length) body.files = await saveProjectUploads(storageRoot, uploads(), store, files);
      if (!body.repo_url && !body.local_folder && !(Array.isArray(body.files) && body.files.length) && !(Array.isArray(body.uploaded_file_refs) && body.uploaded_file_refs.length)) {
        return response({ error: "one project source is required" }, 400);
      }
      const recorded = store.db.transaction(() => {
        let createdSystem: BusinessSystem | undefined;
        let systemId = body.business_system_id;
        if (typeof systemId !== "string") {
          const name = typeof body.name === "string" ? body.name : typeof body.project_name === "string" ? body.project_name : "新业务系统";
          createdSystem = store.createBusinessSystem({ name, metadata: { intake: true } });
          systemId = createdSystem.id;
        } else if (!store.getBusinessSystem(systemId)) return undefined;
        const project = store.createIntake({ ...body, business_system_id: systemId });
        const text = `项目「${project.name}」已提交。请厂长识别项目、建立 Operator 与 Machine，搭建业务 DAG，并说明下一步需要的项目资源。项目引用 ${project.id}。`;
        const intakeMessage = store.addTalkMessage({ role: "user", body: text, metadata: { source: "project-intake", project_id: project.id } });
        store.bridgeHumanTalk(intakeMessage.id, intakeMessage.body);
        const intakeEnvelope = store.queueForemanMessage(intakeMessage.id, intakeMessage.body);
        intakeMessage.metadata = { ...intakeMessage.metadata, inbox_id: intakeEnvelope.id };
        store.db.query("UPDATE business_talk_messages SET metadata=? WHERE id=?").run(JSON.stringify(intakeMessage.metadata), intakeMessage.id);
        const visibleProject = publicProject(project);
        const committed = [
          ...(createdSystem ? [store.appendEvent("business-system.created", createdSystem)] : []),
          store.appendEvent("project.intake.created", visibleProject),
          store.appendEvent("talk.message.created", intakeMessage),
          store.appendEvent("foreman.message.queued", intakeEnvelope),
        ];
        return { projectId: project.id, visibleProject, intakeEnvelope, committed };
      }).immediate();
      if (!recorded) return response({ error: "business system was not found" }, 404);
      const { projectId, visibleProject, intakeEnvelope, committed } = recorded;
      for (const event of committed) events.notifyCommitted(event);
      dispatchForemanIfConfigured(intakeEnvelope.id);
      const queueState = foremanQueueState();
      return response({ ...visibleProject, project_id: projectId, ...queueState, foreman_setup_required: queueState.setup_required, foreman_inbox_id: intakeEnvelope.id }, 201);
    }
    const projectFile = url.pathname.match(/^\/api\/v1\/projects\/([^/]+)\/files\/(sha256:[0-9a-f]{64})$/);
    if (projectFile && request.method === "GET") {
      const [, projectId, digest] = projectFile;
      const stored = store.db.query("SELECT 1 FROM intake_uploads WHERE digest=?").get(digest);
      const source = store.getProject(projectId)?.sources.find((item) => item.fileRef === `cas://${digest}`);
      if (!stored || !source) return response({ error: "project upload was not found" }, 404);
      const path = uploads().blobPath(digest);
      if (!existsSync(path)) return response({ error: "project upload was not found" }, 404);
      const filename = typeof source.metadata.name === "string" ? source.metadata.name : digest;
      return new Response(Bun.file(path), { headers: {
        "content-type": "application/octet-stream", "x-content-type-options": "nosniff", "cache-control": "no-store",
        "content-disposition": `attachment; filename*=UTF-8''${encodeURIComponent(filename)}`,
      } });
    }
    const artifactFile = url.pathname.match(/^\/api\/v1\/projects\/([^/]+)\/artifacts\/([^/]+)\/files\/(sha256:[0-9a-f]{64})$/);
    if (artifactFile && request.method === "GET") {
      const [, projectId, artifactId, digest] = artifactFile;
      const grant = store.projectArtifactFile(projectId, artifactId, digest);
      if (!grant) return response({ error: "artifact file was not found" }, 404);
      const path = uploads().blobPath(digest);
      const file = lstatSync(path, { throwIfNoEntry: false });
      if (!file?.isFile() || file.size !== grant.size) return response({ error: "artifact file was not found" }, 404);
      const filename = grant.path?.split("/").at(-1) || digest;
      return new Response(Bun.file(path), { headers: {
        "content-type": "application/octet-stream", "x-content-type-options": "nosniff", "cache-control": "no-store",
        "content-disposition": `attachment; filename*=UTF-8''${encodeURIComponent(filename)}`,
      } });
    }
    const artifactMatch = url.pathname.match(/^\/api\/v1\/projects\/([^/]+)\/artifacts$/);
    if (artifactMatch && request.method === "GET") return response(store.listArtifacts(artifactMatch[1]).map(publicArtifact));
    if (artifactMatch && request.method === "POST") {
      if (principal?.scoped && !canMutate(principal, "operator")) return response({ code: "FORBIDDEN_ROLE", message: "当前凭据没有登记业务产物权限" }, 403);
      const body = await readJson(request);
      if (typeof body.kind !== "string" || typeof body.ref !== "string" || typeof body.summary !== "string") return response({ error: "kind, ref, and summary are required" }, 400);
      if (body.record_type !== undefined && body.record_type !== "artifact" && body.record_type !== "material") return response({ error: "record_type must be artifact or material" }, 400);
      if (body.files !== undefined && (!Array.isArray(body.files) || body.files.some((file: unknown) => !file || typeof file !== "object" || typeof (file as any).ref !== "string"))) return response({ error: "files must contain file references" }, 400);
      const project = store.getProject(artifactMatch[1]);
      if (!project) return response({ error: "project was not found" }, 404);
      if (body.schema !== undefined && body.schema !== "mao.business-record/1") return response({ error: "unsupported business record schema" }, 400);
      if (project.businessSystemId) {
        const references = [...(Array.isArray(body.inputs) ? body.inputs : []), ...(Array.isArray(body.lineage) ? body.lineage : [])];
        const invalid = store.invalidBusinessInputRefs(project.businessSystemId, references);
        if (invalid.length) return response({ error: "record references must belong to the current business system", detail: { refs: invalid } }, 400);
      }
      const safeMetadata = body.metadata && typeof body.metadata === "object" ? portableValue(body.metadata) : {};
      const safeTitle = typeof body.title === "string" ? redactText(body.title) : undefined;
      const safeDescription = typeof body.description === "string" ? redactText(body.description) : undefined;
      const safeSummary = redactText(body.summary);
      const safePurpose = typeof body.purpose === "string" ? redactText(body.purpose) : undefined;
      const safeProducer = body.producer && typeof body.producer === "object" ? portableValue(body.producer) : undefined;
      const safeFiles = body.files === undefined ? undefined : portableValue(body.files);
      const safeInputs = body.inputs === undefined ? undefined : portableValue(body.inputs);
      const safeOutputs = body.outputs === undefined ? undefined : portableValue(body.outputs);
      const safeLineage = body.lineage === undefined ? undefined : portableValue(body.lineage);
      const metadata = { ...safeMetadata, record_type: body.record_type, title: safeTitle, description: safeDescription, summary: safeSummary, purpose: safePurpose, status: body.status, producer: safeProducer, files: safeFiles, inputs: safeInputs, outputs: safeOutputs, lineage: safeLineage };
      const artifact = store.addArtifact({ projectId: project.id, sourceId: typeof body.source_id === "string" ? body.source_id : undefined, recordType: body.record_type, kind: redactText(body.kind), ref: redactText(body.ref), title: safeTitle, description: safeDescription, status: typeof body.status === "string" ? body.status : undefined, producer: safeProducer, summary: safeSummary, purpose: safePurpose, files: safeFiles as Artifact["files"], inputs: safeInputs as string[], outputs: safeOutputs as string[], lineage: safeLineage as string[], metadata });
      events.publish("artifact.created", artifact);
      if (project.businessSystemId && (artifact.inputs?.length || artifact.lineage?.length)) {
        const conversion = store.createBusinessConversion({
          businessSystemId: project.businessSystemId,
          kind: "record-production",
          summary: artifact.summary ?? artifact.title ?? `生成 ${artifact.kind}`,
          status: "completed",
          inputs: [...new Set([...(artifact.inputs ?? []), ...(artifact.lineage ?? [])])],
          outputs: [artifact.ref],
          producer: artifact.producer ? { operator: artifact.producer.operator, harness: artifact.producer.harness } : undefined,
          metadata: { artifact_id: artifact.id, schema: "mao.business-record/1" },
        });
        events.publish("business-conversion.created", conversion);
      }
      return response(publicArtifact(artifact), 201);
    }
    if (url.pathname === "/api/v1/talk" && request.method === "POST") {
      const body = await readJson(request);
      const text = typeof body.body === "string" ? body.body : body.text;
      if (typeof text !== "string" || text.length === 0) return response({ error: "body is required" }, 400);
      const scope = talkScopeMetadata(body, principal, store);
      if (scope.error) return response(scope.error, scope.status);
      const { message, envelope, events: committed } = store.enqueueForemanTalk({ body: redactText(text), metadata: scope.metadata });
      for (const event of committed) events.notifyCommitted(event);
      dispatchForemanIfConfigured(envelope.id);
      return response({ ...foremanQueueState(), message, foremanTurn: foreman.turnState(envelope) }, 201);
    }
    if (url.pathname === "/api/v1/talk" && request.method === "GET") {
      const messages = new Map<string, TalkMessage>();
      for (const message of store.legacyTalk()) messages.set(message.id, message);
      for (const message of store.listTalkMessages()) messages.set(message.id, message);
      return response([...messages.values()].sort((left, right) => left.createdAt.localeCompare(right.createdAt) || left.id.localeCompare(right.id)));
    }
    if (url.pathname === "/api/v1/talk/reply" && request.method === "POST") {
      const body = await readJson(request);
      const text = typeof body.text === "string" ? body.text : body.body;
      if (typeof text !== "string" || text.length === 0 || typeof body.turn_id !== "string") return response({ error: "text and turn_id are required" }, 400);
      const envelope = store.getForemanMessage(body.turn_id);
      if (principal?.scoped && (!envelope || !talkEnvelopeVisible(principal, store, envelope))) return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据没有该厂长轮次权限" }, 403);
      const message = store.addTalkMessage({ role: "foreman", body: redactText(text), metadata: { turn_id: body.turn_id, by: typeof body.by === "string" ? body.by : "foreman" } });
      events.publish("talk.foreman.replied", message);
      return response({ state: "accepted", message }, 201);
    }
    if (url.pathname === "/api/v1/foreman/inbox" && request.method === "GET") {
      const limit = Number(url.searchParams.get("limit") ?? 50);
      return response(store.listForemanInbox(limit));
    }
    const foremanAck = url.pathname.match(/^\/api\/v1\/foreman\/inbox\/([^/]+)\/(claim|done)$/);
    if (foremanAck && request.method === "POST") {
      const current = store.getForemanMessage(foremanAck[1]);
      if (!current) return response({ error: "foreman message was not found" }, 404);
      if (principal?.scoped && !talkEnvelopeVisible(principal, store, current)) return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据没有该厂长轮次权限" }, 403);
      const envelope = foremanAck[2] === "claim" ? store.claimForemanMessage(foremanAck[1]) : store.completeForemanMessage(foremanAck[1]);
      if (!envelope) return response({ error: "foreman message was not found" }, 404);
      if (foremanAck[2] === "done" && envelope.status !== "done") {
        return response({ error: "厂长轮次已结束", foremanTurn: foreman.turnState(envelope) }, 409);
      }
      events.publish(`foreman.message.${foremanAck[2]}`, { ...envelope, cancellable: foreman.turnState(envelope).cancellable });
      return response(envelope, 200);
    }
    const foremanCancel = url.pathname.match(/^\/api\/v1\/foreman\/inbox\/([^/]+)\/cancel$/);
    if (foremanCancel && request.method === "POST") {
      const current = store.getForemanMessage(foremanCancel[1]);
      if (!current) return response({ error: "厂长轮次不存在" }, 404);
      if (principal?.scoped && !talkEnvelopeVisible(principal, store, current)) return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据没有该厂长轮次权限" }, 403);
      const { cancelled, foremanTurn } = foreman.cancel(foremanCancel[1]);
      if (!foremanTurn) return response({ error: "厂长轮次不存在" }, 404);
      if (cancelled) return response({ foremanTurn });
      const error = foremanTurn.status === "claimed" && !foremanTurn.cancellable ? "厂长轮次由另一个进程领取" : "厂长轮次状态已改变";
      return response({ error, foremanTurn }, 409);
    }
    const foremanDispatch = url.pathname.match(/^\/api\/v1\/foreman\/inbox\/([^/]+)\/dispatch$/);
    if (foremanDispatch && request.method === "POST") {
      const current = store.getForemanMessage(foremanDispatch[1]);
      if (!current) return response({ error: "厂长轮次不存在" }, 404);
      if (principal?.scoped && !talkEnvelopeVisible(principal, store, current)) return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据没有该厂长轮次权限" }, 403);
      runInBackground(foreman.dispatch(foremanDispatch[1]));
      return response({ state: "dispatching", inbox_id: foremanDispatch[1] }, 202);
    }
    if (url.pathname === "/api/v1/secrets/requests" && request.method === "POST") {
      if (principal?.scoped && !canMutate(principal, "operator")) return response({ code: "FORBIDDEN_ROLE", message: "当前凭据没有申请项目密钥权限" }, 403);
      const body = await readJson(request);
      if (typeof body.project_id !== "string" || !projectCredentialName(body.name)) return response({ error: "project_id and a valid credential name are required" }, 400);
      if (!store.getProject(body.project_id)) return response({ error: "project was not found" }, 404);
      if (principal?.scoped && !canAccessProject(principal, store, body.project_id)) return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据没有该项目权限" }, 403);
      const requestRecord = store.createSecretRequest({ projectId: body.project_id, name: body.name, vaultRef: `vault://secret/${store.uuid()}`, metadata: { purpose: typeof body.purpose === "string" ? redactText(body.purpose) : undefined, scope: typeof body.scope === "string" ? redactText(body.scope) : undefined } });
      const { vaultRef: _vaultRef, ...visible } = requestRecord;
      events.publish("secret.requested", visible);
      return response({ request_id: requestRecord.id, state: requestRecord.status, request: visible }, 201);
    }
    const secretActionMatch = url.pathname.match(/^\/api\/v1\/secrets\/requests\/([^/]+)\/(deny|revoke|rotate|audit)$/);
    if (secretActionMatch && request.method === "GET" && secretActionMatch[2] === "audit") {
      const record = store.getSecretRequest(secretActionMatch[1]);
      if (record && principal?.scoped && !canAccessProject(principal, store, record.projectId)) return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据没有该项目权限" }, 403);
      return record ? response(store.listSecretAudit(record.id)) : response({ error: "secret request was not found" }, 404);
    }
    if (secretActionMatch && request.method === "POST" && secretActionMatch[2] !== "audit") {
      if (principal?.scoped && !canMutate(principal, "foreman")) return response({ code: "FORBIDDEN_ROLE", message: "只有厂长或管理员可以治理项目密钥" }, 403);
      const record = store.getSecretRequest(secretActionMatch[1]);
      if (!record) return response({ error: "secret request was not found" }, 404);
      if (principal?.scoped && !canAccessProject(principal, store, record.projectId)) return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据没有该项目权限" }, 403);
      if (secretActionMatch[2] === "rotate") {
        if (record.status === "revoked") return response({ error: "secret request is already revoked" }, 409);
        store.setSecretStatus(record.id, "revoked", "user", { reason: "rotation" });
        vault.delete(record.vaultRef);
        const rotated = store.createSecretRequest({ projectId: record.projectId, name: record.name, vaultRef: `vault://secret/${store.uuid()}`, metadata: { ...record.metadata, rotated_from: record.id } });
        events.publish("secret.rotated", { id: rotated.id, projectId: rotated.projectId, name: rotated.name, status: rotated.status });
        return response({ request_id: rotated.id, state: rotated.status }, 201);
      }
      const status = secretActionMatch[2] === "deny" ? "denied" : "revoked";
      const updated = store.setSecretStatus(record.id, status, "user");
      if (status === "revoked") vault.delete(record.vaultRef);
      events.publish(`secret.${status}`, { id: record.id, projectId: record.projectId, name: record.name, status: updated?.status ?? status });
      return response({ request_id: record.id, state: updated?.status ?? status }, 200);
    }
    const secretValueMatch = url.pathname.match(/^\/api\/v1\/secrets\/requests\/([^/]+)\/value$/);
    const resourceReadyMatch = url.pathname.match(/^\/api\/v1\/secrets\/requests\/([^/]+)\/ready$/);
    if (resourceReadyMatch && request.method === "POST") {
      if (principal?.scoped && !canMutate(principal, "foreman")) return response({ code: "FORBIDDEN_ROLE", message: "只有厂长或管理员可以标记项目资源已准备" }, 403);
      const requestRecord = store.getSecretRequest(resourceReadyMatch[1]);
      if (!requestRecord) return response({ error: "resource request was not found" }, 404);
      if (principal?.scoped && !canAccessProject(principal, store, requestRecord.projectId)) return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据没有该项目权限" }, 403);
      if (requestRecord.metadata.kind !== "source-tool") return response({ error: "only source-tool resources can be marked ready" }, 400);
      if (requestRecord.status !== "requested") return response({ error: `resource request is ${requestRecord.status}` }, 409);
      const { runIds, event } = store.db.transaction(() => {
        const available = store.markSecretAvailable(requestRecord.id);
        if (!available) throw new Error("resource request changed while writing");
        const runIds = store.resumeResourceRuns(requestRecord.id);
        const event = store.appendEvent("source.tool.available", {
          id: requestRecord.id, projectId: requestRecord.projectId, name: requestRecord.name,
          tool: requestRecord.metadata.tool, status: available.status,
        });
        return { runIds, event };
      }).immediate();
      events.notifyCommitted(event);
      for (const runId of runIds) dispatchOperatorRun(runId);
      return response({ request_id: requestRecord.id, state: "available" }, 200);
    }
    if (secretValueMatch && request.method === "POST") {
      if (principal?.scoped && !canMutate(principal, "foreman")) return response({ code: "FORBIDDEN_ROLE", message: "只有厂长或管理员可以写入项目密钥" }, 403);
      const requestRecord = store.getSecretRequest(secretValueMatch[1]);
      if (!requestRecord) return response({ error: "secret request was not found" }, 404);
      if (principal?.scoped && !canAccessProject(principal, store, requestRecord.projectId)) return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据没有该项目权限" }, 403);
      const body = await readJson(request);
      if (typeof body.value !== "string" || body.value.length === 0) return response({ error: "value is required" }, 400);
      if (requestRecord.status !== "requested") return response({ error: `secret request is ${requestRecord.status}` }, 409);
      try {
        vault.put(requestRecord.vaultRef, body.value);
      } catch (error) {
        return response({ error: error instanceof Error ? error.message : "secret storage failed" }, 503);
      }
      const { runIds, event } = store.db.transaction(() => {
        const available = store.markSecretAvailable(requestRecord.id);
        if (!available) throw new Error("secret request changed while writing");
        const runIds = store.resumeResourceRuns(requestRecord.id);
        const event = store.appendEvent("secret.available", {
          id: requestRecord.id, projectId: requestRecord.projectId, name: requestRecord.name, status: available?.status ?? "available",
        });
        return { runIds, event };
      }).immediate();
      events.notifyCommitted(event);
      for (const runId of runIds) dispatchOperatorRun(runId);
      return response({ request_id: requestRecord.id, state: "available" }, 200);
    }
    // The existing web client uses these read-only projections while it migrates to v1.
    if (url.pathname === "/api/web/talk" && request.method === "GET") {
      const requested = Number(url.searchParams.get("limit") ?? 80);
      const limit = Number.isFinite(requested) ? Math.max(1, Math.min(1000, Math.trunc(requested))) : 80;
      return response(scopedTalkMessages(store, limit, principal, visibleBusinessSystemIds(principal, store)));
    }
    if (url.pathname === "/api/web/logs" && request.method === "GET") {
      const requested = Number(url.searchParams.get("limit") ?? 100);
      const limit = Number.isSafeInteger(requested) ? Math.max(1, Math.min(1000, requested)) : 100;
      const since = url.searchParams.get("since");
      if (since !== null && (!/^\d+$/.test(since) || Number(since) > Number.MAX_SAFE_INTEGER)) return response({ code: "INVALID_CURSOR", message: "日志游标无效" }, 400);
      const systems = visibleBusinessSystemIds(principal, store);
      return response(systems ? scopedLogs(store, limit, principal, since === null ? undefined : Number(since)) : webLogs(store, limit, since === null ? undefined : Number(since)));
    }
    if (url.pathname === "/api/web/graph" && request.method === "GET") {
      const scope = url.searchParams.get("scope") ?? "factory";
      const workarea = url.searchParams.get("workarea");
      if (scope !== "factory" && scope !== "workarea") return response({ code: "INVALID_GRAPH_SCOPE", message: "scope 必须是 factory 或 workarea" }, 400);
      if (scope === "factory" && workarea) return response({ code: "INVALID_GRAPH_SCOPE", message: "factory 图不能指定 workarea" }, 400);
      if (scope === "workarea" && !workarea) return response({ code: "WORKAREA_REQUIRED", message: "workarea 图需要业务系统标识" }, 400);
      if (scope === "workarea") {
        const businessSystem = store.getBusinessSystem(workarea!);
        if (businessSystem) return response(graphProjection(store, scope, workarea));
        const registryGraph = await registryWorkareaProjection(store, storageRoot, workarea!);
        return registryGraph ? response(registryGraph) : response({ code: "WORKAREA_NOT_FOUND", message: "厂区不存在" }, 404);
      }
      return response(await factoryGraphProjection(store, storageRoot));
    }
    if (url.pathname === "/api/web/constitution" && request.method === "GET") {
      return response(await constitutionProjection(storageRoot));
    }
    if (url.pathname === "/api/web/harnesses" && request.method === "GET") {
      if (principal?.scoped && principal.role !== "admin") return response({ code: "FORBIDDEN_ROLE", message: "只有管理员凭据可以读取全局 Harness 配置" }, 403);
      const profiles = store.listHarnessProfiles();
      return response(officialHarnesses(profiles).map((item) => ({ ...item, configuration: publicProfile(profiles.find((profile) => profile.runtime === item.id), (ref) => vault.get(ref)) })));
    }
    if (url.pathname === "/api/web/harnesses/configure" && request.method === "POST") {
      if (principal?.scoped && principal.role !== "admin") return response({ code: "FORBIDDEN_ROLE", message: "只有管理员凭据可以配置 Harness" }, 403);
      const body = await readJson(request);
      const definition = typeof body.runtime === "string" ? officialHarness(body.runtime) : undefined;
      if (!definition || (body.role !== "foreman" && body.role !== "operator") || typeof body.model !== "string" || !body.model.trim()) {
        return response({ error: "runtime, role, and model are required" }, 400);
      }
      if (typeof body.api_key === "string" && body.api_key.length > 0) return response({ error: "密钥必须通过项目资源请求写入保险箱" }, 400);
      const baseUrl = typeof body.base_url === "string" ? body.base_url.trim() : "";
      try { harnessEndpointEnv(definition, baseUrl); }
      catch { return response({ error: "invalid Harness base URL" }, 400); }
      const image = typeof body.image === "string" ? body.image.trim() : "";
      if (image && !/^[^\s@]+@sha256:[0-9a-f]{64}$/.test(image)) return response({ error: "Harness image must be pinned by digest" }, 400);
      const executionMode = body.role === "foreman"
        ? body.execution_mode === "native_host" ? "native_host" : "trusted_container"
        : "native_host";
      const currentProfile = store.listHarnessProfiles().find((item) => item.id === `${body.role}:${definition.id}`);
      const sourceProfileId = typeof body.api_key_profile_id === "string" && body.api_key_profile_id.length > 0 ? body.api_key_profile_id : undefined;
      const sourceProfile = sourceProfileId ? store.listHarnessProfiles().find((item) => item.id === sourceProfileId && item.role === body.role) : undefined;
      if (sourceProfileId && !sourceProfile) return response({ error: "saved API key configuration was not found" }, 400);
      const sessionTimeoutSeconds = body.session_timeout_s;
      if (sessionTimeoutSeconds !== undefined && !validSessionTimeout(sessionTimeoutSeconds)) {
        return response({ error: "Harness session timeout must be a whole number of seconds from 1 to 2147483" }, 400);
      }
      const thinkingEffort = body.thinking_effort === undefined ? undefined : body.thinking_effort;
      if (thinkingEffort !== undefined && (typeof thinkingEffort !== "string" || !["minimal", "low", "medium", "high", "max"].includes(thinkingEffort))) {
        return response({ error: "thinking_effort must be minimal, low, medium, high, or max" }, 400);
      }
      const contextTokens = body.context;
      if (contextTokens !== undefined && (!Number.isInteger(contextTokens) || contextTokens < 1 || contextTokens > 2_147_483_647)) {
        return response({ error: "context must be a positive whole number" }, 400);
      }
      const sessionBudgetUsd = body.session_budget_usd;
      const dailyBudgetUsd = body.daily_budget_usd;
      if ((sessionBudgetUsd !== undefined && (typeof sessionBudgetUsd !== "number" || !Number.isFinite(sessionBudgetUsd) || sessionBudgetUsd < 0))
        || (dailyBudgetUsd !== undefined && (typeof dailyBudgetUsd !== "number" || !Number.isFinite(dailyBudgetUsd) || dailyBudgetUsd < 0))) {
        return response({ error: "session and daily budgets must be non-negative numbers" }, 400);
      }
      const maxTurns = body.max_turns;
      const maxAttempts = body.max_attempts;
      const backoffSeconds = body.backoff_s;
      if ((maxTurns !== undefined && (!Number.isInteger(maxTurns) || maxTurns < 1))
        || (maxAttempts !== undefined && (!Number.isInteger(maxAttempts) || maxAttempts < 1))
        || (backoffSeconds !== undefined && (!Number.isInteger(backoffSeconds) || backoffSeconds < 0))) {
        return response({ error: "max_turns and max_attempts must be positive whole numbers, backoff_s must be non-negative" }, 400);
      }
      const profile = store.saveHarnessProfile({
        runtime: definition.id, role: body.role, baseUrl, model: body.model.trim(), mode: typeof body.mode === "string" ? body.mode : "session", executionMode,
        sessionTimeoutSeconds, thinkingEffort, contextTokens, sessionBudgetUsd, dailyBudgetUsd, maxTurns, maxAttempts, backoffSeconds,
        secretRef: sourceProfile?.secretRef ?? currentProfile?.secretRef,
        image: image || undefined,
        network: image
          ? body.network === "egress" ? "egress" : body.network === "none" ? "none" : body.role === "foreman" && executionMode === "trusted_container" ? "bridge" : "none"
          : undefined,
        verification: { state: "pending", checkedAt: store.timestamp() },
      });
      const persistedProfile = store.listHarnessProfiles().find((item) => item.id === profile.id) ?? profile;
      if (body.role === "foreman" && [sessionTimeoutSeconds, sessionBudgetUsd, dailyBudgetUsd, maxTurns, maxAttempts, backoffSeconds].some((value) => value !== undefined)) {
        const operatorProfile = store.listHarnessProfiles().find((item) => item.role === "operator");
        if (operatorProfile) store.saveHarnessProfile({
          ...operatorProfile,
          sessionTimeoutSeconds: persistedProfile.sessionTimeoutSeconds,
          sessionBudgetUsd: persistedProfile.sessionBudgetUsd,
          dailyBudgetUsd: persistedProfile.dailyBudgetUsd,
          maxTurns: persistedProfile.maxTurns,
          maxAttempts: persistedProfile.maxAttempts,
          backoffSeconds: persistedProfile.backoffSeconds,
        });
      }
      verificationJobs.get(profile.id)?.abort();
      verificationJobs.delete(profile.id);
      events.publish("harness.profile.saved", { id: profile.id, runtime: profile.runtime, role: profile.role });
      // A project may have been accepted before the Foreman was configured.
      // Reconcile the durable queue immediately so first-run setup does not
      // require a restart or a manual dispatch request.
      if (profile.role === "foreman") recoverQueuedWork();
      const foremanProfile = persistedProfile.role === "foreman" ? persistedProfile : store.listHarnessProfiles().find((item) => item.role === "foreman");
      const foremanPublic = publicProfile(foremanProfile, (ref) => vault.get(ref));
      return response({ profile: publicProfile(persistedProfile, (ref) => vault.get(ref)), foreman: { configured: Boolean(foremanProfile), setup_required: !foremanProfile, runtime: foremanProfile?.runtime, model: foremanProfile?.model, mode: foremanProfile?.mode, runtime_state: foremanPublic?.foreman_runtime?.state, runtime_reason: foremanPublic?.foreman_runtime?.reason, profile: foremanPublic } }, 200);
    }
    if (url.pathname === "/api/web/harnesses/verify" && request.method === "POST") {
      if (principal?.scoped && principal.role !== "admin") return response({ code: "FORBIDDEN_ROLE", message: "只有管理员凭据可以验证 Harness" }, 403);
      const body = await readJson(request);
      if (typeof body.profile_id !== "string") return response({ error: "profile_id is required" }, 400);
      const existing = store.listHarnessProfiles().find((item) => item.id === body.profile_id);
      if (!existing) return response({ error: "profile was not found" }, 404);
      if (verificationJobs.has(existing.id)) return response({ profile: publicProfile(existing, (ref) => vault.get(ref)) }, 202);
      const profile = store.markHarnessVerification(existing.id, "requested", "正在验证官方 Harness 连通性");
      if (!profile) return response({ error: "profile was not found" }, 404);
      const controller = new AbortController();
      verificationJobs.set(profile.id, controller);
      events.publish("harness.profile.verification.requested", { id: profile.id, runtime: profile.runtime, role: profile.role });
      const transportOptions = profile.role === "operator" ? options.operator : options.foreman;
      void verifyHarness(profile, { ...transportOptions, ensureRuntime, readSecret: (ref) => vault.get(ref), signal: controller.signal, timeoutMs: options.harnessVerificationTimeoutMs }).then(({ state, reason }) => {
        if (verificationJobs.get(profile.id) !== controller) return;
        verificationJobs.delete(profile.id);
        const current = store.listHarnessProfiles().find((item) => item.id === profile.id);
        if (current?.updatedAt !== profile.updatedAt || current.verification?.state !== "requested") return;
        const completed = store.markHarnessVerification(profile.id, state, reason);
        if (completed) events.publish("harness.profile.verification.completed", { id: profile.id, runtime: profile.runtime, role: profile.role, verification: publicProfile(completed, (ref) => vault.get(ref))?.verification });
      }).catch(() => {});
      return response({ profile: publicProfile(profile, (ref) => vault.get(ref)) }, 202);
    }
    if (url.pathname === "/api/web/assets" && request.method === "GET") {
      const limit = Math.trunc(Math.max(1, Number(url.searchParams.get("limit") ?? 100)));
      const systems = visibleBusinessSystemIds(principal, store);
      return response(scopedAssets(store, Number.isSafeInteger(limit) ? limit : 100, visibleProjectIds(store, systems)));
    }
    if (url.pathname === "/api/web/assets/page" && request.method === "GET") {
      const limit = assetPageLimit(url.searchParams.get("limit") ?? "100");
      const token = url.searchParams.get("older");
      const older = token ? decodeAssetCursor(token) : undefined;
      if (limit === undefined || (token && !older)) return response({ error: "invalid asset page limit or cursor" }, 400);
      const systems = visibleBusinessSystemIds(principal, store);
      if (systems) {
        const page = scopedAssetPage(store, limit, older, visibleProjectIds(store, systems));
        const olderCursor = page?.older ? Buffer.from(JSON.stringify({ v: 1, ...page.older })).toString("base64url") : null;
        return response({ items: page?.items ?? [], total: page?.total ?? 0, olderCursor });
      }
      return response(webAssetPage(store, limit, older));
    }
    if (url.pathname === "/api/web/secrets" && request.method === "GET") {
      if (principal?.scoped && principal.role !== "admin") return response({ code: "FORBIDDEN_SCOPE", message: "当前凭据没有全局密钥列表权限" }, 403);
      return response(store.listSecretRequests().map(publicSecretRequest));
    }
    if (url.pathname === "/api/web/talk" && request.method === "POST") {
      const body = await readJson(request);
      const text = typeof body.text === "string" ? body.text : body.body;
      if (typeof text !== "string" || text.length === 0) return response({ error: "text is required" }, 400);
      const scope = talkScopeMetadata(body, principal, store);
      if (scope.error) return response(scope.error, scope.status);
      const { message, envelope, events: committed } = store.enqueueForemanTalk({ body: redactText(text), metadata: scope.metadata });
      for (const event of committed) events.notifyCommitted(event);
      dispatchForemanIfConfigured(envelope.id);
      return response({ ...foremanQueueState(), message, eventCursor: store.latestEventSequence(), foremanTurn: foreman.turnState(envelope) }, 201);
    }
    if (url.pathname === "/api/web/bootstrap" && request.method === "GET") {
      const assetLimit = url.searchParams.get("assetLimit");
      const limit = assetLimit === null ? undefined : assetPageLimit(assetLimit);
      if (assetLimit !== null && limit === undefined) return response({ error: "invalid asset page limit" }, 400);
      const health = await runtimeHealth();
      const eventCursor = store.latestEventSequence();
      const systems = visibleBusinessSystemIds(principal, store);
      const projects = visibleProjectIds(store, systems);
      const factoryGraph = systems ? scopedFactoryGraph(graphProjection(store, "factory", null), systems) : await factoryGraphProjection(store, storageRoot);
      const foremanProfile = store.listHarnessProfiles().find((profile) => profile.role === "foreman");
      const talk = scopedTalkMessages(store, 80, principal, systems);
      const foremanTurns = store.listForemanTurns(talk.map(({ id }) => id)).map((turn) => foreman.turnState(turn));
      const scopedPage = systems && limit !== undefined ? scopedAssetPage(store, limit, undefined, projects) : undefined;
      const assetPage = limit === undefined ? undefined : scopedPage ?? webAssetPage(store, limit);
      const assets = assetPage ? assetPage.items : scopedAssets(store, 100, projects);
      const logs = systems ? scopedLogs(store, 100, principal) : webLogs(store, 100);
      const secrets = store.listSecretRequests().filter((secret) => !projects || projects.has(secret.projectId));
      const harnesses = systems ? officialHarnesses([]) : officialHarnesses(store.listHarnessProfiles());
      return response({ version: "backend-v1", connection: { state: "connected" }, status: { at: store.timestamp() },
        eventCursor, foremanTurns, pendingForemanReplies: foreman.pendingReplies(),
        graph: { factory: factoryGraph },
        assets, logs, talk,
        ...(assetPage && { assetTotal: assetPage.total, assetOlderCursor: "olderCursor" in assetPage ? assetPage.olderCursor : assetPage.older ? Buffer.from(JSON.stringify({ v: 1, ...assetPage.older })).toString("base64url") : null }),
        harnesses: harnesses.map((item) => ({ ...item, configuration: systems ? null : publicProfile(store.listHarnessProfiles().find((profile) => profile.runtime === item.id), (ref) => vault.get(ref)) })), harness_profiles: systems ? [] : store.listHarnessProfiles().map((profile) => publicProfile(profile, (ref) => vault.get(ref))), secrets: secrets.map(publicSecretRequest),
        dependencies: { [provider]: runtimeDependency(health, provider), podman: provider === "podman" ? runtimeDependency(health, provider) : { state: "inactive", provider: "podman", message: "当前使用 Kiri" }, kiri: provider === "kiri" ? runtimeDependency(health, provider) : { state: "inactive", provider: "kiri", message: "当前使用 Podman" }, harnesses: officialHarnesses(store.listHarnessProfiles()).map((item) => ({ id: item.id, name: item.name, state: item.available ? "available" : item.container_configured ? "configured" : "unavailable" })), storage: await probeStorageStatus(storage) },
        foreman: (() => { const projected = systems ? null : publicProfile(foremanProfile, (ref) => vault.get(ref)); const runtime = projected?.foreman_runtime; return { configured: Boolean(foremanProfile), setup_required: !foremanProfile, runtime: systems ? null : foremanProfile?.runtime ?? null, model: systems ? null : foremanProfile?.model ?? null, mode: systems ? null : foremanProfile?.mode ?? null, runtime_state: runtime?.state, runtime_reason: runtime?.reason, runtime_provider: provider, profile: projected }; })(), constitution: await constitutionProjection(storageRoot) });
    }
    if (url.pathname === "/api/v1/events" && request.method === "GET") {
      const signal = AbortSignal.any([request.signal, eventStreams.signal]);
      if (principal?.scoped && principal.role !== "admin") {
        return eventsResponse(events, request.headers.get("last-event-id") ?? url.searchParams.get("cursor"), signal,
          {}, (event) => scopedEventVisible(principal, store, event));
      }
      return eventsResponse(events, request.headers.get("last-event-id") ?? url.searchParams.get("cursor"), signal);
    }
    if (request.method === "GET" && !url.pathname.startsWith("/api/")) return staticResponse(url.pathname);
    return response({ error: "not found" }, 404);
  };
  const fetch = async (request: Request): Promise<Response> => {
    const result = await handleRequest(request);
    const cors = corsHeaders(request);
    if (!Object.keys(cors).length) return result;
    const responseHeaders = new Headers(result.headers);
    for (const [key, value] of Object.entries(cors)) responseHeaders.set(key, value);
    return new Response(result.body, { status: result.status, statusText: result.statusText, headers: responseHeaders });
  };
  let stopPromise: Promise<void> | undefined;
  let stopStarted = false;
  const beginStop = () => {
    if (stopStarted) return;
    stopStarted = true;
    stopped = true;
    eventStreams.abort();
    for (const job of verificationJobs.values()) job.abort();
    verificationJobs.clear();
    foreman.stop();
    operator.stop();
    machineExecutor.stop();
  };
  const shutdown = () => stopPromise ??= (async () => {
    beginStop();
    await Promise.allSettled([foreman.close(), operator.close(), machineExecutor.close(), ...activeKernelMachines.values()]);
    for (const context of activeKernelContexts.values()) context.close();
    activeKernelContexts.clear();
    storage.state.close();
    store.close();
  })();
  const stop = () => {
    beginStop();
    for (const context of activeKernelContexts.values()) context.close();
    activeKernelContexts.clear();
    storage.state.close();
    store.close();
  };
  return { store, events, vault, storage, foreman, operator, registerRepository, fetch, recoverQueuedWork, stop, shutdown };
}

function staticResponse(pathname: string): Response {
  const root = process.env.MAO_WEB_DIST ?? "frontend/dist";
  const relative = pathname === "/" ? "index.html" : pathname.replace(/^\/+/, "");
  const candidate = normalize(join(root, relative));
  const normalizedRoot = normalize(root);
  if (!candidate.startsWith(`${normalizedRoot}\\`) && !candidate.startsWith(`${normalizedRoot}/`)) return new Response("not found", { status: 404 });
  const file = existsSync(candidate) ? candidate : join(root, "index.html");
  if (!existsSync(file)) return new Response("not found", { status: 404 });
  const contentTypes: Record<string, string> = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json" };
  return new Response(Bun.file(file), { headers: { "content-type": contentTypes[extname(file)] ?? "application/octet-stream", "cache-control": "no-cache" } });
}

function officialHarnesses(profiles: readonly HarnessProfile[] = []) {
  return harnessDefinitions().map((definition) => ({
    id: definition.id, name: definition.displayName, kind: "agent-cli", source: "official",
    roles: ["operator", "foreman"], available: Boolean(Bun.which(definition.executable)),
    host_available: Boolean(Bun.which(definition.executable)),
    container_configured: profiles.some((profile) => profile.runtime === definition.id && Boolean(profile.image)),
    capabilities: { acp: definition.transportPreference === "acp", resume: definition.supportsResume, stream: true },
  }));
}

function publicProfile(profile?: HarnessProfile, readSecret?: (ref: string) => string | undefined) {
  if (!profile) return null;
  const { secretRef, ...safeProfile } = profile;
  const configured = profile.role === "foreman"
    ? (profile.executionMode === "trusted_container"
      ? Boolean(profile.image && (foremanCredential(profile, readSecret) || profile.image))
      : Boolean(foremanCredential(profile, readSecret) || (!profile.image && resolveOfficialHarness(profile.runtime) && Bun.which(resolveOfficialHarness(profile.runtime)!.executable))))
    : Boolean(secretRef && (!readSecret || readSecret(secretRef)));
  const foremanRuntime = profile.role === "foreman"
    ? profile.executionMode === "native_host"
      ? { state: "native_unverified", reason: "本机官方 Harness 的仓库脚本执行能力尚未验证" }
      : profile.image
        ? { state: "trusted_container", reason: "Foreman 使用 digest 固定的受控容器" }
        : { state: "waiting_configuration", reason: "可信 Foreman 需要配置 digest 固定的 Harness 镜像" }
    : undefined;
  return {
    ...safeProfile,
    execution_mode: profile.executionMode,
    base_url: profile.baseUrl,
    session_timeout_s: profile.sessionTimeoutSeconds,
    thinking_effort: profile.thinkingEffort ?? (profile.role === "foreman" ? "max" : "medium"),
    context: profile.contextTokens ?? (profile.role === "foreman" ? 500000 : 256000),
    session_budget_usd: profile.sessionBudgetUsd ?? 1,
    daily_budget_usd: profile.dailyBudgetUsd ?? 10,
    max_turns: profile.maxTurns ?? 20,
    max_attempts: profile.maxAttempts ?? 2,
    backoff_s: profile.backoffSeconds ?? 60,
    api_key_configured: configured,
    ...(foremanRuntime ? { foreman_runtime: foremanRuntime, runtime_state: foremanRuntime.state, runtime_reason: foremanRuntime.reason } : {}),
    updated_at: profile.updatedAt,
    verification: profile.verification ? { ...profile.verification, checked_at: profile.verification.checkedAt } : undefined,
  };
}

function publicUnit(unit: any) {
  const sourceKind = typeof unit.metadata?.source_kind === "string" ? unit.metadata.source_kind : undefined;
  return { ...unit, metadata: portableValue(unit.metadata ?? {}), machineRef: unit.kind === "machine" ? sourceKind : undefined };
}

function publicUnitRun(run: any) {
  return {
    ...run,
    inputs: publicReferences(run.inputs),
    outputs: publicReferences(run.outputs),
    error: typeof run.error === "string" ? redactText(run.error) : run.error,
  };
}

function publicBusinessRun(run: any) {
  return { ...run, metadata: portableValue(run.metadata ?? {}) };
}

function publicBusinessSystem(system: any) {
  return { ...system, metadata: portableValue(system.metadata ?? {}) };
}

function publicConversion(conversion: any) {
  return {
    ...conversion,
    inputs: publicReferences(conversion.inputs),
    outputs: publicReferences(conversion.outputs),
    metadata: portableValue(conversion.metadata ?? {}),
    producer: portableValue(conversion.producer ?? {}),
  };
}

function publicProject(project: any) {
  return {
    ...project,
    metadata: portableValue(project.metadata ?? {}),
    sources: Array.isArray(project.sources)
      ? project.sources.map((source: any) => {
        const { localFolder: _localFolder, fileRef, ...portableSource } = source;
        return {
          ...portableSource,
          ...(fileRef === undefined ? {} : { fileRef: publicReference(fileRef) }),
          repoUrl: repositoryReference(source.repoUrl),
          metadata: portableValue(source.metadata ?? {}),
        };
      })
      : [],
    secretRequests: Array.isArray(project.secretRequests)
      ? project.secretRequests.map((request: any) => {
        const { vaultRef: _vaultRef, ...publicRequest } = request;
        return { ...publicRequest, metadata: portableValue(request.metadata ?? {}) };
      })
      : [],
  };
}

function publicArtifact(record: any) {
  return {
    ...record,
    metadata: portableValue(record.metadata ?? {}),
    files: Array.isArray(record.files) ? record.files.map(publicArtifactFile) : record.files,
    inputs: publicReferences(record.inputs),
    outputs: publicReferences(record.outputs),
    lineage: publicReferences(record.lineage),
    project_id: record.projectId,
    source_id: record.sourceId,
    record_type: record.recordType,
    validation_epoch: validationEpoch(record),
    created_at: record.createdAt,
  };
}

function validationEpoch(record: any): string | undefined {
  const metadata = record?.metadata && typeof record.metadata === "object" && !Array.isArray(record.metadata) ? record.metadata as Record<string, unknown> : {};
  const value = metadata.validation_epoch ?? metadata.epoch;
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function publicSecretRequest(secret: any) {
  const checks = Array.isArray(secret.metadata?.checks)
    ? secret.metadata.checks.filter((item: unknown): item is string => typeof item === "string" && item.trim().length > 0).map((item: string) => redactText(item))
    : [];
  return {
    id: secret.id,
    key: secret.name,
    configured: secret.status === "available",
    status: secret.status,
    usage_scope: secret.metadata.scope,
    kind: secret.metadata.kind,
    source_tool: secret.metadata.tool,
    agent_generated_checks: checks,
    request: {
      id: secret.id,
      project_id: secret.projectId,
      project_name: secret.projectName,
      status: secret.status,
      purpose: secret.metadata.purpose,
      scope: secret.metadata.scope,
    },
  };
}

function publicArtifactFile(file: unknown): Record<string, unknown> {
  const portable = portableValue(file);
  if (!portable || typeof portable !== "object" || Array.isArray(portable)) return {};
  const value = { ...(portable as Record<string, unknown>) };
  const ref = typeof value.ref === "string" ? value.ref : undefined;
  if (ref?.startsWith("file://")) value.ref = "[REDACTED]";
  const path = typeof value.path === "string" ? value.path : undefined;
  if (path === "[REDACTED]" || (path && /^(?:[A-Za-z]:[\\/]|[\\/]{1,2})/.test(path))) delete value.path;
  return value;
}

function publicReference(value: unknown): unknown {
  const portable = portableValue(value);
  if (typeof portable !== "string") return portable;
  if (/^(?:file|data|blob|javascript):/i.test(portable)
    || /^(?:[A-Za-z]:[\\/]|[\\/]{1,2})/.test(portable)) return "[REDACTED]";
  return portable;
}

function publicReferences(value: unknown): unknown[] {
  return Array.isArray(value) ? value.map(publicReference) : [];
}

function assetPageLimit(raw: string): number | undefined {
  return /^(?:[1-9][0-9]?|100)$/.test(raw) ? Number(raw) : undefined;
}

function decodeAssetCursor(token: string): AssetCursor | undefined {
  if (token.length > 4096 || !/^[A-Za-z0-9_-]+$/.test(token)) return;
  try {
    const value = JSON.parse(Buffer.from(token, "base64url").toString("utf8"));
    if (value?.v !== 1 || typeof value.createdAt !== "string" || value.createdAt.length === 0) return;
    if (value.source === "business" && typeof value.id === "string" && value.id.length > 0) {
      return { source: "business", createdAt: value.createdAt, id: value.id };
    }
    if (value.source === "legacy" && typeof value.rowId === "string" && /^-?[0-9]{1,19}$/.test(value.rowId)) {
      return { source: "legacy", createdAt: value.createdAt, rowId: value.rowId };
    }
  } catch { return; }
}

function webAssetPage(store: SqliteStore, limit: number, older?: AssetCursor) {
  return store.db.transaction(() => {
    const page = store.assetPage(limit, older);
    return {
      items: projectWebAssets(store, page.business, page.legacy),
      total: page.total,
      olderCursor: page.older ? Buffer.from(JSON.stringify({ v: 1, ...page.older })).toString("base64url") : null,
    };
  })();
}

function webAssets(store: SqliteStore, limit?: number) {
  const records = store.listAllArtifacts(limit);
  return projectWebAssets(store, records, store.legacyArtifacts(limit === undefined ? undefined : limit - records.length));
}

function projectWebAssets(store: SqliteStore, records: Artifact[], archived: Artifact[]) {
  const legacy = archived.map((record) => {
    const document = record.metadata?.document && typeof record.metadata.document === "object" ? record.metadata.document as Record<string, unknown> : {};
    return {
      id: record.id,
      type: typeof document.kind === "string" ? document.kind : record.kind,
      record_type: document.record_type === "material" ? "material" : "artifact",
      ref: typeof document.ref === "string" ? document.ref : record.ref,
      title: typeof document.title === "string" ? document.title : undefined,
      description: typeof document.description === "string" ? document.description : undefined,
      summary: typeof document.summary === "string" ? document.summary : undefined,
      purpose: typeof document.purpose === "string" ? document.purpose : undefined,
      status: typeof document.status === "string" ? document.status : undefined,
      validation_epoch: validationEpoch({ metadata: document.metadata }),
      files: Array.isArray(document.files) ? document.files.map(publicArtifactFile) : [],
      inputs: publicReferences(document.inputs),
      outputs: publicReferences(document.outputs),
      lineage: publicReferences(document.lineage),
      sealed: true,
      expired: false,
      created_at: record.createdAt,
      source: { node: typeof document.producer === "object" && document.producer ? (document.producer as Record<string, unknown>).operator : undefined },
    };
  });
  const grants = new Map(store.artifactFileGrants(null, records.map(({ id }) => id))
    .map(({ artifactId, position, digest }) => [`${artifactId}:${position}`, digest]));
  const business = records.map((record) => ({
    id: record.id,
    type: record.kind,
    record_type: record.recordType,
    ref: record.ref,
    title: record.title,
    description: record.description,
    summary: record.summary,
    purpose: record.purpose,
    status: record.status,
    validation_epoch: validationEpoch(record),
    files: (record.files ?? []).flatMap((file, position) => {
      if (typeof file?.ref !== "string") return [];
      const { ref, path, mediaType, digest } = file;
      const trustedDigest = grants.get(`${record.id}:${position}`);
      const download = trustedDigest
        ? `/api/v1/projects/${encodeURIComponent(record.projectId)}/artifacts/${encodeURIComponent(record.id)}/files/${trustedDigest}`
        : undefined;
      return [{ ...publicArtifactFile({ ref, path, mediaType, digest }), download }];
    }),
    inputs: publicReferences(record.inputs),
    outputs: publicReferences(record.outputs),
    lineage: publicReferences(record.lineage),
    sealed: ["sealed", "produced", "completed"].includes(record.status ?? ""),
    expired: false,
    created_at: record.createdAt,
    source: { node: record.producer?.operator, run: typeof record.metadata.run_id === "string" ? record.metadata.run_id : undefined },
  }));
  return [...legacy, ...business];
}

function portableValue(value: unknown, key = ""): any {
  if (/(secret|token|password|api[_-]?key|authorization|cookie|local[_-]?folder|host[_-]?path)/i.test(key)) return "[REDACTED]";
  if (typeof value === "string") {
    if (/(?:^|[_-])path$/i.test(key) && /^(?:[A-Za-z]:[\\/]|[\\/]{1,2})/.test(value)) return "[REDACTED]";
    if (/^file:/i.test(value)) return "[REDACTED]";
    return redactText(value);
  }
  if (Array.isArray(value)) return value.map((item) => portableValue(item));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, portableValue(item, name)]));
  return value;
}

/**
 * Project the Registry's Lines into the factory-level graph. Business systems
 * remain the compatibility projection when no Registry is configured. A Line
 * is the workarea identity at this level, while Station and Machine facts are
 * used to derive explicit material edges between Lines.
 */
async function factoryGraphProjection(store: SqliteStore, storageRoot: string) {
  const snapshot = await factoryRegistrySnapshot(store, storageRoot);
  if (!snapshot) return graphProjection(store, "factory", null);
  const businessRuns = businessRunFactsByLine(store, snapshot.keys("Line"));

  const lines = snapshot.keys("Line").map((lineKey, index) => {
    const document = snapshot.doc(lineKey);
    const spec = document?.spec && typeof document.spec === "object" ? document.spec as Record<string, unknown> : {};
    const metadata = document?.metadata && typeof document.metadata === "object" ? document.metadata as Record<string, unknown> : {};
    const runs = [...legacyRunsForLine(store, lineKey), ...(businessRuns.get(lineKey) ?? [])]
      .sort((left, right) => left.createdAt.localeCompare(right.createdAt) || left.id.localeCompare(right.id));
    const latest = runs.at(-1);
    const status = lineStatus(latest?.state);
    const steps = spec.steps && typeof spec.steps === "object" && !Array.isArray(spec.steps) ? Object.keys(spec.steps) : [];
    const references = lineReferences(snapshot, spec);
    return {
      id: lineKey,
      type: "workarea",
      group: "factory",
      label: typeof metadata.name === "string" && metadata.name.trim() ? metadata.name : lineName(lineKey),
      position: { x: 180 + (index % 3) * 300, y: 160 + Math.floor(index / 3) * 200 },
      status,
      metadata: {
        line: lineKey,
        brief: `${steps.length} 个执行步骤`,
        stations: references.stations,
        machines: references.machines,
        active: runs.filter((run) => ["PENDING", "RUNNING", "FAILING", "CANCELLING", "QUEUED"].includes(run.state.toUpperCase())).length,
        runs: runs.length,
        latest: latest?.id ?? null,
        exception: runs.filter((run) => ["FAILED", "FAILING"].includes(run.state.toUpperCase())).length,
      },
    };
  });
  const statusByLine = new Map(lines.map((node) => [node.id, String(node.status)]));
  const edges = factoryLineEdges(snapshot, lines.map((node) => node.id)).map((edge) => ({
    ...edge,
    status: statusByLine.get(String(edge.target)) === "exception" || statusByLine.get(String(edge.source)) === "exception"
      ? "exception" : statusByLine.get(String(edge.source)) === "running" || statusByLine.get(String(edge.target)) === "running"
        ? "running" : statusByLine.get(String(edge.target)) === "ready" ? "ready" : edge.status,
  }));
  return {
    scope: "factory",
    id: "factory",
    title: "工厂",
    nodes: lines,
    edges,
    metadata: {
      projection: "web_read_model.registry",
      static_source: "registry Line, Station and Machine",
      runtime_source: "legacy runs and BusinessRun records",
    },
  };
}

function lineReferences(snapshot: Snapshot, line: Record<string, unknown>): { stations: string[]; machines: string[] } {
  const stations = new Set<string>();
  const machines = new Set<string>();
  const steps = line.steps && typeof line.steps === "object" && !Array.isArray(line.steps) ? Object.values(line.steps) : [];
  for (const rawStep of steps) {
    if (!rawStep || typeof rawStep !== "object" || typeof (rawStep as any).station !== "string") continue;
    try {
      const stationKey = snapshot.key("Station", (rawStep as any).station);
      const station = snapshot.spec(stationKey) as Record<string, unknown>;
      stations.add(stationKey);
      if (typeof station.machine === "string") machines.add(snapshot.key("Machine", station.machine));
    } catch {
      // Registry validation reports unresolved references. Keep the projection
      // available for the remaining valid steps.
    }
  }
  return { stations: [...stations].sort(), machines: [...machines].sort() };
}

async function factoryRegistrySnapshot(store: SqliteStore, storageRoot: string): Promise<Snapshot | undefined> {
  const configured = readGovernedRegistry(storageRoot)?.registry;
  if (!configured) return undefined;
  let context: Context | undefined;
  try {
    context = Context.open(storageRoot, { clock: store.clock });
    const epochs = await EpochStore.open(context, { registry: configured });
    const active = epochs.pointers().active;
    if (active) return await epochs.materialize(active);
  } catch {
    // A non-Git example Registry or an uninitialized epoch falls through to
    // the configured working tree for compatibility with local installations.
  } finally {
    context?.close();
  }
  const candidates: string[] = [configured];
  // The governed Registry is the authoritative factory source. Avoid probing
  // business-system rows when it is absent, preserving the compatibility
  // projection's query shape for installations without a Registry.
  for (const candidate of [...new Set(candidates)]) {
    try {
      if (existsSync(candidate)) return await loadSnapshot(candidate);
    } catch {
      // A malformed or unavailable Registry must not make the Web read model
      // fail. The business-system compatibility projection is returned below.
    }
  }
  return undefined;
}

type LegacyLineRun = { id: string; line: string; state: string; createdAt: string; updatedAt: string };

function legacyRunsForLine(store: SqliteStore, line: string): LegacyLineRun[] {
  const database = store.legacy ?? store.db;
  const short = line.includes(":") ? line.slice(line.indexOf(":") + 1) : line;
  try {
    const available = database.query<{ name: string }>("SELECT name FROM sqlite_schema WHERE type='table' AND name='runs'").get();
    if (!available) return [];
    return database.query<any>("SELECT id,line,state,created_at,updated_at FROM runs WHERE line IN (?, ?) ORDER BY created_at, id").all(line, short).map((row: any) => ({
      id: String(row.id), line: String(row.line), state: String(row.state), createdAt: String(row.created_at), updatedAt: String(row.updated_at),
    }));
  } catch {
    return [];
  }
}

function lineStatus(state: string | undefined): string {
  if (!state) return "unknown";
  // FAILING is an exception state even though the run may still be active.
  const normalized = state.toUpperCase();
  if (["FAILED", "FAILING"].includes(normalized)) return "exception";
  if (["PENDING", "RUNNING", "CANCELLING", "QUEUED"].includes(normalized)) return normalized === "QUEUED" ? "waiting" : "running";
  if (["SUCCEEDED", "COMPLETED"].includes(normalized)) return "ready";
  return normalized.toLowerCase();
}

function businessRunFactsByLine(store: SqliteStore, lineKeys: string[]): Map<string, LegacyLineRun[]> {
  const found = new Map<string, LegacyLineRun[]>();
  const byRef = new Map(lineKeys.flatMap((key) => [[key, key], [key.slice(key.indexOf(":") + 1), key]]));
  for (const system of store.listBusinessSystems()) {
    const kernel = system.metadata.kernel;
    if (!kernel || typeof kernel !== "object" || Array.isArray(kernel)) continue;
    const line = (kernel as Record<string, unknown>).line;
    if (typeof line !== "string") continue;
    const lineKey = byRef.get(line);
    if (!lineKey) continue;
    const facts = store.listBusinessRuns(system.id).map((run) => ({ id: run.id, line: lineKey, state: run.status, createdAt: run.createdAt, updatedAt: run.updatedAt }));
    if (facts.length) found.set(lineKey, [...(found.get(lineKey) ?? []), ...facts]);
  }
  return found;
}

function lineName(lineKey: string): string {
  return lineKey.split(":", 2).at(-1)?.split("@", 1)[0] ?? lineKey;
}

function factoryLineEdges(snapshot: Snapshot, lineKeys: string[]) {
  const inputs = new Map<string, Set<string>>();
  const outputs = new Map<string, Set<string>>();
  for (const lineKey of lineKeys) {
    const line = snapshot.spec(lineKey) as Record<string, unknown>;
    const interfaceSpec = line.interface && typeof line.interface === "object" ? line.interface as Record<string, unknown> : {};
    inputs.set(lineKey, declaredPortTypes(interfaceSpec.inputs));
    const lineOutputs = new Set<string>();
    const steps = line.steps && typeof line.steps === "object" && !Array.isArray(line.steps) ? Object.values(line.steps) : [];
    for (const rawStep of steps) {
      if (!rawStep || typeof rawStep !== "object" || typeof (rawStep as any).station !== "string") continue;
      try {
        const station = snapshot.spec(snapshot.key("Station", (rawStep as any).station)) as Record<string, unknown>;
        for (const type of declaredPortTypes(station.outputs)) lineOutputs.add(type);
      } catch {
        // Invalid references are handled by Registry validation. Ignore one
        // unavailable station when producing a read-only projection.
      }
    }
    outputs.set(lineKey, lineOutputs);
  }
  const edges: Array<Record<string, unknown>> = [];
  for (const source of lineKeys) for (const target of lineKeys) {
    if (source === target) continue;
    const shared = [...(outputs.get(source) ?? [])].filter((type) => inputs.get(target)?.has(type)).sort();
    if (!shared.length) continue;
    edges.push({ id: `${source}:to:${target}`, source, target, kind: "material", condition: `类型为 ${shared[0]}`, status: "unknown", metadata: { types: shared } });
  }
  return edges;
}

function declaredPortTypes(value: unknown): Set<string> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return new Set();
  const found = new Set<string>();
  for (const declaration of Object.values(value as Record<string, unknown>)) {
    if (typeof declaration === "string" && declaration) found.add(declaration);
    else if (declaration && typeof declaration === "object" && typeof (declaration as any).type === "string") found.add((declaration as any).type);
  }
  return found;
}

async function registryWorkareaProjection(store: SqliteStore, storageRoot: string, lineKey: string) {
  const snapshot = await factoryRegistrySnapshot(store, storageRoot);
  if (!snapshot || !snapshot.keys("Line").includes(lineKey)) return undefined;
  const line = snapshot.spec(lineKey) as Record<string, unknown>;
  const steps = line.steps && typeof line.steps === "object" && !Array.isArray(line.steps)
    ? Object.entries(line.steps) : [];
  const runtimes = new Set<string>();
  const machines = new Set<string>();
  const internalSteps: Array<Record<string, unknown>> = [];
  for (const [stepName, rawStep] of steps) {
    if (!rawStep || typeof rawStep !== "object" || typeof (rawStep as any).station !== "string") continue;
    const stationKey = snapshot.key("Station", (rawStep as any).station);
    const station = snapshot.spec(stationKey) as Record<string, unknown>;
    const operator = station.operator && typeof station.operator === "object" ? station.operator as Record<string, unknown> : {};
    const runtime = typeof operator.runtime === "string" ? operator.runtime : typeof station.operator === "string" ? station.operator : "operator:" + stepName;
    const machine = typeof station.machine === "string" ? snapshot.key("Machine", station.machine) : "Machine:" + stepName;
    runtimes.add(runtime);
    machines.add(machine);
    internalSteps.push({ name: stepName, station: stationKey, operator: runtime, machine, status: "unknown" });
  }
  const runFacts = [...legacyRunsForLine(store, lineKey), ...(businessRunFactsByLine(store, [lineKey]).get(lineKey) ?? [])]
    .sort((left, right) => left.createdAt.localeCompare(right.createdAt) || left.id.localeCompare(right.id));
  const latest = runFacts.at(-1);
  const status = lineStatus(latest?.state);
  for (const step of internalSteps) step.status = status;
  const operatorId = "Operator:" + lineKey;
  const machineId = "Machine:" + lineKey;
  const nodes: Array<Record<string, unknown>> = [
    { id: operatorId, type: "operator", group: lineKey, label: "Operator", position: { x: 170, y: 160 }, status, metadata: { line: lineKey, runtimes: [...runtimes].sort(), step_count: internalSteps.length, internal_steps: internalSteps, recent_status: latest?.state ?? "unknown" } },
    { id: machineId, type: "machine", group: lineKey, label: lineName(lineKey), position: { x: 470, y: 160 }, status, metadata: { line: lineKey, machines: [...machines].sort(), step_count: internalSteps.length, internal_steps: internalSteps, recent_status: latest?.state ?? "unknown" } },
  ];
  const edges: Array<Record<string, unknown>> = [{ id: operatorId + ":to:" + machineId, source: operatorId, target: machineId, kind: "material", condition: "Operator 驱动项目机器", status, metadata: { line: lineKey, steps: internalSteps.map(({ name, station }) => ({ name, station })) } }];
  return {
    scope: "workarea", id: lineKey, title: lineName(lineKey), nodes, edges,
    metadata: { projection: "web_read_model.registry_line", line: lineKey, source: "Line, Station, Machine, runs", runtime_status: status },
  };
}

function graphProjection(store: SqliteStore, scope: string, workarea: string | null) {
  if (scope === "workarea" && workarea) {
    const selectedSystem = store.getBusinessSystem(workarea);
    const units = selectedSystem ? store.listBusinessUnits(selectedSystem.id) : [];
    const operatorUnits = units.filter((unit) => unit.kind === "operator");
    const selectedProjects = operatorUnits.length ? [] : store.listProjects(workarea);
    // A workarea link can also refer directly to a project from an earlier layout.
    if (!operatorUnits.length) {
      const directProject = store.getProject(workarea);
      if (directProject && directProject.businessSystemId !== workarea) {
        selectedProjects.push(directProject);
        selectedProjects.sort((a, b) => a.createdAt.localeCompare(b.createdAt) || a.id.localeCompare(b.id));
      }
    }
    const storedGraph = selectedSystem?.metadata.graph && typeof selectedSystem.metadata.graph === "object" ? selectedSystem.metadata.graph as { nodes?: unknown; edges?: unknown } : undefined;
    const storedNodes = Array.isArray(storedGraph?.nodes) ? storedGraph.nodes.filter((node): node is Record<string, any> => Boolean(node && typeof node === "object" && typeof (node as any).id === "string")) : [];
    const storedById = new Map<string, Record<string, any>>();
    for (const node of storedNodes) if (!storedById.has(node.id)) storedById.set(node.id, node);
    const profiles = operatorUnits.length ? store.listHarnessProfiles() : [];
    const nodes = units.length ? units.map((unit, index) => {
      const saved = storedById.get(unit.id);
      return {
      id: unit.id,
      type: unit.kind,
      group: unit.projectId ?? unit.businessSystemId,
      label: unit.kind === "operator" ? "Operator" : unit.name,
      position: saved?.position && typeof saved.position === "object" ? saved.position : { x: unit.kind === "operator" ? 170 : 470, y: 130 + Math.floor(index / 2) * 180 },
      status: unit.status === "pending" ? "waiting" : unit.status,
      metadata: { project: unit.name, harness: unit.harnessRuntime, model: unit.model, source_kind: unit.metadata.source_kind },
      ...(unit.kind === "operator" ? { operatorBinding: operatorBindingView(unit, profiles) } : {}),
      };
    }) : selectedProjects.flatMap((project, index) => [
      { id: `${project.id}:operator`, type: "operator", group: project.id, label: "Operator", position: { x: 170, y: 130 + index * 180 }, status: "ready", metadata: { project: project.name, harness: "official" } },
      { id: `${project.id}:machine`, type: "machine", group: project.id, label: project.name, position: { x: 470, y: 130 + index * 180 }, status: "ready", metadata: { project: project.name, sources: project.sources.length } },
    ]);
    const validNodeIds = new Set(nodes.map((node) => node.id));
    const alias = new Map([["operator", units.find((unit) => unit.kind === "operator")?.id], ["machine", units.find((unit) => unit.kind === "machine")?.id]]);
    const storedEdges = Array.isArray(storedGraph?.edges) ? storedGraph.edges.flatMap((edge): Array<Record<string, any>> => {
      if (!edge || typeof edge !== "object" || typeof (edge as any).source !== "string" || typeof (edge as any).target !== "string") return [];
      const source = alias.get((edge as any).source) ?? (edge as any).source;
      const target = alias.get((edge as any).target) ?? (edge as any).target;
      return typeof source === "string" && typeof target === "string" && validNodeIds.has(source) && validNodeIds.has(target) ? [{ ...edge, source, target }] : [];
    }) : [];
    const authoredEdges = Array.isArray(storedGraph?.edges);
    const edges = authoredEdges ? storedEdges.map((edge) => ({ id: typeof edge.id === "string" ? edge.id : `${edge.source}:${edge.target}`, source: edge.source, target: edge.target, kind: edge.kind ?? "material", condition: edge.condition, status: edge.status ?? "ready" })) : operatorUnits.length ? operatorUnits.map((unit) => ({ id: `${unit.id}:machine-to-operator`, source: `${unit.projectId}:machine`, target: unit.id, kind: "material", condition: "项目物料", status: "ready" })) : selectedProjects.map((project) => ({ id: `${project.id}:machine-to-operator`, source: `${project.id}:machine`, target: `${project.id}:operator`, kind: "material", condition: "项目物料", status: "ready" }));
    return { scope: "workarea", id: workarea, title: selectedSystem?.name ?? workarea, nodes, edges };
  }
  const systems = store.listBusinessSystemSummaries();
  const projectCounts = store.countProjectsByBusinessSystem();
  return { scope: "factory", id: "factory", title: "工厂", nodes: systems.map((system, index) => ({ id: system.id, type: "workarea", group: "factory", label: system.name, position: { x: 180 + (index % 3) * 300, y: 160 + Math.floor(index / 3) * 200 }, status: system.status === "active" ? "ready" : "unknown", metadata: { description: system.description, project_count: projectCounts.get(system.id) ?? 0 } })), edges: [] };
}

function validateDag(value: unknown): string | undefined {
  if (!value || typeof value !== "object") return "graph must be an object";
  const graph = value as { nodes?: unknown; edges?: unknown };
  if (!Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) return "graph requires nodes and edges arrays";
  const incoming = new Map<string, number>();
  for (const node of graph.nodes) {
    if (!node || typeof node !== "object" || typeof (node as any).id !== "string" || !(node as any).id) return "every graph node requires an id";
    if (incoming.has((node as any).id)) return "graph node ids must be unique";
    incoming.set((node as any).id, 0);
  }
  const next = new Map<string, string[]>();
  for (const edge of graph.edges) {
    if (!edge || typeof edge !== "object" || typeof (edge as any).source !== "string" || typeof (edge as any).target !== "string") return "every graph edge requires source and target";
    const source = (edge as any).source as string;
    const target = (edge as any).target as string;
    const count = incoming.get(target);
    if (!incoming.has(source) || count === undefined) return "graph edges must reference existing nodes";
    const successors = next.get(source);
    if (successors) successors.push(target);
    else next.set(source, [target]);
    incoming.set(target, count + 1);
  }
  const ready: string[] = [];
  for (const [id, count] of incoming) if (count === 0) ready.push(id);
  for (let index = 0; index < ready.length; index += 1) {
    for (const target of next.get(ready[index]) ?? []) {
      const count = incoming.get(target)! - 1;
      incoming.set(target, count);
      if (count === 0) ready.push(target);
    }
  }
  return ready.length === incoming.size ? undefined : "graph must be acyclic";
}

function validateBusinessGraph(store: SqliteStore, businessSystemId: string, value: unknown): string | undefined {
  if (!store.getBusinessSystem(businessSystemId)) return "business system was not found";
  const generic = validateDag(value);
  if (generic) return generic;
  const graph = value as { nodes: Array<Record<string, unknown>>; edges: Array<Record<string, unknown>> };
  const units = store.listBusinessUnits(businessSystemId);
  if (!units.length) return undefined;
  // Pre-M18 authored graphs may omit node types. They can use only the two
  // stable presentation aliases below, while every other node is rejected.
  const byId = new Map(units.map((unit) => [unit.id, unit]));
  const aliases = new Set(["operator", "machine"]);
  for (const node of graph.nodes) {
    const id = node.id;
    if (typeof id !== "string" || (!byId.has(id) && !aliases.has(id))) return "graph nodes must reference business units in the current system";
    const type = node.type;
    if (type !== undefined && aliases.has(id) && type !== id) return "graph node type must match its business unit alias";
    if (type !== undefined && byId.has(id) && type !== byId.get(id)!.kind) return "graph node type must match its business unit";
  }
  for (const edge of graph.edges) {
    if (edge.source === edge.target) return "graph edges cannot connect a node to itself";
    if (edge.kind !== undefined && !["material", "condition", "default", "aggregate"].includes(String(edge.kind))) return "graph edge kind must be material, condition, default, or aggregate";
    if (typeof edge.source !== "string" || typeof edge.target !== "string" || (!byId.has(edge.source) && !aliases.has(edge.source)) || (!byId.has(edge.target) && !aliases.has(edge.target))) {
      return "graph edges must reference business units in the current system";
    }
  }
  return undefined;
}

async function readJson(request: Request): Promise<Record<string, any>> {
  try { const value = await request.json(); return value && typeof value === "object" ? value as Record<string, any> : {}; } catch { return {}; }
}

async function saveProjectUploads(root: string, warehouse: Warehouse, store: SqliteStore, files: File[]) {
  const temp = join(root, "tmp");
  mkdirSync(temp, { recursive: true });
  const stage = mkdtempSync(join(temp, "intake-"));
  try {
    const registered: Array<{ name: string; digest: string; ref: string }> = [];
    for (const [index, file] of files.entries()) {
      const path = join(stage, String(index));
      await Bun.write(path, file);
      const digest = await warehouse.putFile(path);
      store.db.query("INSERT OR IGNORE INTO intake_uploads(digest,size) VALUES (?,?)").run(digest, file.size);
      registered.push({ name: file.name, digest, ref: `cas://${digest}` });
    }
    return registered;
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
}

function webLogs(store: SqliteStore, limit: number, since?: number) {
  store.syncLegacyEvents();
  const recent = store.recentEvents(limit);
  const bridged = recent.filter((event) => event.type.startsWith("legacy.")
    && event.data && typeof event.data === "object" && !Array.isArray(event.data)
    && (event.data as Record<string, unknown>).source === "legacy");
  const bridgedIds = new Set(bridged.map((event) => String((event.data as Record<string, unknown>).legacy_id ?? "")));
  return [...store.legacyEvents(limit).filter((event) => !bridgedIds.has(event.id)), ...recent]
    .sort((a, b) => a.createdAt.localeCompare(b.createdAt))
    .filter((event) => since === undefined || event.seq > since)
    .slice(-limit)
    .map((event) => ({ id: event.id, seq: event.seq, cursor: String(event.seq), kind: event.type, at: event.createdAt, data: portableValue(event.data) }));
}

function scopedLogs(store: SqliteStore, limit: number, principal: AuthPrincipal | undefined, since?: number) {
  return webLogs(store, Math.max(limit * 4, limit), since)
    .filter((event) => scopedEventVisible(principal!, store, {
      id: String(event.id), seq: Number(event.seq ?? 0), type: String(event.kind),
      data: event.data, createdAt: String(event.at ?? ""),
    }))
    .slice(-limit);
}

async function constitutionProjection(root: string) {
  const configured = readGovernedRegistry(root);
  if (!configured) return { available: false, status: "unconfigured", epoch: null, version: null, recent_change: null };
  let context: Context | undefined;
  try {
    context = Context.open(root);
    const epochs = await EpochStore.open(context, { registry: configured.registry });
    const active = epochs.pointers().active;
    if (!active) return { available: false, status: "uninitialized", epoch: null, version: null, recent_change: null };
    const row = context.db.query<{ kind: string; data: string; at: string }>(
      "SELECT kind,data,at FROM events WHERE kind LIKE 'epoch.%' ORDER BY seq DESC LIMIT 1",
    ).get();
    let recentChange: Record<string, unknown> | null = null;
    if (row) {
      try { recentChange = { kind: row.kind, at: row.at, ...portableValue(JSON.parse(row.data)) }; }
      catch { recentChange = { kind: row.kind, at: row.at }; }
    }
    return { available: true, status: "active", epoch: active, version: active, recent_change: recentChange };
  } catch {
    return { available: false, status: "unavailable", epoch: null, version: null, recent_change: null };
  } finally {
    context?.close();
  }
}

function scopedEventVisible(principal: AuthPrincipal, store: SqliteStore, event: Event): boolean {
  if (!principal.scoped || principal.role === "admin") return true;
  const data = event.data && typeof event.data === "object" && !Array.isArray(event.data) ? event.data as Record<string, unknown> : {};
  if (principal.role === "foreman" && (event.type.startsWith("foreman.") || event.type.startsWith("talk.foreman."))) {
    const envelopeId = typeof data.inboxId === "string" ? data.inboxId : typeof data.inbox_id === "string" ? data.inbox_id : event.type === "foreman.message.queued" || event.type === "foreman.message.claimed" || event.type === "foreman.message.done" ? typeof data.id === "string" ? data.id : undefined : undefined;
    const envelope = envelopeId ? store.getForemanMessage(envelopeId) : undefined;
    return Boolean(envelope && talkEnvelopeVisible(principal, store, envelope));
  }
  const messageMetadata = data.metadata && typeof data.metadata === "object" && !Array.isArray(data.metadata) ? data.metadata as Record<string, unknown> : {};
  const systemId = ["businessSystemId", "business_system_id", "systemId", "system_id"].find((key) => typeof data[key] === "string") as string | undefined;
  const projectId = ["projectId", "project_id"].find((key) => typeof data[key] === "string") as string | undefined;
  const scopedSystemId = systemId ?? (typeof messageMetadata.business_system_id === "string" ? messageMetadata.business_system_id : undefined);
  const scopedProjectId = projectId ?? (typeof messageMetadata.project_id === "string" ? messageMetadata.project_id : undefined);
  if (scopedSystemId) return canAccessBusinessSystem(principal, store, scopedSystemId);
  if (scopedProjectId) return canAccessProject(principal, store, scopedProjectId);
  const runId = ["runId", "run_id"].find((key) => typeof data[key] === "string") as string | undefined;
  if (runId) {
    const run = store.getBusinessRun(runId);
    return Boolean(run && canAccessSystem(principal, run.businessSystemId));
  }
  if (event.type.startsWith("business-system.") && typeof data.id === "string") return canAccessSystem(principal, data.id);
  if (event.type.startsWith("project.") && typeof data.id === "string") return canAccessProject(principal, store, data.id);
  return false;
}

function eventsResponse(events: EventBus, cursor: string | null, signal: AbortSignal, cors: Record<string, string> = {}, visible?: (event: Event) => boolean): Response {
  const startSequence = Number(cursor);
  let sequence = Number.isSafeInteger(startSequence) && startSequence >= 0 ? startSequence : 0;
  let page: Event[] = [];
  let index = 0;
  let closed = false;
  let wake: (() => void) | undefined;
  let cleanup = () => {};
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const abort = () => { cleanup(); controller.close(); };
      const unsubscribe = events.subscribe(() => wake?.());
      cleanup = () => {
        closed = true;
        unsubscribe();
        signal.removeEventListener("abort", abort);
        page = [];
        wake?.();
      };
      if (signal.aborted) abort();
      else signal.addEventListener("abort", abort, { once: true });
    },
    async pull(controller) {
      try {
        while (!closed) {
          if (index === page.length) {
            page = events.after(sequence, 128);
            index = 0;
          }
          const event = page[index];
          if (event) {
            index++;
            sequence = event.seq;
            if (visible && !visible(event)) continue;
            const data = event.data && typeof event.data === "object" ? { ...event.data, type: event.type } : { type: event.type, value: event.data };
            controller.enqueue(encoder.encode(`id: ${event.seq}\nevent: mao.event\ndata: ${JSON.stringify(portableValue(data))}\n\n`));
            return;
          }
          // Local publishers wake immediately. A CLI writing the same database cannot notify this process.
          await new Promise<void>((resolve) => {
            const timer = setTimeout(resolve, 250);
            wake = () => { clearTimeout(timer); resolve(); };
          });
          wake = undefined;
        }
      } catch (error) {
        cleanup();
        controller.error(error);
      }
    },
    cancel() { cleanup(); },
  });
  return new Response(stream, { headers: { "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache", "connection": "keep-alive", ...cors } });
}

export function startServer(options: BackendAppOptions & { port?: number; hostname?: string } = {}) {
  const app = createApp(options);
  let server: ReturnType<typeof Bun.serve> | undefined;
  try {
    server = Bun.serve({
      port: options.port ?? Number(process.env.PORT ?? 3000),
      hostname: options.hostname ?? "127.0.0.1",
      fetch(request, server) {
        if (request.method === "GET" && new URL(request.url).pathname === "/api/v1/events") server.timeout(request, 0);
        return app.fetch(request);
      },
    });
    const listening = server;
    const dataVersion = app.store.db.query<{ data_version: number }>("PRAGMA data_version");
    let version = dataVersion.get()?.data_version;
    if (options.dispatchWork !== false) app.operator.reconcileInterruptedRuns();
    app.recoverQueuedWork();
    const externalWork = setInterval(() => {
      try {
        const current = dataVersion.get()?.data_version;
        if (current === version) return;
        app.recoverQueuedWork();
        version = current;
      } catch (error) {
        console.error("External work recovery failed", redactText(error instanceof Error ? error.message : String(error)));
      }
    }, 250);
    return {
      ...app,
      server: listening,
      stop: () => { clearInterval(externalWork); listening.stop(); app.stop(); },
      shutdown: () => { clearInterval(externalWork); listening.stop(); return app.shutdown(); },
    };
  } catch (error) {
    server?.stop();
    app.stop();
    throw error;
  }
}

