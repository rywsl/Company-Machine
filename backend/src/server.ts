import { EventBus } from "./event-bus";
import { SqliteStore } from "./db";
import { VaultStore } from "./vault";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { extname, join, normalize } from "node:path";
import type { BusinessConversion, BusinessSystem } from "./models";
import { PodmanProvider, type RuntimeHealth } from "../runtime/podman";
import { ForemanCoordinator } from "./foreman";
import { OperatorCoordinator } from "./operator";
import type { OfficialSpawn } from "./harness";
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
  fetch(request: Request): Response | Promise<Response>;
  stop(): void;
}

export interface RuntimeProbe {
  health(): Promise<RuntimeHealth>;
  ensureReady?(): Promise<RuntimeHealth>;
}

function runtimeDependency(health: RuntimeHealth) {
  return {
    state: health.available ? "online" : "offline",
    version: health.available ? "available" : null,
    message: health.available ? "Podman 运行时在线" : `Podman 运行时不可用：${health.reason ?? "unknown"}`,
    provider: "podman",
    machine: health.machine,
    rootless: health.rootless,
  };
}

export function createApp(options: { dbPath?: string; legacyPath?: string; runtime?: RuntimeProbe; foreman?: { cwd?: string; spawn?: OfficialSpawn; acpSpawn?: AcpSpawn }; operator?: { cwd?: string; spawn?: OfficialSpawn; acpSpawn?: AcpSpawn } } = {}): BackendApp {
  const store = new SqliteStore(options.dbPath, options.legacyPath);
  const events = new EventBus(store);
  const vault = new VaultStore();
  const foreman = new ForemanCoordinator({ store, events, cwd: options.foreman?.cwd, spawn: options.foreman?.spawn, acpSpawn: options.foreman?.acpSpawn });
  let dispatchOperatorRun = (_runId: string) => {};
  const operator = new OperatorCoordinator({ store, events, cwd: options.operator?.cwd, spawn: options.operator?.spawn, acpSpawn: options.operator?.acpSpawn, onProgress: (runId) => dispatchOperatorRun(runId) });
  const runtime = options.runtime ?? new PodmanProvider({
    egressNetwork: process.env.MAO_EGRESS_NETWORK,
    egressEnv: Object.fromEntries(["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"].flatMap((key) => process.env[key] ? [[key, process.env[key] as string]] : [])),
  });
  let runtimeCache: { at: number; health: RuntimeHealth } | undefined;
  const runtimeHealth = async (refresh = false) => {
    const current = Date.now();
    if (!refresh && runtimeCache && current - runtimeCache.at < 5_000) return runtimeCache.health;
    const health = await runtime.health();
    runtimeCache = { at: current, health };
    return health;
  };
  const dispatchForemanIfConfigured = (id: string) => {
    if (store.listHarnessProfiles().some((profile) => profile.role === "foreman")) void foreman.dispatch(id);
  };
  dispatchOperatorRun = (runId: string) => {
    for (const unitRun of store.listReadyUnitRuns(runId)) void operator.dispatch(runId, unitRun.id);
  };
  const fetch = async (request: Request): Promise<Response> => {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: { ...headers, "access-control-allow-methods": "GET,POST,OPTIONS", "access-control-allow-headers": "content-type,last-event-id" } });
    if (url.pathname === "/healthz" && request.method === "GET") return response({ ok: true });
    if (url.pathname === "/api/v1/runtime" && request.method === "GET") {
      const health = await runtimeHealth();
      return response({ provider: "podman", health, dependency: runtimeDependency(health) });
    }
    if (url.pathname === "/api/v1/runtime/ensure-ready" && request.method === "POST") {
      if (!runtime.ensureReady) return response({ error: "runtime readiness is unavailable" }, 501);
      const health = await runtime.ensureReady();
      runtimeCache = { at: Date.now(), health };
      events.publish("runtime.podman.checked", runtimeDependency(health));
      return response({ provider: "podman", health, dependency: runtimeDependency(health) }, health.available ? 200 : 503);
    }
    if (url.pathname === "/api/v1/business-systems/import" && request.method === "POST") {
      const document = await readJson(request);
      try {
        const imported = importBusinessSystem(store, document);
        events.publish("business-system.imported", { id: imported.id, schema: document.schema });
        return response(exportBusinessSystem(store, imported.id), 201);
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
      const system = store.listBusinessSystems().find((item) => item.id === businessSystemMatch[1]);
      if (!system) return response({ error: "business system was not found" }, 404);
      const projects = store.listProjects().filter((project) => project.businessSystemId === system.id);
      const artifacts = store.listBusinessSystemArtifacts(system.id);
      return response({ ...system, projects: projects.map(publicProject), artifacts: artifacts.map(publicArtifact), materials: artifacts.filter((item) => item.recordType === "material").map(publicArtifact) });
    }
    const graphMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)\/graph$/);
    if (graphMatch && request.method === "GET") {
      const system = store.listBusinessSystems().find((item) => item.id === graphMatch[1]);
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
      const system = store.listBusinessSystems().find((item) => item.id === systemArtifactsMatch[1]);
      if (!system) return response({ error: "business system was not found" }, 404);
      const artifacts = store.listBusinessSystemArtifacts(system.id);
      return response((systemArtifactsMatch[2] === "materials" ? artifacts.filter((item) => item.recordType === "material") : artifacts).map(publicArtifact));
    }
    const conversionMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)\/conversions$/);
    if (conversionMatch && request.method === "GET") {
      if (!store.listBusinessSystems().some((item) => item.id === conversionMatch[1])) return response({ error: "business system was not found" }, 404);
      return response(store.listBusinessConversions(conversionMatch[1]));
    }
    if (conversionMatch && request.method === "POST") {
      const body = await readJson(request);
      if (typeof body.kind !== "string" || typeof body.summary !== "string") return response({ error: "kind and summary are required" }, 400);
      if (body.inputs !== undefined && (!Array.isArray(body.inputs) || body.inputs.some((item: unknown) => typeof item !== "string"))) return response({ error: "inputs must be string references" }, 400);
      if (body.outputs !== undefined && (!Array.isArray(body.outputs) || body.outputs.some((item: unknown) => typeof item !== "string"))) return response({ error: "outputs must be string references" }, 400);
      if (!store.listBusinessSystems().some((item) => item.id === conversionMatch[1])) return response({ error: "business system was not found" }, 404);
      const conversion = store.createBusinessConversion({ businessSystemId: conversionMatch[1], kind: body.kind, summary: body.summary, status: body.status === "running" || body.status === "completed" || body.status === "failed" ? body.status : "planned", inputs: body.inputs ?? [], outputs: body.outputs ?? [], producer: body.producer && typeof body.producer === "object" ? body.producer : undefined, metadata: body.metadata && typeof body.metadata === "object" ? body.metadata : {} });
      events.publish("business-conversion.created", conversion);
      return response(conversion, 201);
    }
    const exportMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)\/export$/);
    if (exportMatch && request.method === "GET") {
      const document = exportBusinessSystem(store, exportMatch[1]);
      return document ? response(document) : response({ error: "business system was not found" }, 404);
    }
    const unitsMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)\/units$/);
    if (unitsMatch && request.method === "GET") {
      if (!store.listBusinessSystems().some((item) => item.id === unitsMatch[1])) return response({ error: "business system was not found" }, 404);
      return response(store.listBusinessUnits(unitsMatch[1]).map(publicUnit));
    }
    const runsMatch = url.pathname.match(/^\/api\/v1\/business-systems\/([^/]+)\/runs$/);
    if (runsMatch && request.method === "GET") {
      if (!store.listBusinessSystems().some((item) => item.id === runsMatch[1])) return response({ error: "business system was not found" }, 404);
      return response(store.listBusinessRuns(runsMatch[1]).map((run) => ({ ...run, units: store.listUnitRuns(run.id).map(publicUnitRun) })));
    }
    if (runsMatch && request.method === "POST") {
      const body = await readJson(request);
      const inputs = Array.isArray(body.inputs) ? body.inputs.filter((item: unknown): item is string => typeof item === "string") : [];
      const run = store.createBusinessRun(runsMatch[1], typeof body.trigger === "string" ? body.trigger : "manual", body.metadata && typeof body.metadata === "object" ? body.metadata : {}, inputs);
      if (!run) return response({ error: "business system was not found" }, 404);
      events.publish("business-run.created", { ...run, units: store.listUnitRuns(run.id) });
      if (run.status === "queued" && body.dispatch !== false) dispatchOperatorRun(run.id);
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
      dispatchOperatorRun(run.id);
      return response({ state: "dispatching", run_id: run.id }, 202);
    }
    if (url.pathname === "/api/v1/projects" && request.method === "GET") return response(store.listProjects().map(publicProject));
    if (url.pathname === "/api/v1/projects/intake" && request.method === "POST") {
      const body = await readJson(request);
      if (!body.repo_url && !body.local_folder && !(Array.isArray(body.files) && body.files.length) && !(Array.isArray(body.uploaded_file_refs) && body.uploaded_file_refs.length)) {
        return response({ error: "one project source is required" }, 400);
      }
      if (typeof body.business_system_id !== "string") {
        const system = store.createBusinessSystem({ name: typeof body.name === "string" ? body.name : typeof body.project_name === "string" ? body.project_name : "新业务系统", metadata: { intake: true } });
        body.business_system_id = system.id;
        events.publish("business-system.created", system);
      }
      const project = store.createIntake(body);
      events.publish("project.intake.created", project);
      return response({ ...publicProject(project), project_id: project.id, state: "accepted" }, 201);
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
      void foreman.dispatch(foremanDispatch[1]);
      return response({ state: "dispatching", inbox_id: foremanDispatch[1] }, 202);
    }
    if (url.pathname === "/api/v1/secrets/requests" && request.method === "POST") {
      const body = await readJson(request);
      if (typeof body.project_id !== "string" || typeof body.name !== "string") return response({ error: "project_id and name are required" }, 400);
      if (!store.getProject(body.project_id)) return response({ error: "project was not found" }, 404);
      const requestRecord = store.createSecretRequest({ projectId: body.project_id, name: body.name, vaultRef: `vault://secret/${randomUUID()}`, metadata: { purpose: body.purpose, scope: body.scope } });
      events.publish("secret.requested", requestRecord);
      return response({ request_id: requestRecord.id, state: requestRecord.status, request: requestRecord }, 201);
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
      return response({ request_id: requestRecord.id, state: "available" }, 200);
    }
    // The existing web client uses these read-only projections while it migrates to v1.
    if (url.pathname === "/api/web/talk" && request.method === "GET") {
      const limit = Number(url.searchParams.get("limit") ?? 80);
      return response([...store.legacyTalk(), ...store.listTalkMessages()].slice(-Math.max(1, limit)));
    }
    if (url.pathname === "/api/web/logs" && request.method === "GET") {
      const limit = Number(url.searchParams.get("limit") ?? 100);
      const records = [...store.legacyEvents(), ...store.eventsAfter(0)].map((event) => ({ id: event.id, kind: event.type, at: event.createdAt, data: event.data }));
      return response(records.slice(-Math.max(1, limit)));
    }
    if (url.pathname === "/api/web/graph" && request.method === "GET") {
      return response(graphProjection(store, url.searchParams.get("scope") ?? "factory", url.searchParams.get("workarea")));
    }
    if (url.pathname === "/api/web/harnesses" && request.method === "GET") {
      return response(officialHarnesses().map((item) => ({ ...item, configuration: publicProfile(store.listHarnessProfiles().find((profile) => profile.runtime === item.id)) })));
    }
    if (url.pathname === "/api/web/harnesses/configure" && request.method === "POST") {
      const body = await readJson(request);
      if (typeof body.runtime !== "string" || typeof body.role !== "string" || typeof body.model !== "string" || typeof body.base_url !== "string") return response({ error: "runtime, role, model, and base_url are required" }, 400);
      if (typeof body.api_key === "string" && body.api_key.length > 0) return response({ error: "密钥必须通过项目资源请求写入保险箱" }, 400);
      const profile = store.saveHarnessProfile({ runtime: body.runtime, role: body.role, baseUrl: body.base_url, model: body.model, mode: typeof body.mode === "string" ? body.mode : "session", image: typeof body.image === "string" ? body.image : undefined, network: typeof body.network === "string" ? body.network : undefined, verification: { state: "pending", checkedAt: new Date().toISOString() } });
      events.publish("harness.profile.saved", { id: profile.id, runtime: profile.runtime, role: profile.role });
      return response({ profile: publicProfile(profile), foreman: { configured: profile.role === "foreman", runtime: profile.runtime, model: profile.model, mode: profile.mode, profile: publicProfile(profile) } }, 200);
    }
    if (url.pathname === "/api/web/harnesses/verify" && request.method === "POST") {
      const body = await readJson(request);
      if (typeof body.profile_id !== "string") return response({ error: "profile_id is required" }, 400);
      const profile = store.markHarnessVerification(body.profile_id, "requested", "官方 Harness 连通性验证已排队");
      if (!profile) return response({ error: "profile was not found" }, 404);
      events.publish("harness.profile.verification.requested", { id: profile.id, runtime: profile.runtime });
      return response({ profile: publicProfile(profile) }, 202);
    }
    if (url.pathname === "/api/web/assets" && request.method === "GET") {
      const limit = Number(url.searchParams.get("limit") ?? 100);
      return response(webAssets(store).slice(-Math.max(1, limit)));
    }
    if (url.pathname === "/api/web/secrets" && request.method === "GET") {
      return response(store.listSecretRequests().map((secret) => ({ id: secret.id, key: secret.name, configured: secret.status === "available", status: secret.status, usage_scope: secret.metadata.scope, request: { id: secret.id, project_id: secret.projectId, status: secret.status, purpose: secret.metadata.purpose, scope: secret.metadata.scope } })));
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
      const factoryGraph = graphProjection(store, "factory", null);
      const foremanProfile = store.listHarnessProfiles().find((profile) => profile.role === "foreman");
      return response({ version: "backend-v1", connection: { state: "connected" }, status: { at: new Date().toISOString() },
        graph: { factory: factoryGraph },
        assets: webAssets(store), logs: store.legacyEvents(), talk: [...store.legacyTalk(), ...store.listTalkMessages()],
        harnesses: officialHarnesses().map((item) => ({ ...item, configuration: publicProfile(store.listHarnessProfiles().find((profile) => profile.runtime === item.id)) })), harness_profiles: store.listHarnessProfiles().map(publicProfile), secrets: store.listSecretRequests().map((secret) => ({ id: secret.id, key: secret.name, configured: secret.status === "available", status: secret.status, request: { id: secret.id, project_id: secret.projectId, status: secret.status, purpose: secret.metadata.purpose, scope: secret.metadata.scope } })),
        dependencies: { podman: runtimeDependency(health), harnesses: officialHarnesses().map((item) => ({ id: item.id, name: item.name, state: item.available ? "available" : "unavailable" })) },
        foreman: { configured: Boolean(foremanProfile), setup_required: !foremanProfile, runtime: foremanProfile?.runtime ?? null, model: foremanProfile?.model ?? null, mode: foremanProfile?.mode ?? null, profile: publicProfile(foremanProfile) }, constitution: { available: false } });
    }
    if (url.pathname === "/api/v1/events" && request.method === "GET") return eventsResponse(request, events, url.searchParams.get("cursor") ?? undefined);
    if (request.method === "GET" && !url.pathname.startsWith("/api/")) return staticResponse(url.pathname);
    return response({ error: "not found" }, 404);
  };
  return { store, events, vault, foreman, operator, fetch, stop: () => { foreman.stop(); operator.stop(); store.close(); } };
}

