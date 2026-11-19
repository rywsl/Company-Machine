import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rm } from "node:fs/promises";
import { createApp } from "../src/server";

async function jsonResponse(response: Response) {
  return response.json() as Promise<Record<string, any>>;
}

describe("Bun business backend", () => {
  test("creates a business system, intakes a project, and persists a talk event", async () => {
    const dbPath = `.factory/test-${randomUUID()}.db`;
    const app = createApp({ dbPath, legacyPath: dbPath });
    try {
      const systemResponse = await app.fetch(new Request("http://localhost/api/v1/business-systems", {
        method: "POST",
        body: JSON.stringify({ name: "视频生产系统" }),
        headers: { "content-type": "application/json" },
      }));
      expect(systemResponse.status).toBe(201);
      const system = await jsonResponse(systemResponse);

      const intakeResponse = await app.fetch(new Request("http://localhost/api/v1/projects/intake", {
        method: "POST",
        body: JSON.stringify({ business_system_id: system.id, repo_url: "https://example.com/project.git", name: "项目" }),
        headers: { "content-type": "application/json" },
      }));
      expect(intakeResponse.status).toBe(201);
      const project = await jsonResponse(intakeResponse);
      expect(project.sources[0].kind).toBe("repo_url");

      const talkResponse = await app.fetch(new Request("http://localhost/api/v1/talk", {
        method: "POST",
        body: JSON.stringify({ text: "搭建视频生产业务系统" }),
        headers: { "content-type": "application/json" },
      }));
      expect(talkResponse.status).toBe(201);
      expect(app.events.after().map((event) => event.type)).toEqual([
        "business-system.created",
        "project.intake.created",
        "talk.message.created",
      ]);
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });

  test("stores secret values through the vault boundary and exposes only status", async () => {
    const dbPath = `.factory/test-${randomUUID()}.db`;
    const vaultPath = `.factory/test-${randomUUID()}.vault.json`;
    const keyPath = `.factory/test-${randomUUID()}.vault.key`;
    const previousVault = process.env.MAO_VAULT_PATH;
    const previousKey = process.env.MAO_VAULT_KEY_PATH;
    process.env.MAO_VAULT_PATH = vaultPath;
    process.env.MAO_VAULT_KEY_PATH = keyPath;
    const app = createApp({ dbPath, legacyPath: dbPath });
    try {
      const projectResponse = await app.fetch(new Request("http://localhost/api/v1/projects/intake", {
        method: "POST",
        body: JSON.stringify({ local_folder: "C:/projects/video", name: "视频项目" }),
        headers: { "content-type": "application/json" },
      }));
      const project = await jsonResponse(projectResponse);
      const requestResponse = await app.fetch(new Request("http://localhost/api/v1/secrets/requests", {
        method: "POST",
        body: JSON.stringify({ project_id: project.id, name: "VIDEO_API_KEY", purpose: "视频生成服务" }),
        headers: { "content-type": "application/json" },
      }));
      const request = await jsonResponse(requestResponse);
      const valueResponse = await app.fetch(new Request(`http://localhost/api/v1/secrets/requests/${request.request_id}/value`, {
        method: "POST",
        body: JSON.stringify({ value: "secret-value" }),
        headers: { "content-type": "application/json" },
      }));
      expect(valueResponse.status).toBe(200);
      expect((await jsonResponse(valueResponse)).state).toBe("available");
      expect(JSON.stringify(app.events.after())).not.toContain("secret-value");
    } finally {
      app.stop();
      if (previousVault === undefined) delete process.env.MAO_VAULT_PATH;
      else process.env.MAO_VAULT_PATH = previousVault;
      if (previousKey === undefined) delete process.env.MAO_VAULT_KEY_PATH;
      else process.env.MAO_VAULT_KEY_PATH = previousKey;
      await rm(dbPath, { force: true });
      await rm(vaultPath, { force: true });
      await rm(keyPath, { force: true });
    }
  });

  test("replays events through the shared SSE event name", async () => {
    const dbPath = `.factory/test-${randomUUID()}.db`;
    const app = createApp({ dbPath, legacyPath: dbPath });
    try {
      app.events.publish("project.intake.created", { projectId: "p-1" });
      const response = await app.fetch(new Request("http://localhost/api/v1/events?cursor=0"));
      expect(response.headers.get("content-type")).toContain("text/event-stream");
      const reader = response.body?.getReader();
      const chunk = await reader?.read();
      const text = new TextDecoder().decode(chunk?.value);
      expect(text).toContain("event: mao.event");
      expect(text).toContain('"type":"project.intake.created"');
      await reader?.cancel();
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });
});
