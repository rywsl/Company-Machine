import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { resolveKiriClient } from "../backend/runtime/kiri";
import { parseImageId, resolvePodmanExecutable, runCommand } from "../backend/runtime/podman";
import { selectedContainerProvider } from "../backend/runtime/selected";
import { spawnOfficialProcess } from "../backend/src/harness";
import { Context, Gate, Runner, Warehouse, loadSnapshot } from "../backend/src/legacy";
import { createApp } from "../backend/src/server";
import { imageIds, imageTag, renderRegistry, writeJson, writeText } from "../tests/fakes/fakelib";

const verifyMounts = `
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
assert.equal(process.getuid(), 10001);
for (const path of ["/factory/in/task/task.json", "/opt/factory/bin/factoryctl"]) {
  assert.throws(() => writeFileSync(path, "unexpected"));
}
`;

async function verifyWorkstation(provider: ReturnType<typeof selectedContainerProvider>): Promise<void> {
  const images = await imageIds();
  const root = await mkdtemp(join(tmpdir(), "mao-runtime-"));
  const context = Context.open(root);
  let jobId: string | undefined;
  let passed = false;
  try {
    const registry = await renderRegistry(join(root, "registry"), { images });
    const snapshot = await loadSnapshot(registry);
    const gate = new Gate(snapshot, context.warehouse, join(root, "tmp", "gate"), provider);
    const expected = "one\ntwo\n";
    const path = await writeJson(join(root, "task.json"), { cost_usd: 0.1, default: [
      { do: "run", argv: ["bun", "-e", verifyMounts] },
      { do: "ctl", args: ["inputs", "--json"] },
      { do: "ctl", args: ["log", "mounted factoryctl"] },
      { do: "ctl", args: ["approval", "request", "--kind", "cost", "--summary", "fixture", "--cost-usd", "0.1"] },
      { do: "write", path: "/workspace/drafts/note.txt", text: expected },
      { do: "ctl", args: ["check", "note", "/workspace/drafts/note.txt"] },
    ] });
    const { report, bean } = await gate.admitExternal("agent.task@1.0", { "task.json": path }, {});
    assert.ok(bean, report.data.feedback);
    context.warehouse.sealMany([[bean.id, report.data.facts[bean.id]]], null, report.digest());
    const runner = new Runner(context, snapshot, provider);
    jobId = runner.submit("writer@1", { task: bean.id }, {}).id;
    const finished = await runner.run(jobId);
    assert.equal(finished.state, "SEALED", JSON.stringify(finished.error));
    assert.equal(finished.attempt, 1);
    assert.equal(runner.approvals.list({ jobId })[0]?.decidedBy, "auto");
    const output = context.warehouse.get(finished.outputs.note[0]);
    assert.equal(output.facts.lines, 2);
    assert.equal(await Bun.file(context.warehouse.blobPath(output.payload[0].digest)).text(), expected);
    assert.deepEqual(context.warehouse.lineage(output.id, "up").beans, [bean.id]);
    assert.match(await Bun.file(join(root, "jobs", jobId, "1", "ctl", "log.jsonl")).text(), /mounted factoryctl/);
    console.log(`${provider.name}: 作业 ${jobId} 已封存，预检、审批和物料血缘通过`);
    passed = true;
  } finally {
    context.close();
    if (passed) await rm(root, { recursive: true, force: true });
    else console.error(`验收数据 ${root}${jobId ? `，作业 ${jobId}` : ""}`);
  }
}

async function foremanImage(provider: string): Promise<string> {
  const kiri = provider === "kiri" ? resolveKiriClient() : undefined;
  const engine = kiri?.argv ?? [resolvePodmanExecutable()];
  const format = kiri ? "{{.Id}}" : "{{index .RepoDigests 0}}";
  const inspected = await runCommand([...engine, "image", "inspect", "--format", format, imageTag("operator")], { env: kiri?.env });
  const output = new TextDecoder().decode(inspected.stdout).trim();
  const image = kiri ? "mao-test/operator@" + parseImageId(output) : output;
  assert.ok(inspected.exitCode === 0 && /^\S+@sha256:[0-9a-f]{64}$/.test(image),
    "找不到已固定摘要的 mao-test/operator。先运行 bun run build:test-images --only operator");
  return image;
}

const checkedReply = "已登记仓库、更新业务图并验证只读来源";
const folderText = "selected project folder";
const upload = [0, 1, 13, 255];
const repoUrl = "https://code.example/team/runtime-fixture.git";
const companionRepo = "https://code.example/team/companion-fixture.git";
const projectKey = "PROJECT_FIXTURE_KEY";
const graph = { nodes: [{ id: "operator" }, { id: "machine" }], edges: [{ source: "operator", target: "machine" }] };