function staticResponse(pathname: string): Response {
  const root = process.env.MAO_WEB_DIST ?? "src/mao/webapp/static/dist";
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
  return [
    { id: "claude-code", name: "Claude Code", kind: "agent-cli", source: "official", roles: ["operator", "foreman"], available: true, capabilities: { acp: false, resume: true, stream: true } },
    { id: "codex", name: "Codex", kind: "agent-cli", source: "official", roles: ["operator", "foreman"], available: true, capabilities: { acp: false, resume: true, stream: true } },
    { id: "deepseek-harness", name: "DeepSeek Harness", kind: "agent-cli", source: "official", roles: ["operator", "foreman"], available: false, capabilities: { acp: true, resume: true, stream: true } },
    { id: "zcode", name: "ZCode", kind: "agent-cli", source: "official", roles: ["operator", "foreman"], available: false, capabilities: { acp: true, resume: true, stream: true } },
    { id: "kimi-code", name: "Kimi Code", kind: "agent-cli", source: "official", roles: ["operator", "foreman"], available: false, capabilities: { acp: true, resume: true, stream: true } },
    { id: "pi", name: "Pi", kind: "agent-cli", source: "official", roles: ["operator", "foreman"], available: false, capabilities: { acp: true, resume: true, stream: true } },
  ];
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

function exportBusinessSystem(store: SqliteStore, id: string) {
  const system = store.listBusinessSystems().find((item) => item.id === id);
  if (!system) return undefined;
  const projects = store.listProjects().filter((project) => project.businessSystemId === id).map((project) => ({
    id: project.id,
    name: project.name,
    metadata: portableValue(project.metadata),
    sources: project.sources.map((source) => ({
      kind: source.kind,
      ref: source.kind === "repo_url" ? source.repoUrl : source.kind === "uploaded_file" ? source.fileRef : `intake://${project.id}`,
      metadata: portableValue(source.metadata),
    })),
    secretRequests: project.secretRequests.map((secret) => ({ id: secret.id, name: secret.name, status: secret.status, metadata: portableValue(secret.metadata) })),
  }));
  const artifacts = store.listBusinessSystemArtifacts(id);
  return {
    schema: "mao.business-system/1",
    exportedAt: new Date().toISOString(),
    system: { ...system, metadata: portableValue(system.metadata) },
    graph: system.metadata.graph ?? graphProjection(store, "workarea", id),
    units: store.listBusinessUnits(id).map(publicUnit),
    projects,
    records: artifacts.map((record) => ({ ...publicArtifact(record), metadata: portableValue(record.metadata) })),
    conversions: store.listBusinessConversions(id).map((conversion) => ({ ...conversion, metadata: portableValue(conversion.metadata) })),
    runs: store.listBusinessRuns(id).map((run) => ({ ...run, units: store.listUnitRuns(run.id).map(publicUnitRun) })),
  };
}

function portableValue(value: unknown, key = ""): any {
  if (/(secret|token|password|api[_-]?key|authorization|cookie|local[_-]?folder|host[_-]?path)/i.test(key)) return "[REDACTED]";
  if (Array.isArray(value)) return value.map((item) => portableValue(item));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, portableValue(item, name)]));
  return value;
}

