import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { resolveKiriClient } from "../backend/runtime/kiri";
import { parseImageId, resolvePodmanExecutable, runCommand } from "../backend/runtime/podman";
import { selectedContainerProvider } from "../backend/runtime/selected";
import { spawnOfficialProcess } from "../backend/src/harness";
import { Context, Gate, Runner, Warehouse, digestBytes, loadSnapshot } from "../backend/src/legacy";
import { materialInputRoot, materialOutputRoot } from "../backend/src/material-files";
import { createApp, type BackendApp } from "../backend/src/server";
import { VaultStore } from "../backend/src/vault";
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
const producedBytes = [3, 10, 42, 255, 0];
const repoUrl = "https://code.example/team/runtime-fixture.git";
const companionRepo = "https://code.example/team/companion-fixture.git";
const projectKey = "PROJECT_FIXTURE_KEY";
const sellerCredential = "seller-private-runtime-fixture-value";
const graph = { nodes: [{ id: "operator" }, { id: "machine" }], edges: [{ source: "operator", target: "machine" }] };

const foremanFixture = `
import assert from "node:assert/strict";
import { existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
const { reply, folderText, upload, repoUrl, companionRepo, graph, projectKey } = ${JSON.stringify({ reply: checkedReply, folderText, upload, repoUrl, companionRepo, graph, projectKey })};
const { producedBytes, materialInputRoot, materialOutputRoot } = ${JSON.stringify({ producedBytes, materialInputRoot, materialOutputRoot })};
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
  const sources = JSON.parse(process.argv.at(-1));
  const operator = sources?.operator === true;
  assert.equal(mode(mailbox + "/requests"), operator ? undefined : "rw");
  assert.equal(mode(mailbox + "/responses"), operator ? undefined : "ro");
  if (operator) assert.equal(mode(materialOutputRoot), "rw");
  if (sources?.consumer) {
    const input = materialInputRoot + "/1";
    assert.equal(mode(input), "ro");
    assert.equal(mode(materialInputRoot + "/2"), undefined);
    assert.equal(mode("/mao/sources/1"), undefined);
    assert.deepEqual([...readFileSync(input)], producedBytes);
    assert.throws(() => writeFileSync(input, "unexpected"));
    console.log(JSON.stringify({ type: "mao.record", record_type: "material", kind: "source-check", ref: "material://consumed-file", summary: "Read the upstream file" }));
  } else if (sources) {
    const folder = "/mao/sources/" + sources.folder;
    const file = "/mao/sources/" + sources.file;
    assert.equal(mode(folder), "ro");
    assert.equal(mode(file), "ro");
    assert.equal(readFileSync(folder + "/readme.txt", "utf8"), folderText);
    assert.deepEqual([...readFileSync(file)], upload);
    if (operator) {
      assert.equal(mode(materialInputRoot + "/1"), undefined);
      writeFileSync(materialOutputRoot + "/result.bin", Uint8Array.from(producedBytes), { mode: 0o644 });
      console.log(JSON.stringify({
        type: "mao.record", record_type: "material", kind: "source-check", ref: "material://sources-checked", summary: "Read both registered project sources",
        files: [{ staged: "result.bin", path: "build/result.bin" }],
      }));
    } else {
      assert.equal((await ask(1, "project.register", { repoUrl })).error.code, "FOREMAN_OPERATION_INVALID");
    }
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

const buyerFixture = `
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
const { materialInputRoot, producedBytes, projectKey, upload } = ${JSON.stringify({ materialInputRoot, producedBytes, projectKey, upload })};
const sources = JSON.parse(process.argv.at(-1));
const mounts = readFileSync("/proc/self/mountinfo", "utf8").split("\\n").map((line) => line.split(" "));
const mode = (path) => mounts.find((fields) => fields[4] === path)?.[5].split(",")[0];
assert.equal(process.getuid(), 10001);
assert.equal(process.env[projectKey], undefined);
const input = materialInputRoot + "/1";
assert.equal(mode(input), "ro");
assert.equal(mode(materialInputRoot + "/2"), undefined);
assert.deepEqual([...readFileSync(input)], producedBytes);
assert.throws(() => writeFileSync(input, "unexpected"));
if (sources.length) {
  assert.equal(sources.length, 1);
  assert.equal(mode(sources[0]), "ro");
  assert.deepEqual([...readFileSync(sources[0])], upload);
}
const role = sources.length ? "producer" : "consumer";
console.log(JSON.stringify({ type: "mao.record", record_type: "material", kind: "package-check", ref: "material://buyer-" + role, summary: "Read a file from the imported business package" }));
console.log(JSON.stringify({ type: "result", text: "Verified imported material" }));
`;

async function verifyBusinessTransfer(
  seller: BackendApp,
  fixture: { root: string; storageRoot: string; image: string; systemId: string; projectId: string; artifactId: string; digest: string },
): Promise<void> {
  const { root, storageRoot, image, systemId, projectId, artifactId, digest } = fixture;
  const credential = seller.store.createSecretRequest({
    projectId, name: projectKey, vaultRef: "vault://secret/seller-fixture", status: "available",
  });
  seller.vault.put(credential.vaultRef, sellerCredential);
  const pkg = join(root, "package");
  const buyerRoot = join(root, "buyer");
  const sellerDb = join(storageRoot, "factory.db");
  const cli = async (...args: string[]) => {
    const result = await runCommand([Bun.argv[0], "run", "backend/src/cli.ts", "business", ...args], {
      timeoutMs: 30_000,
      env: { FACTORY_ROOT: storageRoot, FACTORY_DB_PATH: sellerDb, BUSINESS_DB_PATH: sellerDb,
        MAO_VAULT_PATH: join(storageRoot, "vault.json"), MAO_VAULT_KEY_PATH: join(storageRoot, "vault.key") },
    });
    assert.equal(result.exitCode, 0, new TextDecoder().decode(result.stderr));
    return JSON.parse(new TextDecoder().decode(result.stdout));
  };
  const exported = await cli("export", systemId, "--package", pkg);
  assert.equal(exported.blobs.length, 2);
  const document = await Bun.file(join(pkg, "business.json")).text();
  for (const privateValue of [root, sellerCredential, credential.vaultRef]) {
    assert.ok(!document.includes(privateValue), "业务包包含卖方路径或凭据数据");
  }
  assert.deepEqual(await cli("verify", pkg), { ok: true, problems: [], acceptance: "not_run" });
  const imported = await cli("import", pkg, "--root", buyerRoot);
  assert.equal(imported.root, buyerRoot);
  assert.equal(imported.acceptance, "not_run");
  assert.notEqual(imported.system.id, systemId);

  const database = join(buyerRoot, "factory.db");
  const stages: string[] = [];
  const buyer = createApp({
    dbPath: database, legacyPath: database, storageRoot: buyerRoot, dispatchWork: false,
    vault: new VaultStore(join(buyerRoot, "vault.json"), join(buyerRoot, "vault.key")),
    operator: { spawn: (argv, options) => {
      assert.ok(options.materialFiles);
      assert.equal(options.materialFiles.inputs.length, 1);
      const { outputDirectory, inputs } = options.materialFiles;
      const sources = options.projectSources ?? [];
      stages.push(outputDirectory);
      assert.ok(argv.at(-1)?.includes("build/result.bin"));
      assert.ok(!argv.at(-1)?.includes(root));
      return spawnOfficialProcess(["bun", "-e", buyerFixture, JSON.stringify(sources.map(({ index }) => `/mao/sources/${index}`))], { ...options, stdin: undefined });
    } },
  });
  try {
    const buyerSystemId = imported.system.id;
    assert.deepEqual(buyer.store.listBusinessRuns(buyerSystemId), []);
    assert.deepEqual(buyer.store.listHarnessProfiles(), []);
    const buyerProjects = buyer.store.listProjects(buyerSystemId);
    const producer = buyerProjects.find(({ secretRequests }) => secretRequests.some(({ name }) => name === projectKey));
    const consumer = buyerProjects.find(({ id }) => id !== producer?.id);
    assert.ok(producer && consumer);
    assert.notEqual(producer.id, projectId);
    const [requested] = producer.secretRequests;
    assert.equal(requested.status, "requested");
    assert.notEqual(requested.vaultRef, credential.vaultRef);
    assert.equal(buyer.vault.get(requested.vaultRef), undefined);
    const material = buyer.store.listArtifacts(producer.id).find(({ ref }) => ref === "material://sources-checked");
    assert.ok(material);
    assert.notEqual(material.id, artifactId);
    assert.deepEqual(buyer.store.artifactFileGrants(buyerSystemId, [material.id]), [{ artifactId: material.id, position: 0, digest, size: producedBytes.length }]);
    const warehouse = new Warehouse(buyerRoot, buyer.store.db, buyer.store.clock);
    await chmod(warehouse.blobPath(digest), 0o600);
    buyer.store.saveHarnessProfile({
      runtime: "codex", role: "operator", baseUrl: "", model: "fixture", mode: "session", image, sessionTimeoutSeconds: 45,
    });
    const input = `material://${material.id}`;
    const response = await buyer.fetch(new Request(`http://localhost/api/v1/business-systems/${buyerSystemId}/runs`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ inputs: [input], dispatch: false }),
    }));
    assert.equal(response.status, 201);
    const { id: runId } = await response.json() as { id: string };
    const ready = buyer.store.listReadyUnitRuns(runId);
    assert.deepEqual(ready.map(({ unitId, inputs }) => ({ unitId, inputs })), [{ unitId: producer.id + ":operator", inputs: [input] }]);
    await buyer.operator.dispatch(runId, ready[0].id);
    const upstream = buyer.store.listArtifacts(producer.id).find(({ ref }) => ref === "material://buyer-producer");
    assert.ok(upstream, JSON.stringify(buyer.store.listUnitRuns(runId)));
    const next = buyer.store.listReadyUnitRuns(runId, ready[0].id);
    assert.deepEqual(next.map(({ unitId, inputs }) => ({ unitId, inputs })), [
      { unitId: consumer.id + ":operator", inputs: [input, upstream.ref] },
    ]);
    await buyer.operator.dispatch(runId, next[0].id);
    assert.equal(buyer.store.getBusinessRun(runId)?.status, "completed", JSON.stringify(buyer.store.listUnitRuns(runId)));
    assert.ok(buyer.store.listArtifacts(consumer.id).some(({ ref }) => ref === "material://buyer-consumer"));
    for (const path of stages) {
      try { await stat(path); throw new Error(`material staging path was not removed: ${path}`); }
      catch (error) { if ((error as { code?: string }).code !== "ENOENT") throw error; }
    }
  } finally {
    buyer.stop();
  }
}