const foremanFixture = `
import assert from "node:assert/strict";
import { existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
const { reply, folderText, upload, repoUrl, companionRepo, graph, projectKey } = ${JSON.stringify({ reply: checkedReply, folderText, upload, repoUrl, companionRepo, graph, projectKey })};
const mailbox = "/mao/foreman";
const mountInfo = readFileSync("/proc/self/mountinfo", "utf8").split("\\n").map((line) => line.split(" "));
const mode = (path) => mountInfo.find((fields) => fields[4] === path)?.[5].split(",")[0];
async function ask(index, op, args) {
  const id = "r-" + index.toString(16).padStart(32, "0");
  const request = mailbox + "/requests/" + id;
  writeFileSync(request + ".part", JSON.stringify({ id, op, args }), { mode: 0o644 });
  renameSync(request + ".part", request + ".json");
  const response = mailbox + "/responses/" + id + ".json";
  for (let attempt = 0; attempt < 100; attempt++) {
    if (existsSync(response)) {
      const result = JSON.parse(readFileSync(response, "utf8"));
      assert.equal(result.id, id);
      return result;
    }
    await Bun.sleep(50);
  }
  throw new Error("Mailbox response timed out for " + op);
}
try {
  assert.equal(process.getuid(), 10001);
  assert.equal(mode(mailbox + "/requests"), "rw");
  assert.equal(mode(mailbox + "/responses"), "ro");
  const sources = JSON.parse(process.argv.at(-1));
  if (sources) {
    const folder = "/mao/sources/" + sources.folder;
    const file = "/mao/sources/" + sources.file;
    assert.equal(mode(folder), "ro");
    assert.equal(mode(file), "ro");
    assert.equal(readFileSync(folder + "/readme.txt", "utf8"), folderText);
    assert.deepEqual([...readFileSync(file)], upload);
    assert.equal((await ask(1, "project.register", { repoUrl })).error.code, "FOREMAN_OPERATION_INVALID");
  } else {
    assert.equal(mode("/mao/sources/1"), undefined);
    const registered = await ask(1, "project.register", { repoUrl });
    assert.equal(registered.ok, true, JSON.stringify(registered.error));
    const { systemId, projectId } = registered.result;
    const units = await ask(2, "business.units", { systemId });
    assert.equal(units.ok, true, JSON.stringify(units.error));
    assert.equal(units.result.length, 2);
    const recorded = await ask(3, "business.graph", { systemId, graph });
    assert.equal(recorded.ok, true, JSON.stringify(recorded.error));
    const secret = { projectId, name: projectKey, purpose: "Run the project's publishing step" };
    const requested = await ask(4, "project.secret.request", secret);
    assert.equal(requested.ok, true, JSON.stringify(requested.error));
    assert.equal(typeof requested.result.id, "string");
    assert.deepEqual(Object.keys(requested.result).sort(), ["id", "status"]);
    assert.deepEqual((await ask(5, "project.secret.request", secret)).result, requested.result);
    const attached = await ask(6, "project.register", { systemId, repoUrl: companionRepo });
    assert.equal(attached.ok, true, JSON.stringify(attached.error));
    assert.equal(attached.result.systemId, systemId);
    assert.notEqual(attached.result.projectId, projectId);
    assert.equal((await ask(7, "business.units", { systemId })).result.length, 4);
  }
  console.log(JSON.stringify({ type: "result", text: reply }));
} catch (error) {
  console.log(JSON.stringify({ type: "error", text: String(error.stack ?? error) }));
  process.exitCode = 1;
}
`;

