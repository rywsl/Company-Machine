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
    expect(await response.json()).toMatchObject({ state: "accepted", eventCursor: app.store.latestEventSequence() });
  } finally { app.stop(); }
});

test("public artifact projection redacts metadata secrets", async () => {
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
  try {
    const project = app.store.createIntake({ name: "Secrets", skip_default_units: true });
    const response = await app.fetch(new Request(`http://localhost/api/v1/projects/${project.id}/artifacts`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ kind: "report", ref: "material://report", summary: "Report", metadata: { api_key: "TOPSECRET" } }),
    }));
    expect(response.status).toBe(201);
    const value = await response.json() as { metadata: { api_key: string } };
    expect(value.metadata.api_key).toBe("[REDACTED]");
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
    expect(workarea.nodes.every((node) => node.type === "operator" || node.type === "machine")).toBe(true);
    expect(workarea.edges.some((edge) => edge.kind === "material")).toBe(true);
  } finally {
    app.stop();
    rmSync(root, { recursive: true, force: true });
  }
});