async function verifyForeman(provider: string): Promise<void> {
  assert.equal(process.platform, "linux", "厂长容器邮箱需要本地 Linux 容器引擎");
  const image = await foremanImage(provider);
  const root = await mkdtemp(join(tmpdir(), "mao-runtime-foreman-"));
  const storageRoot = join(root, "factory");
  const sourceFolder = join(root, "selected project");
  const mailboxes: string[] = [];
  const uploads: string[] = [];
  const outputStages: string[] = [];
  const inputCopies: string[] = [];
  const sourceTargets = (prompt: string | undefined, sources: Array<{ index: number; source: string }>) => {
    assert.equal(sources.length, 2);
    const folder = sources.find(({ source }) => source === sourceFolder);
    const file = sources.find(({ source }) => source !== sourceFolder);
    assert.ok(folder && file, "缺少选定目录或上传文件的容器挂载");
    uploads.push(file.source);
    assert.ok(prompt?.includes("uploaded data.bin") && prompt.includes("/mao/sources/" + file.index));
    assert.ok(!prompt.includes(root));
    return { folder: folder.index, file: file.index };
  };
  const app = createApp({
    dbPath: join(storageRoot, "factory.db"), legacyPath: join(storageRoot, "factory.db"), storageRoot, dispatchWork: false,
    vault: new VaultStore(join(storageRoot, "vault.json"), join(storageRoot, "vault.key")),
    foreman: { spawn: (argv, options) => {
      assert.ok(options.foremanMailbox);
      mailboxes.push(options.foremanMailbox);
      const sources = options.projectSources ?? [];
      const targets = sources.length ? sourceTargets(argv.at(-1), sources) : null;
      return spawnOfficialProcess(["bun", "-e", foremanFixture, JSON.stringify(targets)], { ...options, env: {}, stdin: undefined });
    } },
    operator: { spawn: (argv, options) => {
      assert.equal(options.foremanMailbox, undefined);
      assert.ok(options.materialFiles);
      const { outputDirectory, inputs } = options.materialFiles;
      outputStages.push(outputDirectory);
      const sources = options.projectSources ?? [];
      const prompt = argv.at(-1);
      let targets;
      if (sources.length) {
        assert.deepEqual(inputs, []);
        targets = { ...sourceTargets(prompt, sources), operator: true };
      } else {
        assert.equal(inputs.length, 1);
        assert.equal(inputs[0].index, 1);
        assert.ok(prompt?.includes(materialInputRoot + "/1") && prompt.includes("build/result.bin"));
        assert.ok(!prompt.includes(root));
        inputCopies.push(inputs[0].source);
        targets = { operator: true, consumer: true };
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
    app.store.saveHarnessProfile({
      runtime: "codex", role: "operator", baseUrl: "", model: "fixture", mode: "session", image, sessionTimeoutSeconds: 45,
    });
    const intakeProject = app.store.getProject(project_id);
    assert.ok(intakeProject?.businessSystemId);
    const systemId = intakeProject.businessSystemId;
    const consumer = app.store.createIntake({ name: "Material consumer", business_system_id: systemId, repo_url: "https://code.example/consumer.git" });
    const producerUnit = project_id + ":operator";
    const consumerUnit = consumer.id + ":operator";
    const graphUpdate = await app.fetch(new Request(`http://localhost/api/v1/business-systems/${systemId}/graph`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ graph: {
        nodes: [{ id: producerUnit }, { id: consumerUnit }], edges: [{ source: producerUnit, target: consumerUnit }],
      } }),
    }));
    assert.equal(graphUpdate.status, 200);
    const run = app.store.createBusinessRun(systemId);
    assert.ok(run);
    const initial = app.store.listReadyUnitRuns(run.id);
    assert.deepEqual(initial.map(({ unitId }) => unitId), [producerUnit]);
    await app.operator.dispatch(run.id, initial[0].id);
    const artifact = app.store.listArtifacts(project_id).find(({ ref }) => ref === "material://sources-checked");
    assert.ok(artifact, JSON.stringify(app.store.listUnitRuns(run.id)));
    const bytes = Uint8Array.from(producedBytes);
    const digest = digestBytes(bytes);
    assert.deepEqual(app.store.artifactFileGrants(systemId, [artifact.id]), [{ artifactId: artifact.id, position: 0, digest, size: bytes.length }]);
    assert.deepEqual(app.store.artifactFileGrants(project.businessSystemId, [artifact.id]), []);
    const producedBlob = new Warehouse(storageRoot, app.store.db, app.store.clock).blobPath(digest);
    await chmod(producedBlob, 0o600);
    const stored = await app.fetch(new Request(`http://localhost/api/v1/projects/${project_id}/artifacts/${artifact.id}/files/${digest}`));
    assert.equal(stored.status, 200);
    assert.deepEqual(new Uint8Array(await stored.arrayBuffer()), bytes);
    const downstream = app.store.listReadyUnitRuns(run.id, initial[0].id);
    assert.deepEqual(downstream.map(({ unitId, inputs }) => ({ unitId, inputs })), [
      { unitId: consumerUnit, inputs: [`material://${artifact.id}`] },
    ]);
    await app.operator.dispatch(run.id, downstream[0].id);
    assert.equal(app.store.getBusinessRun(run.id)?.status, "completed", JSON.stringify(app.store.listUnitRuns(run.id)));
    assert.ok(app.store.listArtifacts(consumer.id).some(({ ref }) => ref === "material://consumed-file"));
    assert.equal(inputCopies.length, 1);
    assert.notEqual(inputCopies[0], producedBlob);
    assert.equal(outputStages.length, 2);
    assert.equal(uploads.length, 2);
    for (const path of uploads) assert.notEqual(path, blob);
    for (const path of [...mailboxes, ...uploads, ...outputStages, ...inputCopies]) {
      try { await stat(path); throw new Error(`foreman staging path was not removed: ${path}`); }
      catch (error) { if ((error as { code?: string }).code !== "ENOENT") throw error; }
    }
    assert.equal(app.store.listTalkMessages().filter(({ body }) => body === checkedReply).length, 2);
    console.log(`${provider}: 厂长邮箱、仓库登记、业务图、项目密钥申请、项目来源挂载、Operator 间 CAS 文件交接和清理通过`);
    await verifyBusinessTransfer(app, { root, storageRoot, image, systemId, projectId: project_id, artifactId: artifact.id, digest });
    console.log(`${provider}: 业务包 CLI 移交、买方 DAG、已导入 CAS 和项目凭据隔离通过`);
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
  if (typeof Bun.YAML?.parse !== "function" || typeof Bun.YAML?.stringify !== "function") {
    throw new Error(`Bun ${Bun.version} 缺少运行时验收所需的 Bun.YAML 接口。请使用已验证的 Bun 1.3.14`);
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