function importBusinessSystem(store: SqliteStore, document: Record<string, any>): BusinessSystem {
  if (document.schema !== "mao.business-system/1" || !document.system || typeof document.system.name !== "string") throw new Error("business system document schema or name is invalid");
  const importedGraph = document.graph && typeof document.graph === "object" ? document.graph : undefined;
  if (importedGraph) {
    const graphError = validateDag(importedGraph);
    if (graphError) throw new Error(`business system graph is invalid: ${graphError}`);
  }
  const system = store.createBusinessSystem({ name: document.system.name, description: typeof document.system.description === "string" ? document.system.description : undefined, metadata: document.system.metadata && typeof document.system.metadata === "object" ? document.system.metadata : {} });
  const projectIds = new Map<string, string>();
  for (const sourceProject of Array.isArray(document.projects) ? document.projects : []) {
    if (!sourceProject || typeof sourceProject.name !== "string") continue;
    const sources = Array.isArray(sourceProject.sources) ? sourceProject.sources : [];
    const intake: Record<string, any> = { business_system_id: system.id, name: sourceProject.name, metadata: sourceProject.metadata && typeof sourceProject.metadata === "object" ? sourceProject.metadata : {}, portable_sources: sources.filter((source: any) => source && typeof source.kind === "string").map((source: any) => ({ kind: source.kind, ref: typeof source.ref === "string" ? source.ref : undefined })) };
    const project = store.createIntake(intake);
    if (typeof sourceProject.id === "string") projectIds.set(sourceProject.id, project.id);
  }
  for (const record of Array.isArray(document.records) ? document.records : []) {
    if (!record || typeof record.kind !== "string" || typeof record.ref !== "string" || typeof record.summary !== "string") continue;
    const projectId = projectIds.get(String(record.projectId ?? record.project_id));
    if (!projectId) continue;
    store.addArtifact({ projectId, recordType: record.recordType === "artifact" ? "artifact" : "material", sourceId: typeof record.sourceId === "string" ? record.sourceId : undefined, kind: record.kind, ref: record.ref, title: record.title, description: record.description, summary: record.summary, purpose: record.purpose, status: record.status, producer: record.producer, files: Array.isArray(record.files) ? record.files : undefined, inputs: Array.isArray(record.inputs) ? record.inputs : undefined, outputs: Array.isArray(record.outputs) ? record.outputs : undefined, lineage: Array.isArray(record.lineage) ? record.lineage : undefined, metadata: record.metadata && typeof record.metadata === "object" ? record.metadata : {} });
  }
  if (importedGraph) store.updateBusinessSystemMetadata(system.id, { graph: importedGraph, graph_updated_by: "import" });
  for (const conversion of Array.isArray(document.conversions) ? document.conversions : []) {
    if (typeof conversion.kind !== "string" || typeof conversion.summary !== "string") continue;
    store.createBusinessConversion({ businessSystemId: system.id, kind: conversion.kind, summary: conversion.summary, status: conversion.status === "running" || conversion.status === "completed" || conversion.status === "failed" ? conversion.status : "planned", inputs: Array.isArray(conversion.inputs) ? conversion.inputs : [], outputs: Array.isArray(conversion.outputs) ? conversion.outputs : [], producer: conversion.producer, metadata: conversion.metadata && typeof conversion.metadata === "object" ? conversion.metadata : {} });
  }
  return system;
}

