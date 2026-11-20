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
