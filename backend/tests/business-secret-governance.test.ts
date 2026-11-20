import { expect, test } from "bun:test";
import { createApp } from "../src/server";

test("sanitizes credentials before persisting business records and conversions", async () => {
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
  try {
    const system = app.store.createBusinessSystem({ name: "Secret boundary" });
    const project = app.store.createIntake({ business_system_id: system.id, name: "Project", repo_url: "https://example.test/project.git" });
    const secret = "credential-value-should-not-persist";
    const artifactResponse = await app.fetch(new Request(`http://localhost/api/v1/projects/${project.id}/artifacts`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        kind: "report", ref: "material://safe", summary: `token=${secret}`,
        metadata: { api_key: secret, nested: { authorization: secret }, note: `secret=${secret}` },
      }),
    }));
    expect(artifactResponse.status).toBe(201);
    const artifact = app.store.listArtifacts(project.id)[0];
    expect(app.store.listBusinessConversions(system.id)).toHaveLength(0);
    expect(JSON.stringify(artifact)).not.toContain(secret);
    expect(artifact.metadata).toMatchObject({ api_key: "[REDACTED]", nested: { authorization: "[REDACTED]" }, note: "secret=[REDACTED]" });

    const conversionResponse = await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/conversions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        kind: "record-production", summary: `authorization=${secret}`,
        inputs: [artifact.ref], outputs: [artifact.ref], metadata: { token: secret },
      }),
    }));
    expect(conversionResponse.status).toBe(201);
    expect(JSON.stringify(app.store.listBusinessConversions(system.id))).not.toContain(secret);
    expect(app.store.listBusinessConversions(system.id)[0]).toMatchObject({ summary: "authorization=[REDACTED]", metadata: { token: "[REDACTED]" } });
  } finally {
    app.stop();
  }
});

test("sanitizes secret request metadata and audit details at the storage boundary", () => {
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
  try {
    const project = app.store.createIntake({ name: "Secret metadata", skip_default_units: true });
    const secret = "request-secret-value";
    const request = app.store.createSecretRequest({
      projectId: project.id,
      name: "PROJECT_TOKEN",
      vaultRef: "vault://request-secret",
      metadata: { api_key: secret, note: `token=${secret}` },
    });
    app.store.recordSecretAudit(request.id, "checked", "operator", undefined, { authorization: secret, note: `secret=${secret}` });
    expect(JSON.stringify(app.store.db.query("SELECT metadata FROM secret_requests").all())).not.toContain(secret);
    expect(JSON.stringify(app.store.db.query("SELECT detail FROM secret_audit").all())).not.toContain(secret);
    expect(app.store.getSecretRequest(request.id)?.metadata).toMatchObject({ api_key: "[REDACTED]", note: "token=[REDACTED]" });
    expect(app.store.listSecretAudit(request.id).find((entry) => Object.hasOwn(entry.detail, "authorization"))?.detail).toMatchObject({ authorization: "[REDACTED]", note: "secret=[REDACTED]" });
  } finally { app.stop(); }
});