async function verifyForeman(provider: string): Promise<void> {
  assert.equal(process.platform, "linux", "厂长容器邮箱需要本地 Linux 容器引擎");
  const image = await foremanImage(provider);
  const root = await mkdtemp(join(tmpdir(), "mao-runtime-foreman-"));
  const storageRoot = join(root, "factory");
  const sourceFolder = join(root, "selected project");
  const mailboxes: string[] = [];
  const uploads: string[] = [];
  const app = createApp({
    dbPath: join(root, "business.db"), legacyPath: ":memory:", storageRoot, dispatchWork: false,
    foreman: { spawn: (argv, options) => {
      assert.ok(options.foremanMailbox);
      mailboxes.push(options.foremanMailbox);
      const sources = options.foremanSources ?? [];
      let targets = null;
      if (sources.length) {
        assert.equal(sources.length, 2);
        const folder = sources.find(({ source }) => source === sourceFolder);
        const file = sources.find(({ source }) => source !== sourceFolder);
        assert.ok(folder && file, "缺少选定目录或上传文件的容器挂载");
        uploads.push(file.source);
        targets = { folder: folder.index, file: file.index };
        const prompt = argv.at(-1);
        assert.ok(prompt?.includes("uploaded data.bin") && prompt.includes("/mao/sources/" + file.index));
        assert.ok(!prompt.includes(root));
      }
      return spawnOfficialProcess(["bun", "-e", foremanFixture, JSON.stringify(targets)], { ...options, env: {}, stdin: undefined });
    } },
  });
  let passed = false;
  try {
    app.store.saveHarnessProfile({
      runtime: "claude-code", role: "foreman", baseUrl: "", model: "fixture", mode: "session", image, sessionTimeoutSeconds: 45,
    });
    const dispatch = async (inboxId: string) => {
      await app.foreman.dispatch(inboxId);
      const diagnostic = app.events.after().filter(({ type }) => type.startsWith("foreman.session") && type.endsWith("failed"));
      assert.equal(app.store.getForemanMessage(inboxId)?.status, "done", JSON.stringify(diagnostic));
    };
    const talk = await app.fetch(new Request("http://localhost/api/v1/talk", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: "用仓库搭建业务系统" }),
    }));
    assert.equal(talk.status, 201);
    await dispatch(app.store.listForemanInbox()[0].id);
    const projects = app.store.listProjects();
    const project = projects.find(({ sources }) => sources[0]?.repoUrl === repoUrl);
    const companion = projects.find(({ sources }) => sources[0]?.repoUrl === companionRepo);
    assert.equal(projects.length, 2);
    assert.ok(project?.businessSystemId);
    assert.ok(companion);
    assert.equal(companion.businessSystemId, project.businessSystemId);
    assert.deepEqual(app.store.getBusinessSystem(project.businessSystemId)?.metadata.graph, graph);
    const requests = app.store.listSecretRequests();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].name, projectKey);
    assert.equal(requests[0].projectId, project.id);
    assert.ok(app.events.after().some(({ type, data }) => type === "secret.requested" && data.id === requests[0].id));

    await mkdir(sourceFolder, { mode: 0o755 });
    await writeText(join(sourceFolder, "readme.txt"), folderText);
    const form = new FormData();
    form.set("name", "项目来源夹具");
    form.set("local_folder", sourceFolder);
    form.set("files", new File([Uint8Array.from(upload)], "uploaded data.bin"));
    const intake = await app.fetch(new Request("http://localhost/api/v1/projects/intake", { method: "POST", body: form }));
    assert.equal(intake.status, 201);
    const { project_id, foreman_inbox_id } = await intake.json() as { project_id: string; foreman_inbox_id: string };
    const fileRef = app.store.getProject(project_id)?.sources.find(({ kind }) => kind === "uploaded_file")?.fileRef;
    assert.ok(fileRef?.startsWith("cas://"), "项目缺少登记的 CAS 上传记录");
    const blob = new Warehouse(storageRoot, app.store.db, app.store.clock).blobPath(fileRef.slice(6));
    await chmod(blob, 0o600);
    await dispatch(foreman_inbox_id);
    assert.equal(uploads.length, 1);
    assert.notEqual(uploads[0], blob);
    for (const path of [...mailboxes, ...uploads]) await assert.rejects(stat(path), { code: "ENOENT" });
    assert.equal(app.store.listTalkMessages().filter(({ body }) => body === checkedReply).length, 2);
    console.log(`${provider}: 厂长邮箱、仓库登记、业务图、项目密钥申请、项目来源只读挂载和清理通过`);
    passed = true;
  } finally {
    app.stop();
    if (passed) await rm(root, { recursive: true, force: true });
    else console.error(`验收数据 ${root}`);
  }
}

async function main(): Promise<void> {
  const args = Bun.argv.slice(2);
  if (args.length && (args.length !== 1 || args[0] !== "--foreman")) {
    throw new Error("用法 bun run tools/verify-runtime.ts [--foreman]");
  }
  const provider = selectedContainerProvider();
  const health = await provider.ensureReady();
  assert.ok(health.available, health.reason ?? `${provider.name} unavailable`);
  if (args.length) await verifyForeman(provider.name);
  else await verifyWorkstation(provider);
}

try {
  await main();
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
