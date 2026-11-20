import { EventBus } from "./event-bus";
import { SqliteStore } from "./db";
import { VaultStore } from "./vault";
import { exportBusinessSystem, importBusinessSystem } from "./business-portability";
import { existsSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { dirname, extname, join, normalize, resolve } from "node:path";
import { RandomIds, type Clock } from "./legacy/foundation";
import { Warehouse } from "./legacy/warehouse";
import type { BusinessConversion, BusinessSystem, Event } from "./models";
import type { RuntimeHealth } from "../runtime/podman";
import { selectedContainerProvider } from "../runtime/selected";
import { ForemanCoordinator } from "./foreman";
import { verifyHarness } from "./harness-verification";
import { OperatorCoordinator, projectCredentialName } from "./operator";
import { operatorBindingView, parseOperatorBinding } from "./operator-binding";
import { harnessEndpointEnv, officialHarness, officialHarnesses as harnessDefinitions, type OfficialSpawn } from "./harness";
import type { AcpSpawn } from "./acp";

const headers = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "access-control-allow-origin": "*" };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers });
const SECRET_TEXT = /(api[_-]?key|secret|token|password|authorization|cookie)\s*[:=]\s*\S+/gi;
const redactText = (value: string) => value.replace(SECRET_TEXT, (match) => `${match.split(/[:=]/, 1)[0]}=[REDACTED]`);

export interface BackendApp {
  store: SqliteStore;
  events: EventBus;
  vault: VaultStore;
  foreman: ForemanCoordinator;
  operator: OperatorCoordinator;
  recoverQueuedWork(): void;
  fetch(request: Request): Response | Promise<Response>;
  stop(): void;
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
  foreman?: { cwd?: string; spawn?: OfficialSpawn; acpSpawn?: AcpSpawn };
  operator?: { cwd?: string; spawn?: OfficialSpawn; acpSpawn?: AcpSpawn };
  harnessVerificationTimeoutMs?: number;
  dispatchWork?: boolean;
  clock?: Clock;
  uuid?: () => string;
}

