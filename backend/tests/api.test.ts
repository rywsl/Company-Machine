import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rm } from "node:fs/promises";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createApp } from "../src/server";
import { SqliteStore } from "../src/db";
import { FakeClock } from "../src/legacy/foundation";

async function jsonResponse(response: Response) {
  return response.json() as Promise<Record<string, any>>;
}

describe("Bun business backend", () => {
  test("uses the injected clock and UUID generator for business records and events", () => {
    const clock = new FakeClock(new Date("2026-01-02T03:04:05Z"));
    const ids = ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"];
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", clock, uuid: () => ids.shift()! });
    try {
      const system = app.store.createBusinessSystem({ name: "Timed system" });
      expect(system).toMatchObject({ id: "00000000-0000-4000-8000-000000000001", createdAt: "2026-01-02T03:04:05.000Z" });
      clock.advance(8);
      expect(app.events.publish("business-system.created", system)).toMatchObject({ id: "00000000-0000-4000-8000-000000000002", createdAt: "2026-01-02T03:04:13.000Z" });
    } finally { app.stop(); }
  });

  test("rolls back project intake when it cannot record the Foreman request", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:" });
    const intake = (business_system_id?: string) => app.fetch(new Request("http://localhost/api/v1/projects/intake", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: "Atomic intake", repo_url: "https://example.invalid/repo", business_system_id }),
    }));
    try {
      expect((await intake("unknown-system")).status).toBe(404);
      app.store.db.exec("CREATE TRIGGER reject_intake BEFORE INSERT ON foreman_outbox BEGIN SELECT RAISE(ABORT, 'cannot queue intake'); END;");
      await expect(Promise.resolve(intake())).rejects.toThrow("cannot queue intake");
      expect(app.store.listBusinessSystems()).toHaveLength(0);
      expect(app.store.listProjects()).toHaveLength(0);
      expect(app.store.listTalkMessages()).toHaveLength(0);
      expect(app.events.after()).toHaveLength(0);
    } finally { app.stop(); }
  });

  test("bounds recent conversation reads and fills unused slots from archived messages", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:" });
    try {
      app.store.db.exec(`CREATE TABLE talk_messages (id TEXT, role TEXT, body TEXT, card TEXT, created_at TEXT);
        INSERT INTO talk_messages VALUES ('a', 'user', 'earlier', '{}', '2025-01-01'), ('b', 'foreman', 'latest archived', '{}', '2025-01-02');
        INSERT INTO business_talk_messages VALUES ('c', 'user', 'active', '{}', '2026-01-01'), ('d', 'foreman', 'latest active', '{}', '2026-01-02');`);
      const fetchIds = async (path: string) => (await (await app.fetch(new Request(`http://localhost${path}`))).json() as Array<{ id: string }>).map(({ id }) => id);
      expect(await fetchIds("/api/web/talk?limit=3")).toEqual(["b", "c", "d"]);
      expect(await fetchIds("/api/web/talk?limit=1")).toEqual(["d"]);
      expect(await fetchIds("/api/v1/talk")).toEqual(["a", "b", "c", "d"]);
    } finally { app.stop(); }
  });

  test("stores uploaded project bytes under a digest and fetches them through their project", async () => {
    const storageRoot = mkdtempSync(join(tmpdir(), "mao-project-upload-"));
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", storageRoot });
    try {
      const files = new FormData();
      const bytes = new Uint8Array([0x50, 0, 0xff, 0x42]);
      files.set("name", "文件项目");
      files.append("files", new Blob([bytes]), "项目数据.bin");
      const intake = await app.fetch(new Request("http://localhost/api/v1/projects/intake", { method: "POST", body: files }));
      expect(intake.status).toBe(201);
      const created = await jsonResponse(intake);
      const source = created.sources[0];
      expect(source.metadata.name).toBe("项目数据.bin");
      expect(source.fileRef).toBe(`cas://${source.metadata.digest}`);
      const fileRoute = `/api/v1/projects/${created.id}/files/${source.metadata.digest}`;
      const download = await app.fetch(new Request(`http://localhost${fileRoute}`));
      expect(download.status).toBe(200);
      expect(download.headers.get("content-type")).toBe("application/octet-stream");
      expect(new Uint8Array(await download.arrayBuffer())).toEqual(bytes);
      expect((await app.fetch(new Request(`http://localhost${fileRoute.replace(created.id, "missing-project")}`))).status).toBe(404);
    } finally {
      app.stop();
      await rm(storageRoot, { recursive: true, force: true });
    }
  });

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
      expect(typeof project.foreman_inbox_id).toBe("string");

      const graphResponse = await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/graph`, {
        method: "POST",
        body: JSON.stringify({ graph: { nodes: [{ id: "operator" }, { id: "machine" }], edges: [{ source: "operator", target: "machine" }] } }),
        headers: { "content-type": "application/json" },
      }));
      expect(graphResponse.status).toBe(200);
      const artifactResponse = await app.fetch(new Request(`http://localhost/api/v1/projects/${project.id}/artifacts`, {
        method: "POST",
        body: JSON.stringify({ kind: "video", ref: "cas://video-1", summary: "视频成片", purpose: "交付", files: [{ ref: "cas://file-1", path: "output.mp4" }] }),
        headers: { "content-type": "application/json" },
      }));
      expect(artifactResponse.status).toBe(201);
      expect((await jsonResponse(artifactResponse)).files[0].path).toBe("output.mp4");
      const details = await jsonResponse(await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}`)));
      expect(details.artifacts).toHaveLength(1);

      const talkResponse = await app.fetch(new Request("http://localhost/api/v1/talk", {
        method: "POST",
        body: JSON.stringify({ text: "搭建视频生产业务系统" }),
        headers: { "content-type": "application/json" },
      }));
      expect(talkResponse.status).toBe(201);
      const inboxResponse = await app.fetch(new Request("http://localhost/api/v1/foreman/inbox"));
      const inbox = await inboxResponse.json() as Array<Record<string, any>>;
      expect(inbox).toHaveLength(2);
      const talkEnvelope = inbox.find((item) => item.text.includes("搭建视频生产业务系统"));
      expect(talkEnvelope?.text).toContain("搭建视频生产业务系统");
      expect(talkEnvelope?.text).not.toContain("secret-value");
      const claimed = await app.fetch(new Request(`http://localhost/api/v1/foreman/inbox/${talkEnvelope?.id}/claim`, { method: "POST" }));
      expect(claimed.status).toBe(200);
      const completed = await app.fetch(new Request(`http://localhost/api/v1/foreman/inbox/${talkEnvelope?.id}/done`, { method: "POST" }));
      expect(completed.status).toBe(200);
      expect(app.events.after().map((event) => event.type)).toEqual([
        "business-system.created",
        "project.intake.created",
        "talk.message.created",
        "foreman.message.queued",
        "business-system.graph.updated",
        "artifact.created",
        "talk.message.created",
        "foreman.message.queued",
        "foreman.message.claim",
        "foreman.message.done",
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

  test("replays multiple event pages and streams writes from a separate SQLite connection", async () => {
    const dbPath = `.factory/test-${randomUUID()}.db`;
    const app = createApp({ dbPath, legacyPath: dbPath });
    const external = new SqliteStore(dbPath, dbPath);
    const abort = new AbortController();
    try {
      for (let i = 1; i <= 141; i++) external.appendEvent("business-system.graph.updated", { i });
      const response = await app.fetch(new Request("http://localhost/api/v1/events?cursor=0", {
        headers: { "Last-Event-ID": "11" }, signal: abort.signal,
      }));
      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      const ids: number[] = [];
      for (let i = 12; i <= 141; i++) {
        const { value } = await reader.read();
        ids.push(Number(decoder.decode(value).match(/^id: (\d+)/)?.[1]));
      }
      expect(ids).toEqual(Array.from({ length: 130 }, (_, index) => index + 12));
      const next = reader.read();
      await new Promise((resolve) => setTimeout(resolve, 20));
      external.appendEvent("business-system.graph.updated", { i: 142 });
      expect(decoder.decode((await next).value)).toContain("id: 142\n");
      const pending = reader.read();
      abort.abort();
      expect(await pending).toEqual({ done: true, value: undefined });
    } finally {
      abort.abort();
      external.close();
      app.stop();
      await rm(dbPath, { force: true });
    }
  });
});
