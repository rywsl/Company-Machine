import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, renameSync, symlinkSync } from "node:fs";
import { rm } from "node:fs/promises";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { createApp } from "../src/server";
import type { OfficialProcess } from "../src/harness";
import type { ProjectSourceMount } from "../src/project-source-mounts";
import { foremanCredential } from "../src/foreman-credentials";
import { FakeClock } from "../src/legacy/foundation";

function fakeProcess(lines: string[]): OfficialProcess {
  const body = new TextEncoder().encode(`${lines.join("\n")}\n`);
  return { stdout: new ReadableStream({ start(controller) { controller.enqueue(body); controller.close(); } }), kill() {} };
}

type MailboxReply = { id: string; ok: boolean; result: unknown; error: { code: string } | null };

async function cancelTurn(app: ReturnType<typeof createApp>, id: string): Promise<Response> {
  return app.fetch(new Request(`http://localhost/api/v1/foreman/inbox/${id}/cancel`, { method: "POST" }));
}

async function askMailbox(root: string, index: number, op: string, args: Record<string, unknown>): Promise<MailboxReply> {
  const id = "r-" + index.toString(16).padStart(32, "0");
  const request = join(root, "requests", id + ".json");
  await Bun.write(request + ".part", JSON.stringify({ id, op, args }));
  renameSync(request + ".part", request);
  const response = join(root, "responses", id + ".json");
  for (let attempt = 0; attempt < 100 && !existsSync(response); attempt++) await new Promise((done) => setTimeout(done, 10));
  return JSON.parse(await Bun.file(response).text());
}

test("keeps native Foreman sessions working beside the legacy foreman_sessions schema", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-foreman-schema-"));
  const dbPath = join(root, "factory.db");
  const legacy = new (await import("bun:sqlite")).Database(dbPath);
  legacy.exec("CREATE TABLE foreman_sessions (id TEXT PRIMARY KEY, wake_id TEXT NOT NULL, role TEXT NOT NULL, runtime TEXT NOT NULL, actor TEXT NOT NULL, model TEXT NOT NULL, epoch TEXT NOT NULL, state TEXT NOT NULL, exit_code INTEGER, cost_usd REAL NOT NULL DEFAULT 0, turns INTEGER, outcome TEXT NOT NULL DEFAULT '{}', error TEXT, started_at TEXT NOT NULL, ended_at TEXT)");
  legacy.close();
  const app = createApp({ dbPath, legacyPath: dbPath, dispatchWork: false, foreman: { spawn: async () => fakeProcess([JSON.stringify({ type: "result", text: "已连接" })]) } });
  try {
    app.store.saveHarnessProfile({ runtime: "codex", role: "foreman", baseUrl: "", model: "fixture", mode: "session" });
    const posted = await app.fetch(new Request("http://localhost/api/web/talk", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: "hello" }),
    }));
    const body = await posted.json() as { foremanTurn: { id: string } };
    await app.foreman.dispatch(body.foremanTurn.id);
    expect(app.store.getForemanMessage(body.foremanTurn.id)?.status).toBe("done");
    expect(app.store.listTalkMessages().at(-1)?.body).toBe("已连接");
  } finally {
    app.stop();
    await rm(root, { recursive: true, force: true });
  }
});

