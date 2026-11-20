import { expect, test } from "bun:test";
import { createApp } from "../src/server";

test("scoped credentials cannot cross business systems or mutate global control", async () => {
  const previous = {
    token: process.env.MAO_AUTH_TOKEN,
    scopes: process.env.MAO_AUTH_SCOPES_JSON,
    csrf: process.env.MAO_CSRF_TOKEN,
  };
  process.env.MAO_AUTH_TOKEN = "scope-token";
  process.env.MAO_AUTH_SCOPES_JSON = JSON.stringify({
    "scope-token": { role: "operator", business_systems: ["allowed-system"], projects: ["allowed-project"] },
  });
  process.env.MAO_CSRF_TOKEN = "scope-csrf";
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
  try {
    const allowed = app.store.createBusinessSystem({ id: "allowed-system", name: "Allowed" });
    const denied = app.store.createBusinessSystem({ id: "denied-system", name: "Denied" });
    const allowedProject = app.store.createIntake({ business_system_id: allowed.id, name: "Allowed project" }, { projectId: "allowed-project", sourceIds: [] });
    const deniedProject = app.store.createIntake({ business_system_id: denied.id, name: "Denied project" }, { projectId: "denied-project", sourceIds: [] });
    const headers = { authorization: "Bearer scope-token", "x-mao-csrf": "scope-csrf", "content-type": "application/json" };
    expect((await app.fetch(new Request(`http://localhost/api/v1/business-systems/${allowed.id}`, { headers }))).status).toBe(200);
    expect((await app.fetch(new Request(`http://localhost/api/v1/business-systems/${denied.id}`, { headers }))).status).toBe(403);
    expect((await app.fetch(new Request(`http://localhost/api/v1/projects/${deniedProject.id}/artifacts`, { headers }))).status).toBe(403);
    expect((await app.fetch(new Request(`http://localhost/api/v1/business-systems/${allowed.id}/graph`, {
      method: "POST", headers, body: JSON.stringify({ graph: { nodes: [], edges: [] } }),
    }))).status).toBe(403);
    expect((await app.fetch(new Request(`http://localhost/api/v1/business-systems/${allowed.id}/runs`, {
      method: "POST", headers, body: JSON.stringify({ dispatch: false }),
    }))).status).toBe(201);
    expect((await app.fetch(new Request("http://localhost/api/v1/secrets/requests", {
      method: "POST", headers, body: JSON.stringify({ project_id: allowedProject.id, name: "ALLOWED_KEY" }),
    }))).status).toBe(201);
    expect((await app.fetch(new Request("http://localhost/api/web/harnesses/configure", {
      method: "POST", headers, body: JSON.stringify({ runtime: "codex", role: "operator", model: "fixture" }),
    }))).status).toBe(403);
    const deniedSecret = app.store.createSecretRequest({ projectId: deniedProject.id, name: "DENIED_KEY", vaultRef: "vault://denied" });
    expect((await app.fetch(new Request(`http://localhost/api/v1/secrets/requests/${deniedSecret.id}/audit`, { headers }))).status).toBe(403);
    expect((await app.fetch(new Request("http://localhost/api/web/secrets", { headers }))).status).toBe(403);
    expect((await app.fetch(new Request("http://localhost/api/web/harnesses", { headers }))).status).toBe(403);
    process.env.MAO_AUTH_SCOPES_JSON = JSON.stringify({
      "scope-token": { role: "viewer", business_systems: [allowed.id], projects: [allowedProject.id] },
    });
    const bootstrap = await app.fetch(new Request("http://localhost/api/web/bootstrap", { headers }));
    expect(bootstrap.status).toBe(200);
    const bootstrapBody = await bootstrap.json() as { graph: { factory: { nodes: Array<{ id: string }> } }; talk: Array<{ body?: string }> };
    expect(bootstrapBody.graph.factory.nodes.map(({ id }) => id)).toEqual([allowed.id]);
    expect(bootstrapBody.talk).toEqual([]);
    expect((await app.fetch(new Request(`http://localhost/api/v1/business-systems/${allowed.id}/runs`, {
      method: "POST", headers, body: JSON.stringify({ dispatch: false }),
    }))).status).toBe(403);
    const missingCsrf = { authorization: "Bearer scope-token", "content-type": "application/json" };
    expect((await app.fetch(new Request(`http://localhost/api/v1/business-systems/${allowed.id}/graph`, {
      method: "POST", headers: missingCsrf, body: JSON.stringify({ graph: { nodes: [], edges: [] } }),
    }))).status).toBe(401);
    expect(allowedProject.id).toBe("allowed-project");
  } finally {
    app.stop();
    if (previous.token === undefined) delete process.env.MAO_AUTH_TOKEN; else process.env.MAO_AUTH_TOKEN = previous.token;
    if (previous.scopes === undefined) delete process.env.MAO_AUTH_SCOPES_JSON; else process.env.MAO_AUTH_SCOPES_JSON = previous.scopes;
    if (previous.csrf === undefined) delete process.env.MAO_CSRF_TOKEN; else process.env.MAO_CSRF_TOKEN = previous.csrf;
  }
});

