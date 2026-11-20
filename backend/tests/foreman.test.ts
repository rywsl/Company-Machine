import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { existsSync, renameSync } from "node:fs";
import { rm } from "node:fs/promises";
import { join, resolve } from "node:path";
import { createApp } from "../src/server";
import type { OfficialProcess } from "../src/harness";

function fakeProcess(lines: string[]): OfficialProcess {
  const body = new TextEncoder().encode(`${lines.join("\n")}\n`);
  return { stdout: new ReadableStream({ start(controller) { controller.enqueue(body); controller.close(); } }), kill() {} };
}

type MailboxReply = { id: string; ok: boolean; result: unknown; error: { code: string } | null };

async function askMailbox(root: string, index: number, op: string, args: Record<string, unknown>): Promise<MailboxReply> {
  const id = "r-" + index.toString(16).padStart(32, "0");
  const request = join(root, "requests", id + ".json");
  await Bun.write(request + ".part", JSON.stringify({ id, op, args }));
  renameSync(request + ".part", request);
  const response = join(root, "responses", id + ".json");
  for (let attempt = 0; attempt < 100 && !existsSync(response); attempt++) await new Promise((done) => setTimeout(done, 10));
  return JSON.parse(await Bun.file(response).text());
}

test("applies a container Foreman mailbox request within its intake scope", async () => {
  let root: string | undefined;
  let sourcePrompt = "";
  const replies: MailboxReply[] = [];
  const app = createApp({
    dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false,
    runtime: { health: async () => ({ available: true, platform: "linux", executable: "fixture", machine: null, rootless: null }) },
    foreman: { spawn: async (argv, options) => {
      root = options.foremanMailbox;
      if (!root) throw new Error("Expected scoped mailbox");
      sourcePrompt = argv.join(" ");
      expect(options.env).not.toHaveProperty("FACTORY_DB_PATH");
      const ask = async (index: number, op: string, args: Record<string, unknown>) => {
        replies.push(await askMailbox(root!, index, op, args));
      };
      await ask(1, "business.units", { systemId: project.businessSystemId });
      await ask(2, "business.graph", { systemId: project.businessSystemId, graph });
      await ask(3, "business.graph", { systemId: other.id, graph });
      await ask(4, "vault.get", { systemId: project.businessSystemId });
      await ask(5, "business.artifact", { projectId: project.id, kind: "note", summary: "fixture", ref: "file:///tmp/unsafe" });
      await ask(6, "business.bind", { systemId: project.businessSystemId, unitId: project.id + ":operator", harness: "codex", model: "fixture-model" });
      await ask(7, "business.artifact", { projectId: project.id, kind: "note", summary: "fixture", ref: "material://note", files: [{ ref: "cas://blob" }] });
      await ask(8, "business.conversion", { systemId: project.businessSystemId, kind: "publish", summary: "fixture", inputs: ["material://note"] });
      await ask(9, "business.run", { systemId: project.businessSystemId, inputs: ["material://note"] });
      await ask(10, "project.register", { repoUrl: "https://code.example/new-system.git" });
      return fakeProcess([JSON.stringify({ type: "result", text: "已更新业务图" })]);
    } },
  });
  const other = app.store.createBusinessSystem({ name: "其他业务" });
  const graph = { nodes: [{ id: "operator" }, { id: "machine" }], edges: [{ source: "operator", target: "machine" }] };
  const intake = await app.fetch(new Request("http://localhost/api/v1/projects/intake", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name: "测试项目", repo_url: "https://example.invalid/repo" }),
  }));
  const body = await intake.json();
  const project = app.store.getProject(body.project_id)!;
  try {
    expect(intake.status).toBe(201);
    app.store.saveHarnessProfile({
      runtime: "claude-code", role: "foreman", baseUrl: "", model: "fixture", mode: "session",
      image: "registry.example/foreman@sha256:" + "a".repeat(64),
    });
    await app.foreman.dispatch(body.foreman_inbox_id);
    expect(replies.map(({ error }) => error?.code ?? null)).toEqual([
      null, null, "FOREMAN_OPERATION_INVALID", "FOREMAN_OPERATION_INVALID", "FOREMAN_OPERATION_INVALID", null, null, null, null, "FOREMAN_OPERATION_INVALID",
    ]);
    expect(replies[0].result).toHaveLength(2);
    expect(replies[5].result).toMatchObject({ id: project.id + ":operator", harness: "codex", model: "fixture-model" });
    expect(app.store.listArtifacts(project.id)[0]).toMatchObject({ ref: "material://note" });
    expect(app.store.listBusinessConversions(project.businessSystemId!)[0]).toMatchObject({ kind: "publish" });
    expect(app.store.listBusinessRuns(project.businessSystemId!)).toHaveLength(1);
    expect(app.store.getBusinessSystem(project.businessSystemId!)?.metadata.graph).toEqual(graph);
    expect(app.store.getBusinessSystem(other.id)?.metadata.graph).toBeUndefined();
    expect(sourcePrompt).toContain("/mao/foreman/requests");
    expect(sourcePrompt).not.toContain("其他业务");
    expect(app.store.getForemanMessage(body.foreman_inbox_id)?.status).toBe("done");
    expect(root && existsSync(root)).toBe(false);
  } finally { app.stop(); }
});

