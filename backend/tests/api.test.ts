import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rm } from "node:fs/promises";
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, renameSync, rmdirSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createApp } from "../src/server";
import { SqliteStore } from "../src/db";
import { FakeClock } from "../src/legacy/foundation";
import { VaultStore } from "../src/vault";

async function jsonResponse(response: Response) {
  return response.json() as Promise<Record<string, any>>;
}

describe("Bun business backend", () => {
  test("persists and reloads a validated Harness timeout from an older SQLite profile", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-harness-timeout-"));
    const dbPath = join(root, "factory.db");
    const old = new SqliteStore(dbPath, dbPath);
    old.saveHarnessProfile({ runtime: "claude-code", role: "foreman", baseUrl: "", model: "fixture", mode: "session" });
    old.db.exec("ALTER TABLE harness_profiles DROP COLUMN session_timeout_s");
    old.close();
    const app = createApp({ dbPath, legacyPath: dbPath, storageRoot: root, dispatchWork: false });
    const configure = (fields: object) => app.fetch(new Request("http://localhost/api/web/harnesses/configure", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ runtime: "claude-code", role: "foreman", model: "fixture", ...fields }),
    }));
    try {
      expect(app.store.listHarnessProfiles()[0].sessionTimeoutSeconds).toBe(600);
      for (const value of [0, 2_147_484, 1.5, "2", null]) {
        expect((await configure({ session_timeout_s: value })).status).toBe(400);
      }
      const saved = await configure({ session_timeout_s: 45 });
      expect((await jsonResponse(saved)).profile.session_timeout_s).toBe(45);
      expect((await jsonResponse(await configure({ model: "other" }))).profile.session_timeout_s).toBe(45);
      const reopened = new SqliteStore(dbPath, dbPath);
      try { expect(reopened.listHarnessProfiles()[0]).toMatchObject({ sessionTimeoutSeconds: 45, model: "other" }); }
      finally { reopened.close(); }
    } finally { app.stop(); await rm(root, { recursive: true, force: true }); }
  });

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
    const notifications: string[] = [];
    app.events.subscribe(({ type }) => notifications.push(type));
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

      app.store.db.exec(`
        DROP TRIGGER reject_intake;
        CREATE TRIGGER reject_intake_event BEFORE INSERT ON business_events WHEN NEW.type='foreman.message.queued'
        BEGIN SELECT RAISE(ABORT, 'cannot record intake event'); END;
      `);
      await expect(Promise.resolve(intake())).rejects.toThrow("cannot record intake event");
      expect(app.store.listBusinessSystems()).toEqual([]);
      expect(app.store.listProjects()).toEqual([]);
      expect(app.store.listTalkMessages()).toEqual([]);
      expect(app.store.listForemanInbox()).toEqual([]);
      expect(app.events.after()).toEqual([]);
      expect(notifications).toEqual([]);

      app.store.db.exec("DROP TRIGGER reject_intake_event");
      expect((await intake()).status).toBe(201);
      expect(notifications).toEqual(["business-system.created", "project.intake.created", "talk.message.created", "foreman.message.queued"]);
    } finally { app.stop(); }
  });

  test("keeps repository credentials out of project intake and public project views", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
    const intake = (repo_url: string) => app.fetch(new Request("http://localhost/api/v1/projects/intake", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: "Repository", repo_url }),
    }));
    const repository = "https://example.test/team/repo.git";
    const historic = "https://legacy-user:legacy-value@example.test/team/repo.git?signature=legacy-query#legacy-fragment";
    try {
      for (const url of [historic, repository + "?token=value", "ssh://git:password@example.test/team/repo.git"]) {
        expect((await intake(url)).status).toBe(400);
      }
      expect(app.store.listProjects()).toEqual([]);
      expect(app.events.after()).toEqual([]);

      app.store.createIntake({ name: "Historical repository", repo_url: historic });
      const ssh = "git@example.test:team/repo.git";
      expect((await intake(ssh)).status).toBe(201);
      const visible = await app.fetch(new Request("http://localhost/api/v1/projects"));
      const projects = await visible.json() as Array<{ sources: Array<{ repoUrl: string }> }>;
      expect(projects.map(({ sources }) => sources[0].repoUrl).sort()).toEqual([repository, ssh].sort());
    } finally { app.stop(); }
  });

  test("persists chat and its Foreman queue events together before notifying subscribers", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-atomic-talk-"));
    const dbPath = join(root, "business.db");
    const app = createApp({ dbPath, legacyPath: dbPath, dispatchWork: false });
    const reader = new SqliteStore(dbPath, dbPath);
    const observed: Array<{ type: string; messages: number; queued: number }> = [];
    app.events.subscribe(({ type }) => observed.push({
      type, messages: reader.listTalkMessages().length, queued: reader.listForemanInbox().length,
    }));
    const paths = ["/api/v1/talk", "/api/web/talk"];
    const send = (path: string) => app.fetch(new Request("http://localhost" + path, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: "Build the system" }),
    }));
    try {
      app.store.db.exec(`
        CREATE TRIGGER reject_talk_event BEFORE INSERT ON business_events WHEN NEW.type='foreman.message.queued'
        BEGIN SELECT RAISE(ABORT, 'cannot persist queue event'); END;
      `);
      for (const path of paths) await expect(Promise.resolve(send(path))).rejects.toThrow("cannot persist queue event");
      expect(reader.listTalkMessages()).toEqual([]);
      expect(reader.listForemanInbox()).toEqual([]);
      expect(reader.eventsAfter()).toEqual([]);
      expect(observed).toEqual([]);

      app.store.db.exec("DROP TRIGGER reject_talk_event");
      for (const path of paths) expect((await send(path)).status).toBe(201);
      expect(observed).toEqual([
        { type: "talk.message.created", messages: 1, queued: 1 },
        { type: "foreman.message.queued", messages: 1, queued: 1 },
        { type: "talk.message.created", messages: 2, queued: 2 },
        { type: "foreman.message.queued", messages: 2, queued: 2 },
      ]);
    } finally {
      reader.close();
      app.stop();
      await rm(root, { recursive: true, force: true });
    }
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

  test("limits asset summaries before projection and preserves active and archived order", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-recent-assets-"));
    const legacyPath = join(root, "archived assets.db");
    const archive = new SqliteStore(legacyPath, legacyPath);
    try {
      archive.db.exec(`
        CREATE TABLE asset_records (id TEXT PRIMARY KEY, document TEXT NOT NULL, by_actor TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO asset_records (id,document,by_actor,created_at) VALUES
          ('legacy-z','{"title":"Earlier archive"}','fixture','2026-01-02'),
          ('legacy-a','{"title":"Later archive"}','fixture','2026-01-02');
      `);
    } finally { archive.close(); }
    const app = createApp({
      dbPath: join(root, "business.db"), legacyPath, storageRoot: root, dispatchWork: false,
      clock: new FakeClock(new Date("2026-01-01T00:00:00Z")),
      runtime: { health: async () => ({ available: false, platform: "linux", executable: "fixture", machine: null, rootless: null }) },
    });
    try {
      const system = app.store.createBusinessSystem({ name: "Assets" });
      const project = app.store.createIntake({ name: "Project", business_system_id: system.id });
      const digest = "sha256:" + "a".repeat(64);
      const file = { digest, ref: "cas://" + digest, path: "output/report.md" };
      const record = { projectId: project.id, kind: "report", ref: "mao://report", metadata: {}, files: [file] };
      app.store.addArtifact(record, "active-z");
      app.store.addArtifact(record, "active-a");
      app.store.registerArtifactFiles("active-z", [{ position: 0, digest, size: 42 }]);
      const request = (path: string) => app.fetch(new Request("http://localhost" + path));
      const full = (await (await request("/api/web/bootstrap")).json() as { assets: Array<{ id: string; files: Array<{ download?: string }> }> }).assets;
      expect(full.map(({ id }) => id)).toEqual(["legacy-z", "legacy-a", "active-a", "active-z"]);
      expect(full.at(-1)?.files[0].download).toBe(`/api/v1/projects/${project.id}/artifacts/active-z/files/${digest}`);
      expect(full.at(-2)?.files[0].download).toBeUndefined();
      for (const limit of [1, 2, 3, 5, 2.9, 0]) {
        const response = await request("/api/web/assets?limit=" + limit);
        expect(await response.json()).toEqual(full.slice(-Math.max(1, limit)));
      }
      const window = await jsonResponse(await request("/api/web/bootstrap?assetLimit=1"));
      expect(window.assets).toEqual(full.slice(-1));
      expect(window.assetTotal).toBe(4);
      expect(typeof window.assetOlderCursor).toBe("string");
      expect((await jsonResponse(await request("/api/web/assets/page?limit=1")))).toEqual({
        items: window.assets, total: window.assetTotal, olderCursor: window.assetOlderCursor,
      });

      app.store.addArtifact(record, "active-zz");
      const middle = await jsonResponse(await request("/api/web/assets/page?limit=2&older=" + window.assetOlderCursor));
      expect(middle.items).toEqual(full.slice(1, 3));
      expect(middle.total).toBe(5);
      expect(typeof middle.olderCursor).toBe("string");
      const oldest = await jsonResponse(await request("/api/web/assets/page?limit=2&older=" + middle.olderCursor));
      expect(oldest).toEqual({ items: full.slice(0, 1), total: 5, olderCursor: null });
      const refreshed = await jsonResponse(await request("/api/web/assets/page?limit=1"));
      expect(refreshed.items[0].id).toBe("active-zz");
      expect(refreshed.total).toBe(5);
      expect((await request("/api/web/assets/page?limit=101")).status).toBe(400);
      expect((await request("/api/web/assets/page?older=malformed")).status).toBe(400);
      expect((await request("/api/web/bootstrap?assetLimit=0")).status).toBe(400);
    } finally {
      app.stop();
      await rm(root, { recursive: true, force: true });
    }
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

  test("projects factory graph summaries while preserving the authored DAG on the full business API", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false, clock: new FakeClock() });
    try {
      const authored = app.store.createBusinessSystem({ name: "Authored DAG", description: "Factory summary" });
      const empty = app.store.createBusinessSystem({ name: "Empty system" });
      app.store.createIntake({ business_system_id: authored.id, name: "Project one" });
      app.store.createIntake({ business_system_id: authored.id, name: "Project two" });
      const graph = { nodes: [{ id: "operator" }, { id: "machine" }], edges: [{ source: "operator", target: "machine" }] };
      const graphResponse = await app.fetch(new Request("http://localhost/api/v1/business-systems/" + authored.id + "/graph", {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ graph }),
      }));
      expect(graphResponse.status).toBe(200);

      const queries: string[] = [];
      const query = app.store.db.query.bind(app.store.db);
      app.store.db.query = <T,>(sql: string) => {
        queries.push(sql);
        return query<T>(sql);
      };
      const projected = await app.fetch(new Request("http://localhost/api/web/graph?scope=factory"));
      expect(projected.status).toBe(200);
      expect(await projected.json()).toEqual({
        scope: "factory", id: "factory", title: "工厂",
        nodes: [
          { id: authored.id, type: "workarea", group: "factory", label: "Authored DAG", position: { x: 180, y: 160 }, status: "ready", metadata: { description: "Factory summary", project_count: 2 } },
          { id: empty.id, type: "workarea", group: "factory", label: "Empty system", position: { x: 480, y: 160 }, status: "ready", metadata: { project_count: 0 } },
        ],
        edges: [],
      });
      expect(queries.filter((sql) => /FROM business_systems/i.test(sql))).toEqual([
        "SELECT id, name, description, status FROM business_systems ORDER BY created_at",
      ]);
      const full = await app.fetch(new Request("http://localhost/api/v1/business-systems"));
      expect(full.status).toBe(200);
      const systems = await full.json() as Array<{ id: string; metadata: object }>;
      expect(systems.find(({ id }) => id === authored.id)?.metadata).toMatchObject({ graph });
    } finally { app.stop(); }
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
    const app = createApp({ dbPath, legacyPath: dbPath, runtime: {
      health: async () => ({ available: true, platform: "linux", executable: "fixture", machine: null, rootless: null }),
    } });
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
      const bootstrap = await jsonResponse(await app.fetch(new Request("http://localhost/api/web/bootstrap")));
      expect(bootstrap.secrets.find((secret: { key: string }) => secret.key === "VIDEO_API_KEY")).toMatchObject({
        key: "VIDEO_API_KEY", configured: false,
        request: { project_id: project.id, project_name: "视频项目" },
      });
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

  test("commits resource availability, event and work resumption together after the vault write", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-secret-resolution-"));
    const dbPath = join(root, "factory.db");
    const vault = new VaultStore(join(root, "vault.json"), join(root, "vault.key"));
    const app = createApp({ dbPath, legacyPath: dbPath, storageRoot: root, dispatchWork: false, vault });
    const reader = new SqliteStore(dbPath, dbPath);
    try {
      const system = app.store.createBusinessSystem({ name: "Secret transaction" });
      const project = app.store.createIntake({ business_system_id: system.id, name: "Fixture project" });
      const run = app.store.createBusinessRun(system.id, "test", {}, [], true)!;
      const unit = app.store.claimUnitRun(run.id, `${project.id}:operator`, "waiting")!;
      const resource = app.store.createSecretRequest({ projectId: project.id, name: "PROJECT_API_KEY", vaultRef: "vault://fixture/project" });
      app.store.linkUnitSecret(unit.id, resource.id);
      app.store.completeUnitRun(unit.id, "waiting_resources", ["material://retained"]);
      const submit = (value: string) => app.fetch(new Request(`http://localhost/api/v1/secrets/requests/${resource.id}/value`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ value }),
      }));
      const notifications: Array<{ status?: string; unitStatus?: string }> = [];
      app.events.subscribe(({ type }) => {
        if (type === "secret.available") notifications.push({
          status: reader.getSecretRequest(resource.id)?.status,
          unitStatus: reader.getUnitRun(run.id, unit.id)?.status,
        });
      });
      app.store.db.exec(`
        CREATE TRIGGER reject_available_event BEFORE INSERT ON business_events WHEN NEW.type='secret.available'
        BEGIN SELECT RAISE(ABORT, 'cannot persist resource event'); END;
      `);
      await expect(submit("before-retry")).rejects.toThrow("cannot persist resource event");
      expect(vault.get(resource.vaultRef)).toBe("before-retry");
      expect(reader.getSecretRequest(resource.id)?.status).toBe("requested");
      expect(reader.getUnitRun(run.id, unit.id)).toMatchObject({ status: "waiting_resources", sessionId: "waiting" });
      expect(reader.eventsAfter()).toEqual([]);
      expect(notifications).toEqual([]);

      app.store.db.exec("DROP TRIGGER reject_available_event");
      expect((await submit("after-retry")).status).toBe(200);
      expect(reader.getUnitRun(run.id, unit.id)).toMatchObject({ status: "queued", outputs: ["material://retained"] });
      expect(reader.getUnitRun(run.id, unit.id)?.sessionId).toBeUndefined();
      expect(notifications).toEqual([{ status: "available", unitStatus: "queued" }]);
      expect(reader.eventsAfter()[0]).toMatchObject({
        type: "secret.available", data: { id: resource.id, projectId: project.id, name: resource.name, status: "available" },
      });
      expect(JSON.stringify(reader.eventsAfter())).not.toContain(resource.vaultRef);
      expect(JSON.stringify(reader.eventsAfter())).not.toContain("after-retry");
      expect((await submit("rotated-value")).status).toBe(200);
      expect(vault.get(resource.vaultRef)).toBe("rotated-value");
      expect(reader.eventsAfter()).toHaveLength(2);
    } finally {
      reader.close();
      app.stop();
      await rm(root, { recursive: true, force: true });
    }
  });

  test("preserves committed vault values when publication fails or the vault is damaged", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-vault-publication-"));
    const path = join(root, "vault.json");
    const previous = join(root, "previous.json");
    const key = join(root, "vault.key");
    const first = "vault://fixture/first";
    const second = "vault://fixture/second";
    const clock = new FakeClock();
    try {
      const vault = new VaultStore(path, key, clock);
      expect(vault.get(first)).toBeUndefined();
      expect(vault.has(first)).toBe(false);
      vault.put(first, "first-value");
      vault.put(second, "second-value");
      const committed = readFileSync(path, "utf8");
      expect(committed).not.toContain("first-value");
      if (process.platform !== "win32") expect(statSync(path).mode & 0o777).toBe(0o600);
      const incomplete = path + ".unrelated.part";
      writeFileSync(incomplete, "incomplete", "utf8");
      expect(new VaultStore(path, key).get(first)).toBe("first-value");
      expect(new VaultStore(path, key).get(second)).toBe("second-value");

      const failedPublication = new VaultStore(path, key, {
        now() {
          renameSync(path, previous);
          mkdirSync(path);
          return clock.now();
        },
        monotonic: () => clock.monotonic(),
        sleep: (seconds) => clock.sleep(seconds),
      });
      expect(() => failedPublication.put(first, "must-not-replace")).toThrow();
      expect(readFileSync(previous, "utf8")).toBe(committed);
      expect(readdirSync(root).sort()).toEqual(["previous.json", "vault.json", "vault.json.unrelated.part", "vault.key"]);
      expect(() => vault.has(first)).toThrow();
      rmdirSync(path);
      renameSync(previous, path);
      expect(vault.get(first)).toBe("first-value");

      for (const damaged of ["{", "[]", JSON.stringify({ [first]: { ref: first } })]) {
        writeFileSync(path, damaged, "utf8");
        expect(() => vault.get(second)).toThrow(/vault (file|record) is invalid/);
        expect(() => vault.has(second)).toThrow(/vault (file|record) is invalid/);
        expect(() => vault.put(second, "must-not-overwrite")).toThrow(/vault (file|record) is invalid/);
        expect(readFileSync(path, "utf8")).toBe(damaged);
      }
      writeFileSync(path, committed, "utf8");
      vault.put(first, "rotated");
      const restored = new VaultStore(path, key);
      expect(restored.get(first)).toBe("rotated");
      expect(restored.get(second)).toBe("second-value");
      expect(readFileSync(incomplete, "utf8")).toBe("incomplete");
    } finally {
      await rm(root, { recursive: true, force: true });
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