test("scoped SSE filters foreign events and redacts event payloads", async () => {
  const previous = { scopes: process.env.MAO_AUTH_SCOPES_JSON, csrf: process.env.MAO_CSRF_TOKEN };
  process.env.MAO_AUTH_SCOPES_JSON = JSON.stringify({ viewer: { role: "viewer", business_systems: ["allowed"] } });
  process.env.MAO_CSRF_TOKEN = "csrf";
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
  try {
    app.store.createBusinessSystem({ id: "allowed", name: "Allowed" });
    app.store.createBusinessSystem({ id: "denied", name: "Denied" });
    app.events.publish("business-system.created", { id: "denied", secret: "hidden" });
    app.events.publish("business-system.created", { id: "allowed", api_key: "visible only as redacted" });
    const response = await app.fetch(new Request("http://localhost/api/v1/events", { headers: { authorization: "Bearer viewer" } }));
    expect(response.status).toBe(200);
    const reader = response.body!.getReader();
    const first = await reader.read();
    await reader.cancel();
    const text = new TextDecoder().decode(first.value);
    expect(text).toContain('"id":"allowed"');
    expect(text).toContain("[REDACTED]");
    expect(text).not.toContain("denied");
    expect(text).not.toContain("hidden");
  } finally {
    app.stop();
    if (previous.scopes === undefined) delete process.env.MAO_AUTH_SCOPES_JSON; else process.env.MAO_AUTH_SCOPES_JSON = previous.scopes;
    if (previous.csrf === undefined) delete process.env.MAO_CSRF_TOKEN; else process.env.MAO_CSRF_TOKEN = previous.csrf;
  }
});

test("scoped Foreman talk requires and enforces a business context", async () => {
  const previous = { scopes: process.env.MAO_AUTH_SCOPES_JSON, csrf: process.env.MAO_CSRF_TOKEN };
  process.env.MAO_AUTH_SCOPES_JSON = JSON.stringify({ foreman: { role: "foreman", business_systems: ["allowed"] } });
  process.env.MAO_CSRF_TOKEN = "csrf";
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
  try {
    app.store.createBusinessSystem({ id: "allowed", name: "Allowed" });
    app.store.createBusinessSystem({ id: "denied", name: "Denied" });
    const headers = { authorization: "Bearer foreman", "x-mao-csrf": "csrf", "content-type": "application/json" };
    const missing = await app.fetch(new Request("http://localhost/api/web/talk", { method: "POST", headers, body: JSON.stringify({ text: "missing scope" }) }));
    expect(missing.status).toBe(400);
    const accepted = await app.fetch(new Request("http://localhost/api/web/talk", { method: "POST", headers, body: JSON.stringify({ text: "allowed scope", business_system_id: "allowed" }) }));
    expect(accepted.status).toBe(201);
    const foreign = await app.fetch(new Request("http://localhost/api/web/talk", { method: "POST", headers, body: JSON.stringify({ text: "foreign scope", business_system_id: "denied" }) }));
    expect(foreign.status).toBe(403);
    const visibleTalk = await app.fetch(new Request("http://localhost/api/web/talk", { headers }));
    expect(visibleTalk.status).toBe(200);
    const visibleTalkBody = await visibleTalk.json() as Array<{ body?: string }>;
    expect(visibleTalkBody.some(({ body }) => body === "allowed scope")).toBe(true);
    expect(visibleTalkBody.some(({ body }) => body === "foreign scope")).toBe(false);
  } finally {
    app.stop();
    if (previous.scopes === undefined) delete process.env.MAO_AUTH_SCOPES_JSON; else process.env.MAO_AUTH_SCOPES_JSON = previous.scopes;
    if (previous.csrf === undefined) delete process.env.MAO_CSRF_TOKEN; else process.env.MAO_CSRF_TOKEN = previous.csrf;
  }
});