test("registers a repository from a general Foreman turn and uses its new scope", { timeout: 10_000 }, async () => {
  let registration: { projectId: string; systemId: string } | undefined;
  const graph = { nodes: [{ id: "operator" }, { id: "machine" }], edges: [{ source: "operator", target: "machine" }] };
  const app = createApp({
    dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false,
    runtime: { health: async () => ({ available: true, platform: "linux", executable: "fixture", machine: null, rootless: null }) },
    foreman: { spawn: async (_argv, options) => {
      const root = options.foremanMailbox!;
      const invalid = [
        "https://user:secret@code.example/team/private.git",
        "https://code.example/team/private.git?token=fixture-key",
        "file:///mnt/repo",
      ];
      for (const [index, repoUrl] of invalid.entries()) {
        const rejected = await askMailbox(root, index + 1, "project.register", { repoUrl });
        expect(rejected).toMatchObject({ ok: false, error: { code: "FOREMAN_OPERATION_INVALID" } });
      }
      const repository = { repoUrl: "https://code.example/team/sample.git" };
      const created = await askMailbox(root, 4, "project.register", repository);
      expect(created.ok).toBe(true);
      expect(await askMailbox(root, 4, "project.register", repository)).toEqual(created);
      registration = created.result as typeof registration;
      if (!registration) throw new Error("Expected repository registration");
      const { systemId, projectId } = registration;
      const units = await askMailbox(root, 5, "business.units", { systemId });
      expect(units.ok).toBe(true);
      expect(units.result).toHaveLength(2);
      expect((await askMailbox(root, 6, "business.graph", { systemId, graph })).ok).toBe(true);
      expect((await askMailbox(root, 7, "business.bind", { systemId, unitId: projectId + ":operator", harness: "codex" })).ok).toBe(true);
      expect((await askMailbox(root, 8, "business.run", { systemId })).ok).toBe(true);
      return fakeProcess([JSON.stringify({ type: "result", text: "已登记并组装业务系统" })]);
    } },
  });
  try {
    app.store.saveHarnessProfile({
      runtime: "claude-code", role: "foreman", baseUrl: "", model: "fixture", mode: "session",
      image: "registry.example/foreman@sha256:" + "a".repeat(64),
    });
    const talk = await app.fetch(new Request("http://localhost/api/v1/talk", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: "用这个仓库搭建业务系统" }),
    }));
    expect(talk.status).toBe(201);
    const [envelope] = app.store.listForemanInbox();
    await app.foreman.dispatch(envelope.id);
    expect(app.store.getForemanMessage(envelope.id)?.status).toBe("done");
    if (!registration) throw new Error("Missing repository registration");
    expect(app.store.listBusinessSystems()).toHaveLength(1);
    expect(app.store.getBusinessSystem(registration.systemId)?.metadata.graph).toEqual(graph);
    expect(app.store.getProject(registration.projectId)).toMatchObject({
      name: "sample", sources: [{ kind: "repo_url", repoUrl: "https://code.example/team/sample.git" }], secretRequests: [],
    });
    expect(app.store.listBusinessRuns(registration.systemId)).toHaveLength(1);
    expect(app.store.listForemanInbox()).toHaveLength(0);
    expect(app.events.after().filter(({ type }) => type === "foreman.message.queued")).toHaveLength(1);
  } finally { app.stop(); }
});