test("applies a container Foreman mailbox request within its intake scope", async () => {
  let root: string | undefined;
  let sourceOptions: { cloneRepositories?: boolean } | undefined;
  let sourcePrompt = "";
  const replies: MailboxReply[] = [];
  const app = createApp({
    dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false,
    runtime: { health: async () => ({ available: true, platform: "linux", executable: "fixture", machine: null, rootless: null }) },
    foreman: { prepareSources: async (_project, options) => { sourceOptions = options; return undefined; }, spawn: async (argv, options) => {
      root = options.foremanMailbox;
      if (!root) throw new Error("Expected scoped mailbox");
      expect(options.trustedForeman).toBe(true);
      expect(options.network).toBe("bridge");
      expect(options.foremanWorkspace).toBeTruthy();
      expect(options.foremanWorkspace && existsSync(options.foremanWorkspace)).toBe(true);
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
      await ask(10, "project.register", { repoUrl: "https://code.example/new-system.git", systemId: project.businessSystemId });
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
    expect(app.store.getForemanMessage(body.foreman_inbox_id)?.businessStatus).toBe("applied");
    expect(sourcePrompt).toContain("/mao/foreman/requests");
    expect(sourceOptions?.cloneRepositories).toBe(false);
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
    foreman: { prepareSources: async () => undefined, spawn: async (_argv, options) => {
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
      const credentialRequest = await askMailbox(root, 9, "project.secret.request", { projectId, name: "VIDEO_API_KEY" });
      expect(credentialRequest).toMatchObject({ ok: true, result: { status: "requested" } });
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
      name: "sample", sources: [{ kind: "repo_url", repoUrl: "https://code.example/team/sample.git" }],
      secretRequests: [{ name: "VIDEO_API_KEY", status: "requested" }],
    });
    expect(app.store.listBusinessRuns(registration.systemId)).toHaveLength(1);
    expect(app.store.listForemanInbox()).toHaveLength(0);
    expect(app.events.after().filter(({ type }) => type === "foreman.message.queued")).toHaveLength(1);
  } finally { app.stop(); }
});

test("adds a repo to an existing system in scope without opening another Foreman turn", { timeout: 10_000 }, async () => {
  let existingSystemId: string;
  let registeredProjectId: string | undefined;
  const app = createApp({
    dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false,
    runtime: { health: async () => ({ available: true, platform: "linux", executable: "fixture", machine: null, rootless: null }) },
    foreman: { spawn: async (_argv, options) => {
      const root = options.foremanMailbox!;
      const late = app.store.createBusinessSystem({ name: "Created after the turn scope" });
      const repoUrl = "https://code.example/team/second.git";
      expect(await askMailbox(root, 1, "project.register", { repoUrl, systemId: late.id })).toMatchObject({
        ok: false, error: { code: "FOREMAN_OPERATION_INVALID" },
      });
      const input = { repoUrl, systemId: existingSystemId };
      const response = await askMailbox(root, 2, "project.register", input);
      expect(response).toMatchObject({ ok: true, result: { systemId: existingSystemId } });
      expect(await askMailbox(root, 2, "project.register", input)).toEqual(response);
      registeredProjectId = (response.result as { projectId: string }).projectId;
      expect((await askMailbox(root, 3, "business.units", { systemId: existingSystemId })).result).toHaveLength(4);
      return fakeProcess([JSON.stringify({ type: "result", text: "已添加项目" })]);
    } },
  });
  try {
    existingSystemId = app.store.createBusinessSystem({ name: "Existing system" }).id;
    const original = app.store.createIntake({ name: "First", business_system_id: existingSystemId });
    app.store.saveHarnessProfile({
      runtime: "claude-code", role: "foreman", baseUrl: "", model: "fixture", mode: "session",
      image: "registry.example/foreman@sha256:" + "a".repeat(64),
    });
    const response = await app.fetch(new Request("http://localhost/api/v1/talk", {
      method: "POST", body: JSON.stringify({ text: "添加这个仓库到已有业务系统" }),
    }));
    expect(response.status).toBe(201);
    const [envelope] = app.store.listForemanInbox();
    await app.foreman.dispatch(envelope.id);

    expect(app.store.getForemanMessage(envelope.id)?.status).toBe("done");
    expect(app.store.listBusinessSystems()).toHaveLength(2);
    const projects = app.store.listProjects(existingSystemId);
    expect(projects.map(({ id }) => id).sort()).toEqual([original.id, registeredProjectId].sort());
    expect(projects.find(({ id }) => id === registeredProjectId)).toMatchObject({
      name: "second", sources: [{ kind: "repo_url", repoUrl: "https://code.example/team/second.git" }],
    });
    expect(app.events.after().filter(({ type }) => type === "business-system.created")).toHaveLength(0);
    expect(app.events.after().filter(({ type }) => type === "project.intake.created")).toHaveLength(1);
    expect(app.events.after().filter(({ type }) => type === "foreman.message.queued")).toHaveLength(1);
  } finally { app.stop(); }
});

test("guides a native Foreman to register repositories and read the assigned business graph", async () => {
  const prompts: string[] = [];
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false, foreman: {
    spawn: (argv) => {
      prompts.push(argv.at(-1)!);
      return fakeProcess([JSON.stringify({ type: "result", text: "已处理本轮" })]);
    },
  } });
  try {
    const assigned = app.store.createBusinessSystem({ name: "Assigned system" });
    const separate = app.store.createBusinessSystem({ name: "Separate system" });
    app.store.saveHarnessProfile({ runtime: "claude-code", role: "foreman", baseUrl: "", model: "fixture", mode: "session" });
    const general = app.store.queueForemanMessage("ordinary-turn", "登记仓库");
    await app.foreman.dispatch(general.id);
    expect(prompts[0]).toContain("project register --repo <URL> [--name <名称>] [--system <system-id>]");
    expect(prompts[0]).toContain("business graph <system-id>` 读取现有 DAG");
    expect(prompts[0]).toContain(separate.id);

    const response = await app.fetch(new Request("http://localhost/api/v1/projects/intake", {
      method: "POST",
      body: JSON.stringify({ business_system_id: assigned.id, repo_url: "https://example.test/assigned.git" }),
    }));
    expect(response.status).toBe(201);
    const { project_id: projectId, foreman_inbox_id: inboxId } = await response.json() as { project_id: string; foreman_inbox_id: string };
    await app.foreman.dispatch(inboxId);
    expect(prompts[1]).toContain(`本次项目 ID ${projectId}`);
    expect(prompts[1]).toContain(`本次业务系统 ID ${assigned.id}`);
    expect(prompts[1]).toContain("https://example.test/assigned.git");
    expect(prompts[1]).toContain("project secret request <project-id> --name <KEY> --purpose <用途>");
    expect(prompts[1]).not.toContain(separate.id);
    expect(prompts[1]).not.toContain("project register");
  } finally { app.stop(); }
});

test("requests one project credential through the intake Foreman mailbox without exposing any value", { timeout: 15_000 }, async () => {
  const deploymentValue = "FOREMAN_FIXTURE_DEPLOYMENT_KEY";
  const rejectedValue = "fixture-value-from-disallowed-mailbox-argument";
  const deploymentKey = "MAO_FOREMAN_ANTHROPIC_API_KEY";
  const deploymentEndpoint = "MAO_FOREMAN_ANTHROPIC_BASE_URL";
  const previousKey = process.env[deploymentKey];
  const previousEndpoint = process.env[deploymentEndpoint];
  let projectId: string;
  let otherProjectId: string;
  let request: MailboxReply | undefined;
  const app = createApp({
    dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false,
    runtime: { health: async () => ({ available: true, platform: "linux", executable: "fixture", machine: null, rootless: null }) },
    foreman: { prepareSources: async () => undefined, spawn: async (argv, options) => {
      expect(argv.join(" ")).toContain("本次项目 ID " + projectId);
      expect(options.env?.ANTHROPIC_API_KEY === deploymentValue).toBe(true);
      const root = options.foremanMailbox!;
      const valid = { projectId, name: "VIDEO_API_KEY", purpose: "生成短片 " + deploymentValue, scope: "制作预览" };
      const invalid = [
        { ...valid, projectId: otherProjectId },
        { ...valid, name: "PATH" },
        { ...valid, name: deploymentValue },
        { ...valid, purpose: { value: rejectedValue } },
        { ...valid, value: rejectedValue },
      ];
      for (const [index, args] of invalid.entries()) {
        const reply = await askMailbox(root, index + 1, "project.secret.request", args);
        expect(reply).toMatchObject({ ok: false, result: null, error: { code: "FOREMAN_OPERATION_INVALID" } });
      }
      request = await askMailbox(root, 6, "project.secret.request", valid);
      const duplicate = await askMailbox(root, 7, "project.secret.request", { ...valid, purpose: "修改后的用途" });
      expect(duplicate.ok).toBe(true);
      expect(duplicate.result).toEqual(request.result);
      return fakeProcess([JSON.stringify({ type: "result", text: "项目已登记短片制作需求" })]);
    } },
  });
  try {
    process.env[deploymentKey] = deploymentValue;
    process.env[deploymentEndpoint] = "https://api.anthropic.com";
    app.store.saveHarnessProfile({
      runtime: "claude-code", role: "foreman", baseUrl: "", model: "fixture", mode: "session",
      image: "registry.example/foreman@sha256:" + "a".repeat(64),
    });
    const intake = await app.fetch(new Request("http://localhost/api/v1/projects/intake", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: "测试项目", repo_url: "https://example.invalid/project" }),
    }));
    expect(intake.status).toBe(201);
    const { project_id, foreman_inbox_id } = await intake.json();
    projectId = project_id;
    otherProjectId = app.store.createIntake({
      name: "同业务内的其他项目", repo_url: "https://example.invalid/other",
      business_system_id: app.store.getProject(projectId)!.businessSystemId,
    }).id;
    await app.foreman.dispatch(foreman_inbox_id);
    expect(app.store.getForemanMessage(foreman_inbox_id)?.status).toBe("done");
    const requests = app.store.listSecretRequests();
    expect(requests).toHaveLength(1);
    const [stored] = requests;
    expect(stored).toMatchObject({
      projectId, name: "VIDEO_API_KEY", status: "requested",
      metadata: { purpose: "生成短片 [REDACTED]", scope: "制作预览" },
    });
    expect(request).toMatchObject({ ok: true });
    expect(request?.result).toEqual({ id: stored.id, status: "requested" });
    const events = app.events.after().filter(({ type }) => type === "secret.requested");
    expect(events.length).toBeGreaterThan(0);
    expect(events[0].data).toMatchObject({ id: stored.id, projectId, name: "VIDEO_API_KEY", status: "requested" });
    const visible = await app.fetch(new Request("http://localhost/api/web/secrets"));
    const secretViews = await visible.json();
    expect(secretViews).toMatchObject([{ id: stored.id, configured: false, request: { project_id: projectId, project_name: "测试项目" } }]);
    for (const value of [deploymentValue, rejectedValue, stored.vaultRef]) {
      expect(JSON.stringify([request, events, secretViews]).includes(value)).toBe(false);
    }
    expect(JSON.stringify(stored.metadata).includes(deploymentValue)).toBe(false);
  } finally {
    if (previousKey === undefined) delete process.env[deploymentKey]; else process.env[deploymentKey] = previousKey;
    if (previousEndpoint === undefined) delete process.env[deploymentEndpoint]; else process.env[deploymentEndpoint] = previousEndpoint;
    app.stop();
  }
});