function graphProjection(store: SqliteStore, scope: string, workarea: string | null) {
  const systems = store.listBusinessSystems();
  const projects = store.listProjects();
  if (scope === "workarea" && workarea) {
    const selectedProjects = projects.filter((project) => project.businessSystemId === workarea || project.id === workarea);
    const selectedSystem = systems.find((system) => system.id === workarea);
    const units = selectedSystem ? store.listBusinessUnits(selectedSystem.id) : [];
    const storedGraph = selectedSystem?.metadata.graph && typeof selectedSystem.metadata.graph === "object" ? selectedSystem.metadata.graph as { nodes?: unknown; edges?: unknown } : undefined;
    const storedNodes = Array.isArray(storedGraph?.nodes) ? storedGraph.nodes.filter((node): node is Record<string, any> => Boolean(node && typeof node === "object" && typeof (node as any).id === "string")) : [];
    const storedIds = new Set(storedNodes.map((node) => node.id));
    const nodes = units.length ? units.map((unit, index) => {
      const saved = storedIds.has(unit.id) ? storedNodes.find((node) => node.id === unit.id) : undefined;
      return {
      id: unit.id,
      type: unit.kind,
      group: unit.projectId ?? unit.businessSystemId,
      label: unit.kind === "operator" ? "Operator" : unit.name,
      position: saved?.position && typeof saved.position === "object" ? saved.position : { x: unit.kind === "operator" ? 170 : 470, y: 130 + Math.floor(index / 2) * 180 },
      status: unit.status === "pending" ? "waiting" : unit.status,
      metadata: { project: unit.name, harness: unit.harnessRuntime, model: unit.model, source_kind: unit.metadata.source_kind },
      };
    }) : selectedProjects.flatMap((project, index) => [
      { id: `${project.id}:operator`, type: "operator", group: project.id, label: "Operator", position: { x: 170, y: 130 + index * 180 }, status: "ready", metadata: { project: project.name, harness: "official" } },
      { id: `${project.id}:machine`, type: "machine", group: project.id, label: project.name, position: { x: 470, y: 130 + index * 180 }, status: "ready", metadata: { project: project.name, sources: project.sources.length } },
    ]);
    const operatorUnits = units.filter((unit) => unit.kind === "operator");
    const storedEdges = Array.isArray(storedGraph?.edges) ? storedGraph.edges.filter((edge): edge is Record<string, any> => Boolean(edge && typeof edge === "object" && typeof (edge as any).source === "string" && typeof (edge as any).target === "string" && storedIds.has((edge as any).source) && storedIds.has((edge as any).target))) : [];
    const edges = storedEdges.length ? storedEdges.map((edge) => ({ id: typeof edge.id === "string" ? edge.id : `${edge.source}:${edge.target}`, source: edge.source, target: edge.target, kind: edge.kind ?? "material", condition: edge.condition, status: edge.status ?? "ready" })) : operatorUnits.length ? operatorUnits.map((unit) => ({ id: `${unit.id}:operator-to-machine`, source: unit.id, target: `${unit.projectId}:machine`, kind: "material", condition: "业务物料", status: "ready" })) : selectedProjects.map((project) => ({ id: `${project.id}:operator-to-machine`, source: `${project.id}:operator`, target: `${project.id}:machine`, kind: "material", condition: "业务物料", status: "ready" }));
    return { scope: "workarea", id: workarea, title: systems.find((system) => system.id === workarea)?.name ?? workarea, nodes, edges };
  }
  return { scope: "factory", id: "factory", title: "工厂", nodes: systems.map((system, index) => ({ id: system.id, type: "workarea", group: "factory", label: system.name, position: { x: 180 + (index % 3) * 300, y: 160 + Math.floor(index / 3) * 200 }, status: system.status === "active" ? "ready" : "unknown", metadata: { description: system.description, project_count: projects.filter((project) => project.businessSystemId === system.id).length } })), edges: [] };
}