export function createApp(options: BackendAppOptions = {}): BackendApp {
  const store = new SqliteStore(options.dbPath, options.legacyPath, { clock: options.clock, uuid: options.uuid });
  const events = new EventBus(store);
  const eventStreams = new AbortController();
  const vault = options.vault ?? new VaultStore(undefined, undefined, store.clock);
  const storageRoot = resolve(options.storageRoot ?? process.env.FACTORY_ROOT ?? dirname(process.env.FACTORY_DB_PATH ?? ".factory/factory.db"));
  let uploadWarehouse: Warehouse | undefined;
  const uploads = () => uploadWarehouse ??= new Warehouse(storageRoot, store.db, store.clock, new RandomIds());
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
    const health = await ensureRuntimeHealth();
    if (!health.available) throw new Error(`${provider} runtime unavailable: ${health.reason ?? "unknown"}`);
  };
  const verificationJobs = new Map<string, AbortController>();
  const databasePath = (path: string) => path === ":memory:" ? path : resolve(path);
  const foreman = new ForemanCoordinator({
    store, events, ...options.foreman, ensureRuntime,
    factoryEnv: { FACTORY_ROOT: storageRoot, FACTORY_DB_PATH: databasePath(store.legacyPath), BUSINESS_DB_PATH: databasePath(store.dbPath) },
  });
  let stopped = false;
  const runInBackground = (work: Promise<void>) => {
    void work.catch((error) => {
      if (!stopped) console.error("Background dispatch failed", redactText(error instanceof Error ? error.message : String(error)));
    });
  };
  let dispatchOperatorRun = (_runId: string) => {};
  const operator = new OperatorCoordinator({ store, events, cwd: options.operator?.cwd, spawn: options.operator?.spawn, acpSpawn: options.operator?.acpSpawn, onProgress: (runId) => dispatchOperatorRun(runId), ensureRuntime, readSecret: (ref) => vault.get(ref) });
  const hasForeman = () => store.listHarnessProfiles().some((profile) => profile.role === "foreman");
  const dispatchForemanIfConfigured = (id: string) => {
    if (options.dispatchWork !== false && hasForeman()) runInBackground(foreman.dispatch(id));
  };
  dispatchOperatorRun = (runId: string) => {
    if (options.dispatchWork === false) return;
    for (const unitRun of store.listReadyUnitRuns(runId)) runInBackground(operator.dispatch(runId, unitRun.id));
  };
  const recoverQueuedWork = () => {
    if (hasForeman()) {
      for (const id of store.listQueuedForemanMessageIds()) runInBackground(foreman.dispatch(id));
    }
    for (const id of store.listRecoverableBusinessRunIds()) dispatchOperatorRun(id);
  };
  const fetch = async (request: Request): Promise<Response> => {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: { ...headers, "access-control-allow-methods": "GET,POST,OPTIONS", "access-control-allow-headers": "content-type,last-event-id" } });
    if (url.pathname === "/healthz" && request.method === "GET") return response({ ok: true });
    if (url.pathname === "/api/v1/runtime" && request.method === "GET") {
      const health = await runtimeHealth();
      return response({ provider, health, dependency: runtimeDependency(health, provider) });
    }
    if (url.pathname === "/api/v1/runtime/ensure-ready" && request.method === "POST") {
      if (!runtime.ensureReady) return response({ error: "runtime readiness is unavailable" }, 501);
      let health: RuntimeHealth;
      try {
        health = await ensureRuntimeHealth();
      } catch (error) {
        health = runtimeCache?.health ?? await runtime.health();
        runtimeCache = { at: store.clock.monotonic(), health };
        const dependency = runtimeDependency(health, provider);
        events.publish(`runtime.${provider}.checked`, dependency);
        return response({ provider, health, dependency, message: dependency.message, error: error instanceof Error ? error.message : "runtime unavailable" }, 503);
      }
      events.publish(`runtime.${provider}.checked`, runtimeDependency(health, provider));
      return response({ provider, health, dependency: runtimeDependency(health, provider) }, health.available ? 200 : 503);
    }
    if (url.pathname === "/api/v1/business-systems/import" && request.method === "POST") {
      const document = await readJson(request);
      try {
        const imported = importBusinessSystem(store, document, validateDag);
        events.publish("business-system.imported", { id: imported.id, schema: document.schema });
        return response(exportBusinessSystem(store, imported.id, () => graphProjection(store, "workarea", imported.id)), 201);
      } catch (error) {
        return response({ error: error instanceof Error ? error.message : "business system document is invalid" }, 400);
      }
    }
    if (url.pathname === "/api/v1/business-systems" && request.method === "GET") return response(store.listBusinessSystems());
    if (url.pathname === "/api/v1/business-systems" && request.method === "POST") {
      const body = await readJson(request);
      if (!body.name || typeof body.name !== "string") return response({ error: "name is required" }, 400);
      const system = store.createBusinessSystem(body as Partial<BusinessSystem> & { name: string });
      events.publish("business-system.created", system);
      return response(system, 201);
    }
    const businessSystemMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)$/);
    if (businessSystemMatch && request.method === "GET") {
      const system = store.getBusinessSystem(businessSystemMatch[1]);
      if (!system) return response({ error: "business system was not found" }, 404);
      const projects = store.listProjects(system.id);
      const artifacts = store.listBusinessSystemArtifacts(system.id);
      return response({ ...system, projects: projects.map(publicProject), artifacts: artifacts.map(publicArtifact), materials: artifacts.filter((item) => item.recordType === "material").map(publicArtifact) });
    }
    const graphMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)\/graph$/);
    if (graphMatch && request.method === "GET") {
      const system = store.getBusinessSystem(graphMatch[1]);
      if (!system) return response({ error: "business system was not found" }, 404);
      return response(system.metadata.graph ?? graphProjection(store, "workarea", system.id));
    }
    if (graphMatch && request.method === "POST") {
      const body = await readJson(request);
      if (!body.graph || typeof body.graph !== "object") return response({ error: "graph is required" }, 400);
      const graphError = validateDag(body.graph);
      if (graphError) return response({ error: graphError }, 400);
      const system = store.updateBusinessSystemMetadata(graphMatch[1], { graph: body.graph, graph_updated_by: body.by ?? "foreman" });
      if (!system) return response({ error: "business system was not found" }, 404);
      events.publish("business-system.graph.updated", { id: system.id, graph: body.graph });
      return response(system, 200);
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
      return response(store.listBusinessConversions(conversionMatch[1]));
    }
    if (conversionMatch && request.method === "POST") {
      const body = await readJson(request);
      if (typeof body.kind !== "string" || typeof body.summary !== "string") return response({ error: "kind and summary are required" }, 400);
      if (body.inputs !== undefined && (!Array.isArray(body.inputs) || body.inputs.some((item: unknown) => typeof item !== "string"))) return response({ error: "inputs must be string references" }, 400);
      if (body.outputs !== undefined && (!Array.isArray(body.outputs) || body.outputs.some((item: unknown) => typeof item !== "string"))) return response({ error: "outputs must be string references" }, 400);
      if (!store.getBusinessSystem(conversionMatch[1])) return response({ error: "business system was not found" }, 404);
      const conversion = store.createBusinessConversion({ businessSystemId: conversionMatch[1], kind: body.kind, summary: body.summary, status: body.status === "running" || body.status === "completed" || body.status === "failed" ? body.status : "planned", inputs: body.inputs ?? [], outputs: body.outputs ?? [], producer: body.producer && typeof body.producer === "object" ? body.producer : undefined, metadata: body.metadata && typeof body.metadata === "object" ? body.metadata : {} });
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
      return response(store.listBusinessRunsWithUnits(runsMatch[1]).map((run) => ({ ...run, units: run.units.map(publicUnitRun) })));
    }
    if (runsMatch && request.method === "POST") {
      const body = await readJson(request);
      const inputs = Array.isArray(body.inputs) ? body.inputs.filter((item: unknown): item is string => typeof item === "string") : [];
      const dispatch = body.dispatch !== false;
      const run = store.createBusinessRun(runsMatch[1], typeof body.trigger === "string" ? body.trigger : "manual", body.metadata && typeof body.metadata === "object" ? body.metadata : {}, inputs, dispatch);
      if (!run) return response({ error: "business system was not found" }, 404);
      events.publish("business-run.created", { ...run, units: store.listUnitRuns(run.id) });
      if (run.status === "queued" && dispatch) dispatchOperatorRun(run.id);
      return response({ ...run, units: store.listUnitRuns(run.id).map(publicUnitRun) }, 201);
    }
    const runMatch = url.pathname.match(/^\/api\/v1\/runs\/([^/]+)$/);
    if (runMatch && request.method === "GET") {
      const run = store.getBusinessRun(runMatch[1]);
      if (!run) return response({ error: "business run was not found" }, 404);
      return response({ ...run, units: store.listUnitRuns(run.id).map(publicUnitRun) });
    }
    const dispatchRunMatch = url.pathname.match(/^\/api\/v1\/runs\/([^/]+)\/dispatch$/);
    if (dispatchRunMatch && request.method === "POST") {
      const run = store.getBusinessRun(dispatchRunMatch[1]);
      if (!run) return response({ error: "business run was not found" }, 404);
      store.authorizeBusinessRunDispatch(run.id);
      dispatchOperatorRun(run.id);
      return response({ state: "dispatching", run_id: run.id }, 202);
    }
    if (url.pathname === "/api/v1/projects" && request.method === "GET") return response(store.listProjects().map(publicProject));
    if (url.pathname === "/api/v1/projects/intake" && request.method === "POST") {
      let body: Record<string, any>;
      if (request.headers.get("content-type")?.startsWith("multipart/form-data")) {
        let form: FormData;
        try { form = await request.formData(); }
        catch { return response({ error: "invalid project upload" }, 400); }
        body = {};
        for (const field of ["name", "repo_url", "local_folder", "business_system_id"]) {
          const value = form.get(field);
          if (typeof value === "string" && value.trim()) body[field] = value.trim();
        }
        const files = form.getAll("files").filter((file): file is File => typeof file !== "string" && Boolean(file.name));
        body.uploaded_file_refs = form.getAll("uploaded_file_refs").filter((ref): ref is string => typeof ref === "string" && Boolean(ref.trim()));
        if (files.length) body.files = await saveProjectUploads(storageRoot, uploads(), store, files);
      } else body = await readJson(request);
      if (body.secret_requests !== undefined && (!Array.isArray(body.secret_requests)
        || body.secret_requests.some((item: any) => !item || !projectCredentialName(item.name)))) {
        return response({ error: "project credential requests are invalid" }, 400);
      }
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
        return { createdSystem, project, intakeMessage, intakeEnvelope };
      }).immediate();
      if (!recorded) return response({ error: "business system was not found" }, 404);
      const { createdSystem, project, intakeMessage, intakeEnvelope } = recorded;
      if (createdSystem) events.publish("business-system.created", createdSystem);
      events.publish("project.intake.created", publicProject(project));
      events.publish("talk.message.created", intakeMessage);
      events.publish("foreman.message.queued", intakeEnvelope);
      dispatchForemanIfConfigured(intakeEnvelope.id);
      return response({ ...publicProject(project), project_id: project.id, state: "accepted", foreman_inbox_id: intakeEnvelope.id }, 201);
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
    const artifactMatch = url.pathname.match(/^\/api\/v1\/projects\/([^/]+)\/artifacts$/);
    if (artifactMatch && request.method === "GET") return response(store.listArtifacts(artifactMatch[1]).map(publicArtifact));
    if (artifactMatch && request.method === "POST") {
      const body = await readJson(request);
      if (typeof body.kind !== "string" || typeof body.ref !== "string" || typeof body.summary !== "string") return response({ error: "kind, ref, and summary are required" }, 400);
      if (body.record_type !== undefined && body.record_type !== "artifact" && body.record_type !== "material") return response({ error: "record_type must be artifact or material" }, 400);
      if (body.files !== undefined && (!Array.isArray(body.files) || body.files.some((file: unknown) => !file || typeof file !== "object" || typeof (file as any).ref !== "string"))) return response({ error: "files must contain file references" }, 400);
      const project = store.getProject(artifactMatch[1]);
      if (!project) return response({ error: "project was not found" }, 404);
      const metadata = { ...body.metadata, record_type: body.record_type, title: body.title, description: body.description, summary: body.summary, purpose: body.purpose, status: body.status, producer: body.producer, files: body.files, inputs: body.inputs, outputs: body.outputs, lineage: body.lineage };
      const artifact = store.addArtifact({ projectId: project.id, sourceId: typeof body.source_id === "string" ? body.source_id : undefined, recordType: body.record_type, kind: body.kind, ref: body.ref, title: typeof body.title === "string" ? body.title : undefined, description: typeof body.description === "string" ? body.description : undefined, status: typeof body.status === "string" ? body.status : undefined, producer: body.producer && typeof body.producer === "object" ? body.producer : undefined, summary: body.summary, purpose: typeof body.purpose === "string" ? body.purpose : undefined, files: body.files, inputs: body.inputs, outputs: body.outputs, lineage: body.lineage, metadata });
      events.publish("artifact.created", artifact);
      return response(publicArtifact(artifact), 201);
    }
    if (url.pathname === "/api/v1/talk" && request.method === "POST") {
      const body = await readJson(request);
      const text = typeof body.body === "string" ? body.body : body.text;
      if (typeof text !== "string" || text.length === 0) return response({ error: "body is required" }, 400);
      const message = store.addTalkMessage({ ...body, body: redactText(text) });
      store.bridgeHumanTalk(message.id, message.body);
      const envelope = store.queueForemanMessage(message.id, message.body);
      events.publish("talk.message.created", message);
      events.publish("foreman.message.queued", envelope);
      dispatchForemanIfConfigured(envelope.id);
      return response({ state: "accepted", message }, 201);
    }
    if (url.pathname === "/api/v1/talk" && request.method === "GET") return response([...store.legacyTalk(), ...store.listTalkMessages()]);
    if (url.pathname === "/api/v1/talk/reply" && request.method === "POST") {
      const body = await readJson(request);
      const text = typeof body.text === "string" ? body.text : body.body;
      if (typeof text !== "string" || text.length === 0 || typeof body.turn_id !== "string") return response({ error: "text and turn_id are required" }, 400);
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
      const envelope = foremanAck[2] === "claim" ? store.claimForemanMessage(foremanAck[1]) : store.completeForemanMessage(foremanAck[1]);
      if (!envelope) return response({ error: "foreman message was not found" }, 404);
      events.publish(`foreman.message.${foremanAck[2]}`, envelope);
      return response(envelope, 200);
    }
    const foremanDispatch = url.pathname.match(/^\/api\/v1\/foreman\/inbox\/([^/]+)\/dispatch$/);
    if (foremanDispatch && request.method === "POST") {
      runInBackground(foreman.dispatch(foremanDispatch[1]));
      return response({ state: "dispatching", inbox_id: foremanDispatch[1] }, 202);
    }
    if (url.pathname === "/api/v1/secrets/requests" && request.method === "POST") {
      const body = await readJson(request);
      if (typeof body.project_id !== "string" || !projectCredentialName(body.name)) return response({ error: "project_id and a valid credential name are required" }, 400);
      if (!store.getProject(body.project_id)) return response({ error: "project was not found" }, 404);
      const requestRecord = store.createSecretRequest({ projectId: body.project_id, name: body.name, vaultRef: `vault://secret/${store.uuid()}`, metadata: { purpose: body.purpose, scope: body.scope } });
      const { vaultRef: _vaultRef, ...visible } = requestRecord;
      events.publish("secret.requested", visible);
      return response({ request_id: requestRecord.id, state: requestRecord.status, request: visible }, 201);
    }
    const secretValueMatch = url.pathname.match(/^\/api\/v1\/secrets\/requests\/([^/]+)\/value$/);
    if (secretValueMatch && request.method === "POST") {
      const requestRecord = store.getSecretRequest(secretValueMatch[1]);
      if (!requestRecord) return response({ error: "secret request was not found" }, 404);
      const body = await readJson(request);
      if (typeof body.value !== "string" || body.value.length === 0) return response({ error: "value is required" }, 400);
      vault.put(requestRecord.vaultRef, body.value);
      const available = store.markSecretAvailable(requestRecord.id);
      events.publish("secret.available", { id: requestRecord.id, projectId: requestRecord.projectId, name: requestRecord.name, status: available?.status ?? "available" });
      for (const runId of store.resumeResourceRuns(requestRecord.id)) dispatchOperatorRun(runId);
      return response({ request_id: requestRecord.id, state: "available" }, 200);
    }
    // The existing web client uses these read-only projections while it migrates to v1.
    if (url.pathname === "/api/web/talk" && request.method === "GET") {
      const requested = Number(url.searchParams.get("limit") ?? 80);
      const limit = Number.isFinite(requested) ? Math.max(1, Math.min(1000, Math.trunc(requested))) : 80;
      return response(store.recentTalk(limit));
    }
    if (url.pathname === "/api/web/logs" && request.method === "GET") {
      const requested = Number(url.searchParams.get("limit") ?? 100);
      const limit = Number.isSafeInteger(requested) ? Math.max(1, Math.min(1000, requested)) : 100;
      return response(webLogs(store, limit));
    }
    if (url.pathname === "/api/web/graph" && request.method === "GET") {
      return response(graphProjection(store, url.searchParams.get("scope") ?? "factory", url.searchParams.get("workarea")));
    }
    if (url.pathname === "/api/web/harnesses" && request.method === "GET") {
      return response(officialHarnesses().map((item) => ({ ...item, configuration: publicProfile(store.listHarnessProfiles().find((profile) => profile.runtime === item.id)) })));
    }
    if (url.pathname === "/api/web/harnesses/configure" && request.method === "POST") {
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
      const profile = store.saveHarnessProfile({
        runtime: definition.id, role: body.role, baseUrl, model: body.model.trim(), mode: typeof body.mode === "string" ? body.mode : "session",
        image: image || undefined, network: image ? body.network === "egress" ? "egress" : "none" : undefined,
        verification: { state: "pending", checkedAt: store.timestamp() },
      });
      verificationJobs.get(profile.id)?.abort();
      verificationJobs.delete(profile.id);
      events.publish("harness.profile.saved", { id: profile.id, runtime: profile.runtime, role: profile.role });
      const foremanProfile = profile.role === "foreman" ? profile : store.listHarnessProfiles().find((item) => item.role === "foreman");
      return response({ profile: publicProfile(profile), foreman: { configured: Boolean(foremanProfile), setup_required: !foremanProfile, runtime: foremanProfile?.runtime, model: foremanProfile?.model, mode: foremanProfile?.mode, profile: publicProfile(foremanProfile) } }, 200);
    }
    if (url.pathname === "/api/web/harnesses/verify" && request.method === "POST") {
      const body = await readJson(request);
      if (typeof body.profile_id !== "string") return response({ error: "profile_id is required" }, 400);
      const existing = store.listHarnessProfiles().find((item) => item.id === body.profile_id);
      if (!existing) return response({ error: "profile was not found" }, 404);
      if (verificationJobs.has(existing.id)) return response({ profile: publicProfile(existing) }, 202);
      const profile = store.markHarnessVerification(existing.id, "requested", "正在验证官方 Harness 连通性");
      if (!profile) return response({ error: "profile was not found" }, 404);
      const controller = new AbortController();
      verificationJobs.set(profile.id, controller);
      events.publish("harness.profile.verification.requested", { id: profile.id, runtime: profile.runtime, role: profile.role });
      const transportOptions = profile.role === "operator" ? options.operator : options.foreman;
      void verifyHarness(profile, { ...transportOptions, ensureRuntime, signal: controller.signal, timeoutMs: options.harnessVerificationTimeoutMs }).then(({ state, reason }) => {
        if (verificationJobs.get(profile.id) !== controller) return;
        verificationJobs.delete(profile.id);
        const current = store.listHarnessProfiles().find((item) => item.id === profile.id);
        if (current?.updatedAt !== profile.updatedAt || current.verification?.state !== "requested") return;
        const completed = store.markHarnessVerification(profile.id, state, reason);
        if (completed) events.publish("harness.profile.verification.completed", { id: profile.id, runtime: profile.runtime, role: profile.role, verification: publicProfile(completed)?.verification });
      }).catch(() => {});
      return response({ profile: publicProfile(profile) }, 202);
    }
    if (url.pathname === "/api/web/assets" && request.method === "GET") {
      const limit = Number(url.searchParams.get("limit") ?? 100);
      return response(webAssets(store).slice(-Math.max(1, limit)));
    }
    if (url.pathname === "/api/web/secrets" && request.method === "GET") {
      return response(store.listSecretRequests().map((secret) => ({ id: secret.id, key: secret.name, configured: secret.status === "available", status: secret.status, usage_scope: secret.metadata.scope, request: { id: secret.id, project_id: secret.projectId, project_name: secret.projectName, status: secret.status, purpose: secret.metadata.purpose, scope: secret.metadata.scope } })));
    }
    if (url.pathname === "/api/web/talk" && request.method === "POST") {
      const body = await readJson(request);
      const text = typeof body.text === "string" ? body.text : body.body;
      if (typeof text !== "string" || text.length === 0) return response({ error: "text is required" }, 400);
      const message = store.addTalkMessage({ body: redactText(text) });
      store.bridgeHumanTalk(message.id, message.body);
      const envelope = store.queueForemanMessage(message.id, message.body);
      events.publish("talk.message.created", message);
      events.publish("foreman.message.queued", envelope);
      dispatchForemanIfConfigured(envelope.id);
      return response({ state: "accepted", message }, 201);
    }
    if (url.pathname === "/api/web/bootstrap" && request.method === "GET") {
      const health = await runtimeHealth();
      const eventCursor = store.latestEventSequence();
      const factoryGraph = graphProjection(store, "factory", null);
      const foremanProfile = store.listHarnessProfiles().find((profile) => profile.role === "foreman");
      return response({ version: "backend-v1", connection: { state: "connected" }, status: { at: store.timestamp() },
        eventCursor, pendingForemanReplies: foreman.pendingReplies(),
        graph: { factory: factoryGraph },
        assets: webAssets(store), logs: webLogs(store, 100), talk: store.recentTalk(80),
        harnesses: officialHarnesses().map((item) => ({ ...item, configuration: publicProfile(store.listHarnessProfiles().find((profile) => profile.runtime === item.id)) })), harness_profiles: store.listHarnessProfiles().map(publicProfile), secrets: store.listSecretRequests().map((secret) => ({ id: secret.id, key: secret.name, configured: secret.status === "available", status: secret.status, request: { id: secret.id, project_id: secret.projectId, project_name: secret.projectName, status: secret.status, purpose: secret.metadata.purpose, scope: secret.metadata.scope } })),
        dependencies: { [provider]: runtimeDependency(health, provider), podman: provider === "podman" ? runtimeDependency(health, provider) : { state: "inactive", provider: "podman", message: "当前使用 Kiri" }, kiri: provider === "kiri" ? runtimeDependency(health, provider) : { state: "inactive", provider: "kiri", message: "当前使用 Podman" }, harnesses: officialHarnesses().map((item) => ({ id: item.id, name: item.name, state: item.available ? "available" : "unavailable" })) },
        foreman: { configured: Boolean(foremanProfile), setup_required: !foremanProfile, runtime: foremanProfile?.runtime ?? null, model: foremanProfile?.model ?? null, mode: foremanProfile?.mode ?? null, profile: publicProfile(foremanProfile) }, constitution: { available: false } });
    }
    if (url.pathname === "/api/v1/events" && request.method === "GET") {
      const signal = AbortSignal.any([request.signal, eventStreams.signal]);
      return eventsResponse(events, request.headers.get("last-event-id") ?? url.searchParams.get("cursor"), signal);
    }
    if (request.method === "GET" && !url.pathname.startsWith("/api/")) return staticResponse(url.pathname);
    return response({ error: "not found" }, 404);
  };
  return { store, events, vault, foreman, operator, fetch, recoverQueuedWork, stop: () => {
    stopped = true;
    eventStreams.abort();
    for (const job of verificationJobs.values()) job.abort();
    verificationJobs.clear();
    foreman.stop();
    operator.stop();
    store.close();
  } };
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