test("gives the intake Foreman only registered upload bytes and the expressly selected folder", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-intake-source-"));
  const projectFolder = join(root, "project folder");
  const storageRoot = join(root, "factory");
  mkdirSync(projectFolder);
  await Bun.write(join(projectFolder, "readme.txt"), "selected folder");
  const bytes = Uint8Array.from([0, 1, 13, 255]);
  const mountsByTurn: ProjectSourceMount[][] = [];
  const app = createApp({
    dbPath: ":memory:", legacyPath: ":memory:", storageRoot, dispatchWork: false,
    runtime: { health: async () => ({ available: true, platform: "linux", executable: "fixture", machine: null, rootless: null }) },
    foreman: { spawn: async (argv, options) => {
      const mounts = options.projectSources ?? [];
      mountsByTurn.push(mounts);
      if (mounts.length) {
        const prompt = argv.join(" ");
        expect(prompt).toContain("source name.bin");
        expect(prompt).toContain("/mao/sources/");
        expect(prompt).not.toContain(root);
        const folder = mounts.find(({ source }) => source === projectFolder)!;
        const upload = mounts.find(({ source }) => source !== projectFolder)!;
        expect(await Bun.file(join(folder.source, "readme.txt")).text()).toBe("selected folder");
        expect(new Uint8Array(await Bun.file(upload.source).arrayBuffer())).toEqual(bytes);
      }
      return fakeProcess([JSON.stringify({ type: "result", text: "已检查来源" })]);
    } },
  });
  try {
    app.store.saveHarnessProfile({
      runtime: "claude-code", role: "foreman", baseUrl: "", model: "fixture", mode: "session",
      image: "registry.example/foreman@sha256:" + "a".repeat(64),
    });
    const form = new FormData();
    form.set("name", "项目");
    form.set("local_folder", projectFolder);
    form.set("files", new File([bytes], "source name.bin"));
    const intakeUrl = "http://localhost/api/v1/projects/intake";
    const intake = await app.fetch(new Request(intakeUrl, { method: "POST", body: form }));
    const received = await intake.json();
    expect(intake.status).toBe(201);
    await app.foreman.dispatch(received.foreman_inbox_id);
    expect(app.store.getForemanMessage(received.foreman_inbox_id)?.status).toBe("done");
    expect(mountsByTurn[0].map(({ index }) => index)).toEqual([1, 2]);

    const symlink = join(root, "alias");
    symlinkSync(projectFolder, symlink, "junction");
    for (const folder of [symlink, storageRoot, root, join(projectFolder, "unsafe,syntax")]) {
      const response = await app.fetch(new Request(intakeUrl, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name: "拒绝不安全挂载", local_folder: folder }),
      }));
      const row = await response.json();
      await app.foreman.dispatch(row.foreman_inbox_id);
      expect(app.store.getForemanMessage(row.foreman_inbox_id)?.status).toBe("done");
      expect(mountsByTurn.at(-1)).toEqual([]);
    }
    const unregistered = "b".repeat(64);
    const blobFolder = join(storageRoot, "cas", "sha256", unregistered.slice(0, 2));
    mkdirSync(blobFolder, { recursive: true });
    await Bun.write(join(blobFolder, unregistered.slice(2)), "unregistered");
    const unknown = await app.fetch(new Request(intakeUrl, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: "未登记来源", uploaded_file_refs: ["cas://sha256:" + unregistered] }),
    }));
    const unregisteredIntake = await unknown.json();
    await app.foreman.dispatch(unregisteredIntake.foreman_inbox_id);
    expect(mountsByTurn.at(-1)).toEqual([]);
  } finally {
    app.stop();
    await rm(root, { recursive: true, force: true });
  }
});