function validateDag(value: unknown): string | undefined {
  if (!value || typeof value !== "object") return "graph must be an object";
  const graph = value as { nodes?: unknown; edges?: unknown };
  if (!Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) return "graph requires nodes and edges arrays";
  const nodes = new Set<string>();
  for (const node of graph.nodes) {
    if (!node || typeof node !== "object" || typeof (node as any).id !== "string" || !(node as any).id) return "every graph node requires an id";
    if (nodes.has((node as any).id)) return "graph node ids must be unique";
    nodes.add((node as any).id);
  }
  const next = new Map<string, string[]>();
  for (const edge of graph.edges) {
    if (!edge || typeof edge !== "object" || typeof (edge as any).source !== "string" || typeof (edge as any).target !== "string") return "every graph edge requires source and target";
    const source = (edge as any).source as string;
    const target = (edge as any).target as string;
    if (!nodes.has(source) || !nodes.has(target)) return "graph edges must reference existing nodes";
    next.set(source, [...(next.get(source) ?? []), target]);
  }
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (node: string): boolean => {
    if (visiting.has(node)) return false;
    if (visited.has(node)) return true;
    visiting.add(node);
    for (const target of next.get(node) ?? []) if (!visit(target)) return false;
    visiting.delete(node);
    visited.add(node);
    return true;
  };
  for (const node of nodes) if (!visit(node)) return "graph must be acyclic";
  return undefined;
}

