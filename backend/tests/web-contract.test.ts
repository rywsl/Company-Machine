import { expect, test } from "bun:test";
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