describe("Foreman official Harness dispatch", () => {
  test("cancels an unclaimed turn during runtime readiness and preserves the state on reload", async () => {
    let finishReadiness!: () => void;
    let reportCheck!: () => void;
    const checked = new Promise<void>((resolve) => { reportCheck = resolve; });
    const waiting = new Promise<void>((resolve) => { finishReadiness = resolve; });
    const healthy = { available: true as const, platform: "linux" as const, executable: "fixture", machine: null, rootless: null };
    let launches = 0;
    const app = createApp({
      dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false,
      runtime: { health: async () => healthy, ensureReady: async () => { reportCheck(); await waiting; return healthy; } },
      foreman: { spawn: () => { launches++; return fakeProcess([]); } },
    });
    try {
      app.store.saveHarnessProfile({ runtime: "claude-code", role: "foreman", baseUrl: "", model: "fixture", mode: "session",
        image: "registry.example/foreman@sha256:" + "a".repeat(64) });
      const posted = await app.fetch(new Request("http://localhost/api/web/talk", {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: "wait during runtime startup" }),
      }));
      expect(posted.status).toBe(201);
      const { message, foremanTurn } = await posted.json();
      expect(foremanTurn).toMatchObject({ messageId: message.id, status: "queued", cancellable: true });
      const work = app.foreman.dispatch(foremanTurn.id);
      await checked;
      const response = await cancelTurn(app, foremanTurn.id);
      expect(response.status).toBe(200);
      const { foremanTurn: cancelled } = await response.json();
      expect(cancelled).toMatchObject({ id: foremanTurn.id, messageId: foremanTurn.messageId, status: "cancelled", cancellable: false, businessStatus: "failed", businessReason: "cancelled" });
      expect(typeof cancelled.completedAt).toBe("string");
      expect(typeof cancelled.lastEventAt).toBe("string");
      expect(typeof cancelled.elapsedMs).toBe("number");
      const retried = await cancelTurn(app, foremanTurn.id);
      expect(retried.status).toBe(200);
      expect(await retried.json()).toEqual({ foremanTurn: cancelled });
      finishReadiness();
      await work;
      expect(launches).toBe(0);
      const bootstrap = await (await app.fetch(new Request("http://localhost/api/web/bootstrap"))).json();
      expect(bootstrap.foremanTurns).toContainEqual(cancelled);
      expect(bootstrap.talk).toContainEqual(message);
      expect(app.events.after().filter(({ type }) => type === "foreman.message.cancelled").map(({ data }) => data)).toEqual([cancelled]);
      const done = await app.fetch(new Request(`http://localhost/api/v1/foreman/inbox/${foremanTurn.id}/done`, { method: "POST" }));
      expect(done.status).toBe(409);
      expect(app.events.after().some(({ type }) => type === "foreman.message.done")).toBe(false);
    } finally { finishReadiness(); await app.foreman.close(); app.stop(); }
  });

  test("interrupts a locally owned streaming turn while preserving earlier committed replies", async () => {
    const calls: string[][] = [];
    let killed = 0;
    let reportStreaming!: (id: string) => void;
    const streaming = new Promise<string>((resolve) => { reportStreaming = resolve; });
    const app = createApp({
      dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false,
      foreman: { spawn: (argv) => {
        calls.push(argv);
        if (calls.length === 2) return {
          stdout: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(
            JSON.stringify({ type: "item.completed", item: { type: "agent_message", text: "未完成的回复" } }) + "\n",
          )); } }),
          kill: () => { killed++; },
        };
        return fakeProcess([
          JSON.stringify({ type: "thread.started", thread_id: calls.length === 1 ? "first-session" : "new-session" }),
          JSON.stringify({ type: "item.completed", item: { type: "agent_message", text: calls.length === 1 ? "保留第一条" : "后续完成" } }),
          JSON.stringify({ type: "turn.completed" }),
        ]);
      } },
    });
    const post = async (text: string) => (await (await app.fetch(new Request("http://localhost/api/v1/talk", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text }),
    }))).json()).foremanTurn;
    const stopListening = app.events.subscribe(({ type, data }) => {
      if (type === "foreman.reply.delta" && (data as { text: string }).text === "未完成的回复") reportStreaming((data as { inboxId: string }).inboxId);
    });
    try {
      const profile = app.store.saveHarnessProfile({ runtime: "codex", role: "foreman", baseUrl: "", model: "fixture", mode: "session" });
      const first = await post("第一条已完成");
      await app.foreman.dispatch(first.id);
      const noCancel = await cancelTurn(app, first.id);
      expect(noCancel.status).toBe(409);
      expect(await noCancel.json()).toMatchObject({ foremanTurn: { id: first.id, status: "done", cancellable: false } });
      expect(app.store.foremanSession(profile.id)).toBe("first-session");

      const cancelled = await post("取消这一条");
      const work = app.foreman.dispatch(cancelled.id);
      expect(await streaming).toBe(cancelled.id);
      expect(app.foreman.pendingReplies()[cancelled.id]).toEqual("未完成的回复");
      const response = await cancelTurn(app, cancelled.id);
      expect(response.status).toBe(200);
      expect(app.foreman.pendingReplies()[cancelled.id]).toBeUndefined();
      await work;
      expect(killed).toBe(1);
      expect(app.store.foremanSession(profile.id)).toBeUndefined();
      expect(app.store.getForemanMessage(cancelled.id)?.status).toBe("cancelled");
      expect(app.events.after().some(({ type, data }) => type === "foreman.session.failed" && (data as { inboxId: string }).inboxId === cancelled.id)).toBe(false);
      expect(app.store.listTalkMessages().filter(({ role }) => role === "foreman").map(({ body }) => body)).toEqual(["保留第一条"]);

      const next = await post("继续下一条");
      await app.foreman.dispatch(next.id);
      expect(calls[1]).toContain("first-session");
      expect(calls[2]).not.toContain("resume");
      expect(calls[2].at(-1)).toContain("保留第一条");
      expect(calls[2].at(-1)).not.toContain("取消这一条");
      expect(app.store.foremanSession(profile.id)).toBe("new-session");
      expect(app.store.getForemanMessage(next.id)?.status).toBe("done");
    } finally { stopListening(); await app.foreman.close(); app.stop(); }
  });

  test("reports a conflict for another process's claim and leaves its turn available to finish", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-foreman-owner-"));
    const path = join(root, "business.db");
    const app = createApp({ dbPath: path, legacyPath: path, storageRoot: root, dispatchWork: false });
    const other = createApp({ dbPath: path, legacyPath: path, storageRoot: root, dispatchWork: false });
    try {
      const envelope = app.store.queueForemanMessage("cross-process", "别的进程处理");
      expect(other.store.claimForemanMessage(envelope.id)?.status).toBe("claimed");
      const response = await cancelTurn(app, envelope.id);
      expect(response.status).toBe(409);
      expect(await response.json()).toMatchObject({ foremanTurn: { id: envelope.id, status: "claimed", cancellable: false } });
      expect(await cancelTurn(app, "unknown").then(({ status }) => status)).toBe(404);
      expect(other.store.getForemanMessage(envelope.id)?.status).toBe("claimed");
      expect(other.store.finishForemanTurn(envelope.id, { reply: { body: "仍然可完成", metadata: {} } })).toHaveLength(2);
      expect(app.store.getForemanMessage(envelope.id)?.status).toBe("done");
      expect(app.events.after().some(({ type }) => type === "foreman.message.cancelled")).toBe(false);
    } finally { other.stop(); app.stop(); await rm(root, { recursive: true, force: true, maxRetries: 4, retryDelay: 75 }); }
  });

  test("provides causal and bounded Foreman history including project intake turns", async () => {
    const prompts: string[] = [];
    let nextId = 1_000;
    const app = createApp({
      dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false,
      clock: new FakeClock(), uuid: () => `fixture-${--nextId}`,
      runtime: { health: async () => ({ available: true, platform: "linux", executable: "fixture", machine: null, rootless: null }) },
      foreman: { spawn: (argv) => {
        prompts.push(argv.at(-1)!);
        return fakeProcess([JSON.stringify({ type: "result", text: "收到" })]);
      } },
    });
    const queue = (body: string, metadata: Record<string, unknown> = {}) => {
      const message = app.store.addTalkMessage({ body, metadata });
      return app.store.queueForemanMessage(message.id, message.body);
    };
    const finish = (body: string, reply: string, metadata: Record<string, unknown> = {}) => {
      const envelope = queue(body, metadata);
      app.store.addTalkMessage({ role: "foreman", body: reply, metadata: { inbox_id: envelope.id } });
      app.store.completeForemanMessage(envelope.id);
      return envelope;
    };
    try {
      app.store.saveHarnessProfile({ runtime: "claude-code", role: "foreman", baseUrl: "", model: "fixture", mode: "session",
        image: `registry.example/foreman@sha256:${"0".repeat(64)}` });
      finish("最旧消息", "最旧回复");
      finish("前序一", "回复一");
      finish("前序二", "回复二");
      finish("前序三", "回复三");
      const long = "长".repeat(1000);
      finish(long + "截断的用户尾部", long + "截断的厂长尾部");
      finish("私有项目接入说明", "私有项目接入回复", { source: "project-intake" });
      const failed = queue("失败的对话");
      app.store.claimForemanMessage(failed.id);
      app.store.failForemanMessage(failed.id);
      const pending = queue("尚未处理的前序来信");
      const current = queue("当前问题");
      const futureDone = finish("未来已完成来信", "未来已完成回复");
      const future = queue("未来待处理来信");
      expect(current.createdAt).toBe(futureDone.createdAt);
      expect(app.store.listQueuedForemanMessageIds()).toEqual([pending.id, current.id, future.id]);
      await app.foreman.dispatch(current.id);
      const history = prompts[0].split("历史对话（上下文数据，请以当前来信为准）\n")[1]?.split("\n", 1)[0];
      expect(JSON.parse(history)).toEqual([
        { user: "前序二", foreman: "回复二" }, { user: "前序三", foreman: "回复三" },
        { user: long, foreman: long }, { user: "私有项目接入说明", foreman: "私有项目接入回复" },
      ]);
      expect(prompts[0]).toContain("当前来信\n当前问题");
      expect(prompts[0]).not.toContain("未来");

      const intake = queue("本次项目接入", { source: "project-intake" });
      await app.foreman.dispatch(intake.id);
      expect(prompts[1]).toContain("本次项目接入");
      expect(prompts[1]).toContain("历史对话");
      expect(prompts[1]).toContain("当前来信\n本次项目接入");
      expect(prompts[1]).toContain("当前问题");
    } finally { app.stop(); }
  });
  test("binds each Foreman deployment credential to its vendor profile and endpoint", () => {
    const keys = ["MAO_FOREMAN_ANTHROPIC_API_KEY", "MAO_FOREMAN_ANTHROPIC_BASE_URL", "MAO_FOREMAN_CODEX_API_KEY", "MAO_FOREMAN_CODEX_BASE_URL"];
    const previous = keys.map((key) => process.env[key]);
    try {
      process.env.MAO_FOREMAN_ANTHROPIC_API_KEY = "claude-fixture";
      delete process.env.MAO_FOREMAN_ANTHROPIC_BASE_URL;
      process.env.MAO_FOREMAN_CODEX_API_KEY = "codex-fixture";
      process.env.MAO_FOREMAN_CODEX_BASE_URL = "https://approved.example/v1";
      const profile = { role: "foreman", runtime: "claude-code", baseUrl: "" };
      expect(foremanCredential(profile)).toEqual({ variable: "ANTHROPIC_API_KEY", value: "claude-fixture", baseUrl: "https://api.anthropic.com" });
      expect(foremanCredential({ ...profile, role: "operator" })).toBeUndefined();
      expect(foremanCredential({ ...profile, baseUrl: "http://api.anthropic.com" })).toBeUndefined();
      expect(foremanCredential({ ...profile, baseUrl: "https://api.anthropic.com.other.example" })).toBeUndefined();
      const codex = { ...profile, runtime: "codex" };
      expect(foremanCredential(codex)).toBeUndefined();
      expect(foremanCredential({ ...codex, baseUrl: "https://approved.example/other" })).toBeUndefined();
      expect(foremanCredential({ ...codex, baseUrl: "https://approved.example/v1/" })).toEqual({ variable: "CODEX_API_KEY", value: "codex-fixture", baseUrl: "https://approved.example/v1/" });
      expect(foremanCredential({ ...codex, baseUrl: "https://approved.example/v1?api_key=secret" })).toBeUndefined();
    } finally {
      keys.forEach((key, index) => {
        if (previous[index] === undefined) delete process.env[key];
        else process.env[key] = previous[index];
      });
    }
  });

  test("forwards one configured Foreman credential and redacts it across reply deltas", async () => {
    const key = "MAO_FOREMAN_ANTHROPIC_API_KEY";
    const endpoint = "MAO_FOREMAN_ANTHROPIC_BASE_URL";
    const previous = [process.env[key], process.env[endpoint]];
    const secret = "key-fixture-split-by-official-harness";
    process.env[key] = secret;
    delete process.env[endpoint];
    let received: Record<string, string> = {};
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false, foreman: { spawn: (_argv, options) => {
      received = options.env;
      return fakeProcess([
        JSON.stringify({ type: "system", session_id: secret }),
        JSON.stringify({ type: "assistant", text: "结果 " + secret.slice(0, 7) }),
        JSON.stringify({ type: "assistant", text: secret.slice(7) + " 已处理" }),
        JSON.stringify({ type: "result", text: "完成" }),
      ]);
    } } });
    try {
      app.store.saveHarnessProfile({ runtime: "claude-code", role: "foreman", baseUrl: "", model: "fixture", mode: "session", secretRef: "vault://historical-ref" });
      app.vault.put("vault://historical-ref", secret);
      const envelope = app.store.queueForemanMessage("turn-redact", "处理项目");
      await app.foreman.dispatch(envelope.id);
      expect(received.ANTHROPIC_API_KEY === secret).toBe(true);
      expect(received[key]).toBeUndefined();
      expect(app.store.listTalkMessages().map(({ body }) => body)).toEqual(["结果 [REDACTED] 已处理"]);
      expect(JSON.stringify(app.events.after()).includes(secret)).toBe(false);
      const response = await app.fetch(new Request("http://localhost/api/web/harnesses"));
      const payload = await response.text();
      expect(payload).toContain('"api_key_configured":true');
      expect(payload.includes(secret)).toBe(false);
      expect(payload).not.toContain("vault://historical-ref");
    } finally {
      app.stop();
      [key, endpoint].forEach((name, index) => {
        if (previous[index] === undefined) delete process.env[name];
        else process.env[name] = previous[index];
      });
    }
  });

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
    const committedReplies: Array<string | undefined> = [];
    app.events.subscribe(({ seq, type }) => {
      if (type === "foreman.session.text.delta") {
        const [reply] = app.events.after(seq, 1);
        committedReplies.push(reply?.type === "foreman.reply.delta" ? (reply.data as { text?: string }).text : undefined);
      }
    });
    try {
      const profile = app.store.saveHarnessProfile({ runtime: "codex", role: "foreman", baseUrl: "https://example.invalid", model: "codex-test", mode: "session" });
      expect(profile.role).toBe("foreman");
      const response = await app.fetch(new Request("http://localhost/api/v1/talk", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: "建立一个视频业务系统" }) }));
      expect(response.status).toBe(201);
      for (let attempt = 0; attempt < 20 && app.store.listTalkMessages().filter((item) => item.role === "foreman").length === 0; attempt++) await new Promise((resolve) => setTimeout(resolve, 5));
      expect(app.store.listTalkMessages().some((item) => item.role === "foreman" && item.body.includes("我会先建立厂区。"))).toBe(true);
      expect(committedReplies).toEqual(["我会先建立厂区。"]);
      expect(factoryEnv).toMatchObject({ FACTORY_ROOT: resolve(".factory"), BUSINESS_DB_PATH: resolve(dbPath), FACTORY_DB_PATH: resolve(dbPath) });
      expect(app.store.listForemanInbox()).toHaveLength(0);
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });

  test("settles a CLI turn after the final streamed reply instead of waiting for process EOF", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false, foreman: {
      spawn: () => fakeProcess([
        JSON.stringify({ type: "turn.completed" }),
        JSON.stringify({ type: "assistant", message: { content: [{ type: "text", text: "已收到。" }] } }),
        JSON.stringify({ type: "turn.completed" }),
      ]),
    } });
    try {
      app.store.saveHarnessProfile({ runtime: "codex", role: "foreman", baseUrl: "", model: "fixture", mode: "session" });
      const queued = app.store.queueForemanMessage("early-terminal", "hello");
      await app.foreman.dispatch(queued.id);
      expect(app.store.getForemanMessage(queued.id)?.status).toBe("done");
      expect(app.store.listTalkMessages().at(-1)?.body).toBe("已收到。");
    } finally {
      await app.foreman.close();
      app.stop();
    }
  });

  test("waits past Codex reasoning items for the final agent message", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false, foreman: {
      spawn: () => fakeProcess([
        JSON.stringify({ type: "thread.started", thread_id: "reasoning-session" }),
        JSON.stringify({ type: "item.completed", item: { type: "reasoning", text: "Planning repository inspection" } }),
        JSON.stringify({ type: "item.completed", item: { type: "agent_message", text: "最终回复" } }),
        JSON.stringify({ type: "turn.completed" }),
      ]),
    } });
    try {
      app.store.saveHarnessProfile({ runtime: "codex", role: "foreman", baseUrl: "", model: "fixture", mode: "session" });
      const queued = app.store.queueForemanMessage("reasoning-turn", "继续处理");
      await app.foreman.dispatch(queued.id);
      expect(app.store.listTalkMessages().at(-1)?.body).toBe("最终回复");
    } finally {
      await app.foreman.close();
      app.stop();
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
      expect(calls[1].at(-1)).not.toContain("历史对话");
      expect(app.store.listTalkMessages().map(({ body }) => body)).toEqual(["进度已更新。", "进度已更新。"]);
    } finally {
      await app.foreman.close();
      app.stop();
    }
  });

  test("does not persist a synthetic CLI session ID and replays the prior turn", async () => {
    const calls: string[][] = [];
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false, foreman: { spawn: (argv) => {
      calls.push(argv);
      return fakeProcess([JSON.stringify({ type: "result", text: calls.length === 1 ? "第一轮完成" : "第二轮完成" })]);
    } } });
    try {
      const profile = app.store.saveHarnessProfile({ runtime: "codex", role: "foreman", baseUrl: "", model: "fixture", mode: "session" });
      const first = app.store.addTalkMessage({ body: "先处理这个项目" });
      const firstTurn = app.store.queueForemanMessage(first.id, first.body);
      await app.foreman.dispatch(firstTurn.id);
      expect(app.store.foremanSession(profile.id)).toBeUndefined();
      expect(app.store.listTalkMessages().at(-1)?.metadata.turn_id).toBe(firstTurn.id);

      const second = app.store.addTalkMessage({ body: "继续刚才的工作" });
      const secondTurn = app.store.queueForemanMessage(second.id, second.body);
      await app.foreman.dispatch(secondTurn.id);
      expect(calls[1]).not.toContain("resume");
      expect(calls[1].at(-1)).toContain("第一轮完成");
      expect(app.store.getForemanMessage(secondTurn.id)?.status).toBe("done");
    } finally {
      await app.foreman.close();
      app.stop();
    }
  });

  test("falls back to bounded history when an official resume cursor is rejected", async () => {
    const calls: string[][] = [];
    const sessionId = "resume-session-1";
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false, foreman: { spawn: (argv) => {
      calls.push(argv);
      if (calls.length === 2) return fakeProcess([JSON.stringify({ type: "error", message: "session not found" })]);
      return fakeProcess([
        JSON.stringify({ type: "thread.started", thread_id: calls.length === 1 ? sessionId : "fresh-session" }),
        JSON.stringify({ type: "item.completed", item: { type: "agent_message", text: calls.length === 1 ? "首轮完成" : "回退后完成" } }),
        JSON.stringify({ type: "turn.completed" }),
      ]);
    } } });
    try {
      const profile = app.store.saveHarnessProfile({ runtime: "codex", role: "foreman", baseUrl: "", model: "fixture", mode: "session" });
      const queue = (body: string) => {
        const message = app.store.addTalkMessage({ body });
        return app.store.queueForemanMessage(message.id, body);
      };
      const first = queue("建立初始上下文");
      await app.foreman.dispatch(first.id);
      expect(app.store.foremanSession(profile.id)).toBe(sessionId);

      const second = queue("继续初始上下文");
      await app.foreman.dispatch(second.id);
      expect(calls).toHaveLength(3);
      expect(calls[1].slice(0, 3)).toEqual(["codex", "exec", "resume"]);
      expect(calls[1]).toContain(sessionId);
      expect(calls[2]).not.toContain("resume");
      expect(calls[2].at(-1)).toContain("建立初始上下文");
      expect(app.store.foremanSession(profile.id)).toBe("fresh-session");
      expect(app.store.getForemanMessage(second.id)?.status).toBe("done");
    } finally {
      await app.foreman.close();
      app.stop();
    }
  });

  test("persists Foreman turn timing and causal inbox metadata", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false, foreman: {
      spawn: () => fakeProcess([JSON.stringify({ type: "result", text: "已完成" })]),
    } });
    try {
      app.store.saveHarnessProfile({ runtime: "codex", role: "foreman", baseUrl: "", model: "fixture", mode: "session" });
      const posted = await app.fetch(new Request("http://localhost/api/web/talk", {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: "计时并关联这一轮" }),
      }));
      const body = await posted.json() as { message: { id: string; metadata: Record<string, unknown> }; foremanTurn: { id: string } };
      expect(body.message.metadata.inbox_id).toBe(body.foremanTurn.id);
      await app.foreman.dispatch(body.foremanTurn.id);
      const turn = app.store.getForemanMessage(body.foremanTurn.id);
      expect(turn?.status).toBe("done");
      expect(typeof turn?.startedAt).toBe("string");
      expect(typeof turn?.lastEventAt).toBe("string");
      expect(typeof turn?.completedAt).toBe("string");
      expect(typeof turn?.elapsedMs).toBe("number");
      expect(app.store.listTalkMessages().at(-1)?.metadata.inbox_id).toBe(body.foremanTurn.id);
    } finally {
      await app.foreman.close();
      app.stop();
    }
  });

  test("rolls back a Foreman reply and native session if persisting completion fails", async () => {
    const sessionId = "a9919900-0000-4111-9111-200000000000";
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false, foreman: {
      spawn: () => fakeProcess([
        JSON.stringify({ type: "thread.started", thread_id: sessionId }),
        JSON.stringify({ type: "item.completed", item: { type: "agent_message", text: "Fixture reply" } }),
        JSON.stringify({ type: "turn.completed" }),
      ]),
    } });
    const terminalEvents: string[] = [];
    app.events.subscribe(({ type }) => {
      if (type === "talk.foreman.replied" || type === "foreman.message.done") terminalEvents.push(type);
    });
    try {
      const profile = app.store.saveHarnessProfile({ runtime: "codex", role: "foreman", baseUrl: "", model: "fixture", mode: "session" });
      app.store.saveForemanSession(profile.id, "previous-session");
      const first = app.store.queueForemanMessage("turn-first", "Persist atomic completion");
      app.store.db.exec(`
        CREATE TRIGGER reject_done_event BEFORE INSERT ON business_events WHEN NEW.type='foreman.message.done'
        BEGIN SELECT RAISE(ABORT, 'cannot persist completion event'); END;
      `);
      await app.foreman.dispatch(first.id);
      expect(app.store.getForemanMessage(first.id)?.status).toBe("failed");
      expect(app.store.foremanSession(profile.id)).toBe("previous-session");
      expect(app.store.listTalkMessages()).toEqual([]);
      expect(app.events.after().map(({ type }) => type)).not.toContain("talk.foreman.replied");
      expect(terminalEvents).toEqual([]);

      app.store.db.exec("DROP TRIGGER reject_done_event");
      const second = app.store.queueForemanMessage("turn-second", "Persist a new turn");
      await app.foreman.dispatch(second.id);
      expect(app.store.getForemanMessage(second.id)?.status).toBe("done");
      expect(app.store.foremanSession(profile.id)).toBe(sessionId);
      expect(app.store.listTalkMessages()).toMatchObject([{ role: "foreman", body: "Fixture reply", metadata: { inbox_id: second.id } }]);
      expect(terminalEvents).toEqual(["talk.foreman.replied", "foreman.message.done"]);
    } finally {
      await app.foreman.close();
      app.stop();
    }
  });

  test("requeues stale Foreman claims during durable work recovery", () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:" });
    try {
      const envelope = app.store.queueForemanMessage("stale-turn", "恢复这轮工作");
      expect(app.store.claimForemanMessage(envelope.id)?.status).toBe("claimed");
      app.store.db.query("UPDATE foreman_outbox SET claimed_at=? WHERE id=?").run("2000-01-01T00:00:00.000Z", envelope.id);
      app.recoverQueuedWork();
      expect(app.store.getForemanMessage(envelope.id)?.status).toBe("queued");
      expect(app.store.eventsAfter().map(({ type }) => type)).toContain("foreman.message.requeued");
    } finally {
      app.stop();
    }
  });

  test("dispatches queued Foreman work immediately after configuring a profile", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", foreman: {
      spawn: () => fakeProcess([
        JSON.stringify({ type: "result", text: "已接管项目。" }),
      ]),
    } });
    try {
      const queued = app.store.queueForemanMessage("before-profile", "接管项目");
      const response = await app.fetch(new Request("http://localhost/api/web/harnesses/configure", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ runtime: "claude-code", role: "foreman", model: "fixture", execution_mode: "native_host" }),
      }));
      expect(response.status).toBe(200);
      for (let attempt = 0; attempt < 40 && app.store.getForemanMessage(queued.id)?.status !== "done"; attempt++) {
        await new Promise((resolve) => setTimeout(resolve, 5));
      }
      expect(app.store.getForemanMessage(queued.id)?.status).toBe("done");
      expect(app.store.listTalkMessages()).toMatchObject([{ role: "foreman", body: "已接管项目。" }]);
    } finally {
      await app.foreman.close();
      app.stop();
    }
  });

  test("keeps thinking effort and context independent for Foreman and Operator profiles", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
    const configure = async (role: "foreman" | "operator", thinking_effort: string | undefined, context: number) => {
      const response = await app.fetch(new Request("http://localhost/api/web/harnesses/configure", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ runtime: role === "foreman" ? "claude-code" : "codex", role, model: role + "-fixture", thinking_effort, context, execution_mode: role === "foreman" ? "native_host" : undefined }),
      }));
      expect(response.status).toBe(200);
    };
    try {
      await configure("operator", "low", 64_000);
      await configure("foreman", "max", 256_000);
      let profiles = app.store.listHarnessProfiles();
      expect(profiles.some(({ role, thinkingEffort, contextTokens }) => role === "operator" && thinkingEffort === "low" && contextTokens === 64_000)).toBe(true);
      expect(profiles.some(({ role, thinkingEffort, contextTokens }) => role === "foreman" && thinkingEffort === "max" && contextTokens === 256_000)).toBe(true);

      await configure("operator", "high", 32_000);
      profiles = app.store.listHarnessProfiles();
      expect(profiles.some(({ role, thinkingEffort, contextTokens }) => role === "operator" && thinkingEffort === "high" && contextTokens === 32_000)).toBe(true);
      expect(profiles.some(({ role, thinkingEffort, contextTokens }) => role === "foreman" && thinkingEffort === "max" && contextTokens === 256_000)).toBe(true);
    } finally {
      app.stop();
    }
  });

  test("defaults a newly configured Foreman thinking effort to max", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
    try {
      const response = await app.fetch(new Request("http://localhost/api/web/harnesses/configure", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ runtime: "claude-code", role: "foreman", model: "fixture", execution_mode: "native_host" }),
      }));
      expect(response.status).toBe(200);
      expect(app.store.listHarnessProfiles().find(({ role }) => role === "foreman")).toMatchObject({ thinkingEffort: "max", contextTokens: 500_000 });
      const operator = await app.fetch(new Request("http://localhost/api/web/harnesses/configure", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ runtime: "codex", role: "operator", model: "fixture" }),
      }));
      expect(operator.status).toBe(200);
      expect(app.store.listHarnessProfiles().find(({ role }) => role === "operator")).toMatchObject({ thinkingEffort: "medium", contextTokens: 256_000 });
    } finally {
      app.stop();
    }
  });

  test("blocks trusted Foreman work until a digest-pinned image is configured", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
    try {
      const queued = app.store.queueForemanMessage("trusted-before-image", "配置可信厂长");
      const response = await app.fetch(new Request("http://localhost/api/web/harnesses/configure", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ runtime: "claude-code", role: "foreman", model: "fixture" }),
      }));
      expect(response.status).toBe(200);
      await app.foreman.dispatch(queued.id);
      expect(app.store.getForemanMessage(queued.id)).toMatchObject({ status: "queued", businessStatus: "blocked_environment" });
      expect(app.events.after().some(({ type, data }) => type === "foreman.runtime.blocked" && (data as { inboxId: string }).inboxId === queued.id)).toBe(true);
    } finally {
      await app.foreman.close();
      app.stop();
    }
  });

  test("reports a trusted Foreman without an image as waiting for runtime", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
    try {
      const configured = await app.fetch(new Request("http://localhost/api/web/harnesses/configure", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ runtime: "codex", role: "foreman", model: "fixture" }),
      }));
      expect(configured.status).toBe(200);
      const response = await app.fetch(new Request("http://localhost/api/v1/talk", {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: "等待可信运行环境" }),
      }));
      expect(response.status).toBe(201);
      expect(await response.json()).toMatchObject({ state: "waiting_foreman_runtime", setup_required: false, foremanTurn: { status: "queued", businessStatus: "not_started" } });
    } finally {
      app.stop();
    }
  });

  test("defaults a configured trusted Foreman to bridge networking", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
    try {
      const response = await app.fetch(new Request("http://localhost/api/web/harnesses/configure", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ runtime: "claude-code", role: "foreman", model: "fixture", image: "registry.example/foreman@sha256:" + "a".repeat(64) }),
      }));
      expect(response.status).toBe(200);
      expect(app.store.listHarnessProfiles().find(({ role }) => role === "foreman")?.network).toBe("bridge");
    } finally {
      app.stop();
    }
  });

  test("keeps a trusted Foreman queued and marks the environment blocked when runtime readiness fails", async () => {
    const app = createApp({
      dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false,
      runtime: { health: async () => ({ available: false, platform: "linux", executable: "fixture", reason: "runtime offline", machine: null, rootless: null }), ensureReady: async () => ({ available: false, platform: "linux", executable: "fixture", reason: "runtime offline", machine: null, rootless: null }) },
    });
    try {
      app.store.saveHarnessProfile({ runtime: "claude-code", role: "foreman", baseUrl: "", model: "fixture", mode: "session", executionMode: "trusted_container", image: "registry.example/foreman@sha256:" + "a".repeat(64), network: "bridge" });
      const queued = app.store.queueForemanMessage("runtime-blocked", "继续执行");
      await app.foreman.dispatch(queued.id);
      expect(app.store.getForemanMessage(queued.id)).toMatchObject({ status: "queued", businessStatus: "blocked_environment", businessReason: "podman runtime unavailable: runtime offline" });
      expect(app.events.after().some(({ type, data }) => type === "foreman.runtime.blocked" && (data as { inboxId: string }).inboxId === queued.id)).toBe(true);
    } finally {
      app.stop();
    }
  });

  test("fails an ACP-only Foreman explicitly instead of guessing CLI flags", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
    try {
      app.store.saveHarnessProfile({ runtime: "deepseek-harness", role: "foreman", baseUrl: "", model: "fixture", mode: "session" });
      const queued = app.store.queueForemanMessage("acp-turn", "使用 DeepSeek Harness");
      await app.foreman.dispatch(queued.id);
      expect(app.store.getForemanMessage(queued.id)?.status).toBe("failed");
      expect(app.store.eventsAfter().some(({ type, data }) => type === "foreman.session.failed" && JSON.stringify(data).includes("official-acp-unavailable"))).toBe(true);
    } finally {
      app.stop();
    }
  });
});