async function readJson(request: Request): Promise<Record<string, any>> {
  try { const value = await request.json(); return value && typeof value === "object" ? value as Record<string, any> : {}; } catch { return {}; }
}

function eventsResponse(request: Request, events: EventBus, cursor?: string): Response {
  const last = request.headers.get("last-event-id") ?? cursor;
  const cursorSeq = Number.isFinite(Number(last)) ? Number(last) : 0;
  let closeStream = () => {};
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const encoder = new TextEncoder();
      let replaying = true;
      let lastSent = cursorSeq;
      const buffered: Array<ReturnType<EventBus["after"]>[number]> = [];
      const send = (event: any) => {
        if (event.seq <= lastSent) return;
        const data = event.data && typeof event.data === "object" ? { ...event.data, type: event.type } : { type: event.type, value: event.data };
        controller.enqueue(encoder.encode(`id: ${event.seq}\nevent: mao.event\ndata: ${JSON.stringify(data)}\n\n`));
        lastSent = event.seq;
      };
      const unsubscribe = events.subscribe((event) => {
        if (replaying) buffered.push(event);
        else send(event);
      });
      for (const event of events.after(cursorSeq)) send(event);
      replaying = false;
      for (const event of buffered.sort((left, right) => left.seq - right.seq)) send(event);
      closeStream = () => { unsubscribe(); try { controller.close(); } catch { /* client disconnected */ } };
      request.signal.addEventListener("abort", closeStream, { once: true });
    },
    cancel() { closeStream(); },
  });
  return new Response(stream, { headers: { "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache", "connection": "keep-alive", "access-control-allow-origin": "*" } });
}

export function startServer(options: { port?: number; hostname?: string; dbPath?: string; legacyPath?: string } = {}) {
  const app = createApp(options);
  const server = Bun.serve({ port: options.port ?? Number(process.env.PORT ?? 3000), hostname: options.hostname ?? "127.0.0.1", fetch: app.fetch });
  return { ...app, server, stop: () => { server.stop(); app.stop(); } };
}