describe("Foreman official Harness dispatch", () => {
  test("times out a stalled Foreman turn and continues the queue", async () => {
    let launches = 0;
    let killed = 0;
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false, foreman: { spawn: () => {
      if (launches++ === 0) return { stdout: new ReadableStream(), kill: () => { killed++; } };
      return fakeProcess([JSON.stringify({ type: "result", text: "后续已处理" })]);
    } } });
    try {
      app.store.saveHarnessProfile({ runtime: "claude-code", role: "foreman", baseUrl: "", model: "fixture", mode: "session", sessionTimeoutSeconds: 1 });
      const first = app.store.queueForemanMessage("turn-slow", "等待响应");
      const second = app.store.queueForemanMessage("turn-next", "继续处理");
      await Promise.all([app.foreman.dispatch(first.id), app.foreman.dispatch(second.id)]);
      expect(app.store.getForemanMessage(first.id)?.status).toBe("failed");
      expect(app.store.getForemanMessage(second.id)?.status).toBe("done");
      expect(app.events.after().find(({ type }) => type === "foreman.session.failed")?.data).toMatchObject({ inboxId: first.id, reason: "SESSION_TIMEOUT" });
      expect(app.store.listTalkMessages().map(({ body }) => body)).toEqual(["后续已处理"]);
      expect(killed).toBe(1);
    } finally { app.stop(); }
  });

  test("terminates a child that finishes spawning after a Foreman timeout", async () => {
    let publishProcess!: (process: OfficialProcess) => void;
    let reportKill!: () => void;
    const creation = new Promise<OfficialProcess>((resolve) => { publishProcess = resolve; });
    const killed = new Promise<void>((resolve) => { reportKill = resolve; });
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false, foreman: { spawn: () => creation } });
    try {
      app.store.saveHarnessProfile({ runtime: "claude-code", role: "foreman", baseUrl: "", model: "fixture", mode: "session", sessionTimeoutSeconds: 1 });
      const envelope = app.store.queueForemanMessage("turn-late", "启动过慢");
      await app.foreman.dispatch(envelope.id);
      expect(app.store.getForemanMessage(envelope.id)?.status).toBe("failed");
      publishProcess({ stdout: new ReadableStream(), kill: reportKill });
      await killed;
      expect(app.store.listTalkMessages()).toHaveLength(0);
    } finally { app.stop(); }
  });

  test("claims queued intent, streams deltas and persists a reply", async () => {
    const dbPath = `.factory/foreman-${randomUUID()}.db`;
    let factoryEnv: Record<string, string> = {};
    const app = createApp({
      dbPath,
      legacyPath: dbPath,
      storageRoot: ".factory",
      foreman: { spawn: (_argv, options) => {
        factoryEnv = options.env;
        return fakeProcess([
          JSON.stringify({ type: "assistant", message: { content: [{ type: "text", text: "我会先建立厂区。" }] } }),
          JSON.stringify({ type: "result", text: "已开始" }),
        ]);
      } },
    });
    try {
      const profile = app.store.saveHarnessProfile({ runtime: "codex", role: "foreman", baseUrl: "https://example.invalid", model: "codex-test", mode: "session" });
      expect(profile.role).toBe("foreman");
      const response = await app.fetch(new Request("http://localhost/api/v1/talk", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: "建立一个视频业务系统" }) }));
      expect(response.status).toBe(201);
      for (let attempt = 0; attempt < 20 && app.store.listTalkMessages().filter((item) => item.role === "foreman").length === 0; attempt++) await new Promise((resolve) => setTimeout(resolve, 5));
      expect(app.store.listTalkMessages().some((item) => item.role === "foreman" && item.body.includes("我会先建立厂区。"))).toBe(true);
      expect(app.events.after().map((event) => event.type)).toContain("foreman.reply.delta");
      expect(factoryEnv).toMatchObject({ FACTORY_ROOT: resolve(".factory"), BUSINESS_DB_PATH: resolve(dbPath), FACTORY_DB_PATH: resolve(dbPath) });
      expect(app.store.listForemanInbox()).toHaveLength(0);
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });

  test("resumes the official session ID on the next native turn", async () => {
    const calls: string[][] = [];
    const sessionId = "a9919900-0000-4111-9111-100000000000";
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", foreman: { spawn: (argv) => {
      calls.push(argv);
      return fakeProcess([
        JSON.stringify({ type: "thread.started", thread_id: sessionId }),
        JSON.stringify({ type: "item.completed", item: { type: "agent_message", text: "进度已更新。" } }),
        JSON.stringify({ type: "turn.completed" }),
      ]);
    } } });
    try {
      const profile = app.store.saveHarnessProfile({ runtime: "codex", role: "foreman", baseUrl: "", model: "fixture", mode: "session" });
      const first = app.store.queueForemanMessage("turn-1", "创建项目");
      const second = app.store.queueForemanMessage("turn-2", "接着做");
      await Promise.all([app.foreman.dispatch(first.id), app.foreman.dispatch(second.id)]);
      expect(app.store.foremanSession(profile.id)).toBe(sessionId);
      expect(calls[0]).not.toContain("resume");
      expect(calls[1].slice(0, 3)).toEqual(["codex", "exec", "resume"]);
      expect(calls[1]).toContain(sessionId);
      expect(app.store.listTalkMessages().map(({ body }) => body)).toEqual(["进度已更新。", "进度已更新。"]);
    } finally {
      await app.foreman.close();
      app.stop();
    }
  });
});
