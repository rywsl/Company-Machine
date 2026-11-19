import { EventBus } from "./event-bus";
import { SqliteStore } from "./db";
import { VaultStore } from "./vault";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { extname, join, normalize } from "node:path";
import type { BusinessSystem } from "./models";

const headers = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "access-control-allow-origin": "*" };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers });

export interface BackendApp {
  store: SqliteStore;
  events: EventBus;
  vault: VaultStore;
  fetch(request: Request): Response | Promise<Response>;
  stop(): void;
}

export function createApp(options: { dbPath?: string; legacyPath?: string } = {}): BackendApp {
  const store = new SqliteStore(options.dbPath, options.legacyPath);
  const events = new EventBus(store);
  const vault = new VaultStore();
  const fetch = async (request: Request): Promise<Response> => {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: { ...headers, "access-control-allow-methods": "GET,POST,OPTIONS", "access-control-allow-headers": "content-type,last-event-id" } });
    if (url.pathname === "/healthz" && request.method === "GET") return response({ ok: true });
    if (url.pathname === "/api/v1/business-systems" && request.method === "GET") return response(store.listBusinessSystems());
    if (url.pathname === "/api/v1/business-systems" && request.method === "POST") {
      const body = await readJson(request);
      if (!body.name || typeof body.name !== "string") return response({ error: "name is required" }, 400);
      const system = store.createBusinessSystem(body as Partial<BusinessSystem> & { name: string });
      events.publish("business-system.created", system);
      return response(system, 201);
    }
    if (url.pathname === "/api/v1/projects" && request.method === "GET") return response(store.listProjects());
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
      return response({ ...project, project_id: project.id, state: "accepted" }, 201);
    }
    if (url.pathname === "/api/v1/talk" && request.method === "POST") {
      const body = await readJson(request);
      const text = typeof body.body === "string" ? body.body : body.text;
      if (typeof text !== "string" || text.length === 0) return response({ error: "body is required" }, 400);
      const message = store.addTalkMessage({ ...body, body: text });
      events.publish("talk.message.created", message);
      return response({ state: "accepted", message }, 201);
    }
    if (url.pathname === "/api/v1/talk" && request.method === "GET") return response([...store.legacyTalk(), ...store.listTalkMessages()]);
    if (url.pathname === "/api/v1/talk/reply" && request.method === "POST") {
      const body = await readJson(request);
      const text = typeof body.text === "string" ? body.text : body.body;
      if (typeof text !== "string" || text.length === 0 || typeof body.turn_id !== "string") return response({ error: "text and turn_id are required" }, 400);
      const message = store.addTalkMessage({ role: "foreman", body: text, metadata: { turn_id: body.turn_id, by: typeof body.by === "string" ? body.by : "foreman" } });
      events.publish("talk.foreman.replied", message);
      return response({ state: "accepted", message }, 201);
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
      return response(store.legacyArtifacts().slice(-Math.max(1, limit)));
    }
    if (url.pathname === "/api/web/secrets" && request.method === "GET") {
      return response(store.listSecretRequests().map((secret) => ({ id: secret.id, key: secret.name, configured: secret.status === "available", status: secret.status, usage_scope: secret.metadata.scope, request: { id: secret.id, project_id: secret.projectId, status: secret.status, purpose: secret.metadata.purpose, scope: secret.metadata.scope } })));
    }
    if (url.pathname === "/api/web/talk" && request.method === "POST") {
      const body = await readJson(request);
      const text = typeof body.text === "string" ? body.text : body.body;
      if (typeof text !== "string" || text.length === 0) return response({ error: "text is required" }, 400);
      const message = store.addTalkMessage({ body: text });
      events.publish("talk.message.created", message);
      return response({ state: "accepted", message }, 201);
    }
    if (url.pathname === "/api/web/bootstrap" && request.method === "GET") {
      return response({ version: "backend-v1", connection: { state: "connected" }, status: { at: new Date().toISOString() },
        graph: { factory: { scope: "factory", id: "factory", title: "Factory", nodes: [], edges: [] } },
        assets: store.legacyArtifacts(), logs: store.legacyEvents(), talk: [...store.legacyTalk(), ...store.listTalkMessages()],
        harnesses: officialHarnesses().map((item) => ({ ...item, configuration: publicProfile(store.listHarnessProfiles().find((profile) => profile.runtime === item.id)) })), harness_profiles: store.listHarnessProfiles().map(publicProfile), secrets: store.listSecretRequests().map((secret) => ({ id: secret.id, key: secret.name, configured: secret.status === "available", status: secret.status, request: { id: secret.id, project_id: secret.projectId, status: secret.status, purpose: secret.metadata.purpose, scope: secret.metadata.scope } })),
        foreman: { configured: false }, constitution: { available: false } });
    }
    if (url.pathname === "/api/v1/events" && request.method === "GET") return eventsResponse(request, events, url.searchParams.get("cursor") ?? undefined);
    if (request.method === "GET" && !url.pathname.startsWith("/api/")) return staticResponse(url.pathname);
    return response({ error: "not found" }, 404);
  };
  return { store, events, vault, fetch, stop: () => store.close() };
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

function graphProjection(store: SqliteStore, scope: string, workarea: string | null) {
  const systems = store.listBusinessSystems();
  const projects = store.listProjects();
  if (scope === "workarea" && workarea) {
    const selectedProjects = projects.filter((project) => project.businessSystemId === workarea || project.id === workarea);
    const nodes = selectedProjects.flatMap((project, index) => [
      { id: `${project.id}:operator`, type: "operator", group: project.id, label: "Operator", position: { x: 170, y: 130 + index * 180 }, status: "ready", metadata: { project: project.name, harness: "official" } },
      { id: `${project.id}:machine`, type: "machine", group: project.id, label: project.name, position: { x: 470, y: 130 + index * 180 }, status: "ready", metadata: { project: project.name, sources: project.sources.length } },
    ]);
    const edges = selectedProjects.map((project) => ({ id: `${project.id}:operator-to-machine`, source: `${project.id}:operator`, target: `${project.id}:machine`, kind: "material", condition: "业务物料", status: "ready" }));
    return { scope: "workarea", id: workarea, title: systems.find((system) => system.id === workarea)?.name ?? workarea, nodes, edges };
  }
  return { scope: "factory", id: "factory", title: "工厂", nodes: systems.map((system, index) => ({ id: system.id, type: "workarea", group: "factory", label: system.name, position: { x: 180 + (index % 3) * 300, y: 160 + Math.floor(index / 3) * 200 }, status: system.status === "active" ? "ready" : "unknown", metadata: { description: system.description, project_count: projects.filter((project) => project.businessSystemId === system.id).length } })), edges: [] };
}

async function readJson(request: Request): Promise<Record<string, any>> {
  try { const value = await request.json(); return value && typeof value === "object" ? value as Record<string, any> : {}; } catch { return {}; }
}

function eventsResponse(request: Request, events: EventBus, cursor?: string): Response {
  const last = request.headers.get("last-event-id") ?? cursor;
  let closeStream = () => {};
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const encoder = new TextEncoder();
      const send = (event: any) => {
        const data = event.data && typeof event.data === "object" ? { ...event.data, type: event.type } : { type: event.type, value: event.data };
        controller.enqueue(encoder.encode(`id: ${event.seq}\nevent: mao.event\ndata: ${JSON.stringify(data)}\n\n`));
      };
      for (const event of events.after(last)) send(event);
      const unsubscribe = events.subscribe(send);
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

