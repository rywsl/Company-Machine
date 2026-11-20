import { expect, test } from "bun:test";
import { cpSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { createApp } from "../src/server";

test("web graph validates scope, workarea and edge kinds", async () => {
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
  try {
    const system = app.store.createBusinessSystem({ name: "Graph contract" });
    expect((await app.fetch(new Request("http://localhost/api/web/graph?scope=unknown"))).status).toBe(400);
    expect((await app.fetch(new Request("http://localhost/api/web/graph?scope=workarea"))).status).toBe(400);
    expect((await app.fetch(new Request("http://localhost/api/web/graph?scope=workarea&workarea=missing"))).status).toBe(404);
    const project = app.store.createIntake({ business_system_id: system.id, name: "Project", repo_url: "https://example.test/project.git" });
    const dangling = await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/graph`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ graph: { nodes: [{ id: `${project.id}:operator` }, { id: "ghost" }], edges: [{ source: "ghost", target: `${project.id}:operator` }] } }),
    }));
    expect(dangling.status).toBe(400);
    const result = await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/graph`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ graph: { nodes: [{ id: `${project.id}:operator` }], edges: [{ source: `${project.id}:operator`, target: `${project.id}:operator`, kind: "invalid" }] } }),
    }));
    expect(result.status).toBe(400);
  } finally { app.stop(); }
});

test("web logs honor an incremental cursor and talk replies expose the event cursor", async () => {
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
  try {
    app.events.publish("first", { message: "one" });
    const cursor = app.store.latestEventSequence();
    app.events.publish("second", { message: "two" });
    const logs = await (await app.fetch(new Request(`http://localhost/api/web/logs?since=${cursor}`))).json() as Array<{ kind: string }>;
    expect(logs.map(({ kind }) => kind)).toEqual(["second"]);
    const response = await app.fetch(new Request("http://localhost/api/web/talk", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: "hello" }),
    }));
    expect(response.status).toBe(201);
    expect(await response.json()).toMatchObject({ state: "waiting_foreman", setup_required: true, eventCursor: app.store.latestEventSequence() });
  } finally { app.stop(); }
});

test("public artifact projection redacts metadata secrets", async () => {
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
  try {
    const project = app.store.createIntake({ name: "Secrets", skip_default_units: true });
    const response = await app.fetch(new Request(`http://localhost/api/v1/projects/${project.id}/artifacts`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ kind: "report", ref: "material://report", summary: "Report", metadata: { api_key: "TOPSECRET" }, files: [
        { ref: "cas://relative", path: "reports/output.md" },
        { ref: "file:///srv/private/output.md", path: "/srv/private/output.md" },
      ] }),
    }));
    expect(response.status).toBe(201);
    const value = await response.json() as { metadata: { api_key: string }; files: Array<Record<string, unknown>> };
    expect(value.metadata.api_key).toBe("[REDACTED]");
    expect(value.files).toEqual([
      { ref: "cas://relative", path: "reports/output.md" },
      { ref: "[REDACTED]" },
    ]);
  } finally { app.stop(); }
});

test("asset projection exposes a declared validation epoch without exposing private metadata", async () => {
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:" });
  try {
    const system = app.store.createBusinessSystem({ name: "Validation epoch" });
    const project = app.store.createIntake({ business_system_id: system.id, name: "Validated project" });
    app.store.addArtifact({ projectId: project.id, kind: "report", recordType: "material", ref: "material://validated", summary: "Validated", metadata: { validation_epoch: "e-test", api_key: "hidden" } });
    const response = await app.fetch(new Request("http://localhost/api/web/assets"));
    expect(response.status).toBe(200);
    expect((await response.json()).find((item: { ref: string }) => item.ref === "material://validated")).toMatchObject({ validation_epoch: "e-test" });
    expect(JSON.stringify(await (await app.fetch(new Request("http://localhost/api/web/assets"))).json())).not.toContain("hidden");
  } finally { app.stop(); }
});