function officialHarnesses() {
  return harnessDefinitions().map((definition) => ({
    id: definition.id, name: definition.displayName, kind: "agent-cli", source: "official",
    roles: ["operator", "foreman"], available: Boolean(Bun.which(definition.executable)),
    capabilities: { acp: definition.transportPreference === "acp", resume: definition.supportsResume, stream: true },
  }));
}

function publicProfile(profile: any) {
  if (!profile) return null;
  return { ...profile, base_url: profile.baseUrl, api_key_configured: Boolean(profile.secretRef), updated_at: profile.updatedAt, verification: profile.verification ? { ...profile.verification, checked_at: profile.verification.checkedAt } : undefined };
}

function publicUnit(unit: any) {
  const sourceKind = typeof unit.metadata?.source_kind === "string" ? unit.metadata.source_kind : undefined;
  return { ...unit, machineRef: unit.kind === "machine" ? sourceKind : undefined };
}

function publicUnitRun(run: any) {
  return { ...run };
}

function publicProject(project: any) {
  return {
    ...project,
    metadata: portableValue(project.metadata ?? {}),
    sources: Array.isArray(project.sources)
      ? project.sources.map((source: any) => {
        const { localFolder: _localFolder, ...portableSource } = source;
        return { ...portableSource, metadata: portableValue(source.metadata ?? {}) };
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
  return { ...record, project_id: record.projectId, source_id: record.sourceId, record_type: record.recordType, created_at: record.createdAt };
}

function webAssets(store: SqliteStore) {
  const legacy = store.legacyArtifacts().map((record) => {
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
      files: Array.isArray(document.files) ? portableValue(document.files) : [],
      inputs: Array.isArray(document.inputs) ? document.inputs.filter((item): item is string => typeof item === "string") : [],
      outputs: Array.isArray(document.outputs) ? document.outputs.filter((item): item is string => typeof item === "string") : [],
      lineage: Array.isArray(document.lineage) ? document.lineage.filter((item): item is string => typeof item === "string") : [],
      sealed: true,
      expired: false,
      created_at: record.createdAt,
      source: { node: typeof document.producer === "object" && document.producer ? (document.producer as Record<string, unknown>).operator : undefined },
    };
  });
  const business = store.listAllArtifacts().map((record) => ({
    id: record.id,
    type: record.kind,
    record_type: record.recordType,
    ref: record.ref,
    title: record.title,
    description: record.description,
    summary: record.summary,
    purpose: record.purpose,
    status: record.status,
    files: portableValue(record.files ?? []),
    inputs: record.inputs ?? [],
    outputs: record.outputs ?? [],
    lineage: record.lineage ?? [],
    sealed: ["sealed", "produced", "completed"].includes(record.status ?? ""),
    expired: false,
    created_at: record.createdAt,
    source: { node: record.producer?.operator, run: typeof record.metadata.run_id === "string" ? record.metadata.run_id : undefined },
  }));
  return [...legacy, ...business];
}

function portableValue(value: unknown, key = ""): any {
  if (/(secret|token|password|api[_-]?key|authorization|cookie|local[_-]?folder|host[_-]?path)/i.test(key)) return "[REDACTED]";
  if (Array.isArray(value)) return value.map((item) => portableValue(item));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, portableValue(item, name)]));
  return value;
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
    const storedEdges = Array.isArray(storedGraph?.edges) ? storedGraph.edges.filter((edge): edge is Record<string, any> => Boolean(edge && typeof edge === "object" && typeof (edge as any).source === "string" && typeof (edge as any).target === "string" && storedById.has((edge as any).source) && storedById.has((edge as any).target))) : [];
    const edges = storedEdges.length ? storedEdges.map((edge) => ({ id: typeof edge.id === "string" ? edge.id : `${edge.source}:${edge.target}`, source: edge.source, target: edge.target, kind: edge.kind ?? "material", condition: edge.condition, status: edge.status ?? "ready" })) : operatorUnits.length ? operatorUnits.map((unit) => ({ id: `${unit.id}:operator-to-machine`, source: unit.id, target: `${unit.projectId}:machine`, kind: "material", condition: "业务物料", status: "ready" })) : selectedProjects.map((project) => ({ id: `${project.id}:operator-to-machine`, source: `${project.id}:operator`, target: `${project.id}:machine`, kind: "material", condition: "业务物料", status: "ready" }));
    return { scope: "workarea", id: workarea, title: selectedSystem?.name ?? workarea, nodes, edges };
  }
  const systems = store.listBusinessSystems();
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

function webLogs(store: SqliteStore, limit: number) {
  return [...store.legacyEvents(limit), ...store.recentEvents(limit)]
    .sort((a, b) => a.createdAt.localeCompare(b.createdAt))
    .slice(-limit)
    .map((event) => ({ id: event.id, kind: event.type, at: event.createdAt, data: event.data }));
}

function eventsResponse(events: EventBus, cursor: string | null, signal: AbortSignal): Response {
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
            const data = event.data && typeof event.data === "object" ? { ...event.data, type: event.type } : { type: event.type, value: event.data };
            controller.enqueue(encoder.encode(`id: ${event.seq}\nevent: mao.event\ndata: ${JSON.stringify(data)}\n\n`));
            sequence = event.seq;
            index++;
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
  return new Response(stream, { headers: { "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache", "connection": "keep-alive", "access-control-allow-origin": "*" } });
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
    return { ...app, server: listening, stop: () => { clearInterval(externalWork); listening.stop(); app.stop(); } };
  } catch (error) {
    server?.stop();
    app.stop();
    throw error;
  }
}