test("public business projections redact local source and execution references", async () => {
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
  try {
    const system = app.store.createBusinessSystem({ name: "Portable references" });
    const project = app.store.createIntake({ business_system_id: system.id, name: "Project", uploaded_file_refs: ["/srv/private/input.txt"] });
    const run = app.store.createBusinessRun(system.id, "web", {}, ["file:///srv/private/input.txt", "/srv/private/output.txt"], false)!;
    app.store.createBusinessConversion({
      businessSystemId: system.id, kind: "path-test", summary: "Path test", status: "completed",
      inputs: ["file:///srv/private/input.txt"], outputs: ["/srv/private/output.txt"], metadata: {},
    });
    const detail = await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}`));
    const detailBody = await detail.json() as Record<string, any>;
    expect(JSON.stringify(detailBody)).not.toContain("/srv/private");
    expect(detailBody.projects[0].sources[0].fileRef).toBe("[REDACTED]");
    const conversions = await (await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/conversions`))).json() as Array<Record<string, any>>;
    expect(conversions[0].inputs).toEqual(["[REDACTED]"]);
    expect(conversions[0].outputs).toEqual(["[REDACTED]"]);
    const runs = await (await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/runs`))).json() as Array<Record<string, any>>;
    expect(JSON.stringify(runs)).not.toContain("/srv/private");
    expect(run.id).toBe(runs[0].id);
  } finally { app.stop(); }
});

test("Harness projection distinguishes host availability from configured container transport", async () => {
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
  try {
    app.store.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "", model: "fixture", mode: "session", image: "registry.example/codex@sha256:" + "a".repeat(64) });
    const response = await app.fetch(new Request("http://localhost/api/web/harnesses"));
    expect(response.status).toBe(200);
    const codex = (await response.json() as Array<Record<string, any>>).find((item) => item.id === "codex");
    expect(codex).toMatchObject({ container_configured: true });
  } finally { app.stop(); }
});

test("factory graph projects governed Registry Lines, explicit material edges, and legacy run status", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-web-registry-"));
  const registry = join(root, "registry");
  cpSync(resolve("spec/examples/registry"), registry, { recursive: true });
  writeFileSync(join(root, "factory.json"), JSON.stringify({ governance: { registry: "registry" } }));
  writeFileSync(join(registry, "lines", "consume.yaml"), `apiVersion: mao/v1
kind: Line
metadata: {name: consume, version: 1}
spec:
  interface:
    inputs: {brief: {type: brief.video@1}}
  keep: [consume.video, consume.subtitle, consume.script]
  steps:
    consume:
      station: explainer@3
      in: {brief: $inputs.brief}
`);
  writeFileSync(join(registry, "lines", "producer.yaml"), `apiVersion: mao/v1
kind: Line
metadata: {name: producer, version: 1}
spec:
  keep: [produce.brief]
  steps:
    produce:
      station: intake-brief@1
`);
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", storageRoot: root, dispatchWork: false });
  try {
    app.store.db.exec(`CREATE TABLE runs (id TEXT PRIMARY KEY, line TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)`);
    app.store.db.query("INSERT INTO runs (id,line,state,created_at,updated_at) VALUES (?,?,?,?,?)").run("run-1", "Line:explainer-multilang@5", "FAILING", "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:01.000Z");
    const response = await app.fetch(new Request("http://localhost/api/web/graph?scope=factory"));
    expect(response.status).toBe(200);
    const graph = await response.json() as { nodes: Array<Record<string, any>>; edges: Array<Record<string, any>>; metadata: Record<string, any> };
    expect(graph.metadata).toMatchObject({ projection: "web_read_model.registry", static_source: "registry Line, Station and Machine" });
    expect(graph.nodes.map((node) => node.id)).toEqual(["Line:consume@1", "Line:explainer-multilang@5", "Line:producer@1"]);
    expect(graph.nodes.find((node) => node.id === "Line:explainer-multilang@5")).toMatchObject({ status: "exception", metadata: { active: 1, runs: 1, latest: "run-1", exception: 1 } });
    expect(graph.edges.some((edge) => edge.source === "Line:producer@1" && edge.target === "Line:consume@1" && edge.kind === "material" && JSON.stringify(edge.metadata) === JSON.stringify({ types: ["brief.video@1"] }))).toBe(true);
    const workareaResponse = await app.fetch(new Request("http://localhost/api/web/graph?scope=workarea&workarea=Line:consume@1"));
    expect(workareaResponse.status).toBe(200);
    const workarea = await workareaResponse.json() as { scope: string; id: string; nodes: Array<Record<string, any>>; edges: Array<Record<string, any>> };
    expect(workarea).toMatchObject({ scope: "workarea", id: "Line:consume@1" });
    expect(workarea.nodes).toHaveLength(2);
    expect(workarea.nodes.every((node) => node.type === "operator" || node.type === "machine")).toBe(true);
    expect(workarea.edges.some((edge) => edge.kind === "material")).toBe(true);
  } finally {
    app.stop();
    rmSync(root, { recursive: true, force: true });
  }
});

test("public business projections redact metadata secrets and keep platform runtime admin-only", async () => {
  const previousScopes = process.env.MAO_AUTH_SCOPES_JSON;
  process.env.MAO_AUTH_SCOPES_JSON = JSON.stringify({ viewer: { role: "viewer", business_systems: ["system"] } });
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:" });
  try {
    const system = app.store.createBusinessSystem({ id: "system", name: "Scoped", metadata: { api_key: "raw-system-secret" } });
    app.store.updateBusinessSystemMetadata(system.id, { graph: { nodes: [], edges: [], metadata: { api_key: "raw-graph-secret" } } });
    const headers = { authorization: "Bearer viewer" };
    const detail = await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}`, { headers }));
    expect(detail.status).toBe(200);
    const body = await detail.json() as Record<string, any>;
    expect(JSON.stringify(body)).not.toContain("raw-system-secret");
    const graph = await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/graph`, { headers }));
    expect(graph.status).toBe(200);
    expect(JSON.stringify(await graph.json())).not.toContain("raw-graph-secret");
    expect((await app.fetch(new Request("http://localhost/api/v1/runtime", { headers }))).status).toBe(403);
  } finally {
    app.stop();
    if (previousScopes === undefined) delete process.env.MAO_AUTH_SCOPES_JSON;
    else process.env.MAO_AUTH_SCOPES_JSON = previousScopes;
  }
});
