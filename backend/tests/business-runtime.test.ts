import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rm } from "node:fs/promises";
import { chmodSync, existsSync, mkdirSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createApp } from "../src/server";
import type { OfficialProcess } from "../src/harness";
import { OperatorCoordinator, projectCredentialName } from "../src/operator";
import { prepareProjectSources, type PreparedProjectSources } from "../src/project-source-mounts";
import { Warehouse } from "../src/legacy/warehouse";
import { createMaterialFileStage } from "../src/material-files";
import { VaultStore } from "../src/vault";

function processForRecord(ref = "cas://report-1", summary = "项目已完成构建"): OfficialProcess {
  const record = JSON.stringify({ type: "mao.record", record_type: "material", kind: "build.report", ref, title: "构建报告", summary, files: [{ ref: `${ref}:file`, path: "build/report.md" }] });
  const body = new TextEncoder().encode(`${JSON.stringify({ type: "assistant", text: "开始构建" })}\n${record}\n${JSON.stringify({ type: "result", text: "完成" })}\n`);
  return { stdout: new ReadableStream({ start(controller) { controller.enqueue(body); controller.close(); } }), kill() {} };
}

function processForFailure(): OfficialProcess {
  const body = new TextEncoder().encode(`${JSON.stringify({ type: "error", text: "构建失败" })}\n`);
  return { stdout: new ReadableStream({ start(controller) { controller.enqueue(body); controller.close(); } }), kill() {} };
}

describe("Bun business execution loop", () => {
  test("publishes staged Operator bytes and grants them to the dependent Operator and UI", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-material-handoff-"));
    const bytes = Uint8Array.from([0, 7, 128, 255, 13]);
    const stages: string[] = [];
    let producerClosed = false;
    let producerId = "";
    let failProducer = false;
    let consumerReads = 0;
    const app = createApp({
      dbPath: ":memory:", legacyPath: ":memory:", storageRoot: root, dispatchWork: false,
      runtime: { health: async () => ({ available: true, platform: process.platform, machine: null, executable: "fixture", rootless: true }) },
      operator: {
        materialStage: () => createMaterialFileStage(warehouse),
        spawn: async (argv, options) => {
          const stage = options.materialFiles!;
          const prompt = argv.at(-1)!;
          stages.push(stage.outputDirectory);
          expect(options.foremanMailbox).toBeUndefined();
          if (!prompt.includes(producerId)) {
            expect(stage.inputs).toHaveLength(1);
            expect([...new Uint8Array(await Bun.file(stage.inputs[0].source).arrayBuffer())]).toEqual([...bytes]);
            expect(prompt).toContain("/mao/inputs/1");
            expect(prompt).not.toContain(root);
            consumerReads++;
            return processForRecord("material://review");
          }
          expect(stage.inputs).toEqual([]);
          await Bun.write(join(stage.outputDirectory, "report.bin"), bytes);
          const lines = [
            { type: "mao.record", record_type: "material", kind: "report", ref: "material://report", summary: "Report bytes",
              files: [{ staged: "report.bin", path: "reports/report.bin" }] },
            { type: failProducer ? "error" : "result", text: "finished" },
          ];
          const stdout = new ReadableStream<Uint8Array>({ start(controller) {
            controller.enqueue(new TextEncoder().encode(lines.map((item) => JSON.stringify(item)).join("\n") + "\n"));
            controller.close();
          } });
          return { stdout, kill() {
            expect(app.store.listArtifacts(producerId).length).toBe(failProducer ? 1 : 0);
            producerClosed = true;
          } };
        },
      },
    });
    const warehouse = new Warehouse(root, app.store.db, app.store.clock);
    try {
      const system = app.store.createBusinessSystem({ name: "File handoff" });
      const create = (name: string) => app.store.createIntake({
        name, business_system_id: system.id, repo_url: "https://example.test/" + name,
      });
      const producer = create("producer");
      producerId = producer.id;
      const consumer = create("consumer");
      const sourceUnit = producer.id + ":operator";
      const targetUnit = consumer.id + ":operator";
      app.store.updateBusinessSystemMetadata(system.id, { graph: {
        nodes: [{ id: sourceUnit }, { id: targetUnit }], edges: [{ source: sourceUnit, target: targetUnit }],
      } });
      app.store.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "", model: "fixture", mode: "session",
        image: `registry.example/operator@sha256:${"a".repeat(64)}` });
      const run = app.store.createBusinessRun(system.id)!;
      const [upstream] = app.store.listReadyUnitRuns(run.id);
      await app.operator.dispatch(run.id, upstream.id);
      expect(producerClosed).toBe(true);
      expect(app.store.getUnitRun(run.id, upstream.id)?.status).toBe("completed");
      const [material] = app.store.listArtifacts(producer.id);
      const [grant] = app.store.artifactFileGrants(system.id, [material.id]);
      expect(material.files?.[0]).toMatchObject({ ref: "cas://" + grant.digest, path: "reports/report.bin" });
      expect([...new Uint8Array(await Bun.file(warehouse.blobPath(grant.digest)).arrayBuffer())]).toEqual([...bytes]);
      const forged = app.store.addArtifact({
        projectId: consumer.id, kind: "report", ref: "material://ungranted", files: material.files, metadata: {},
      });
      const [downstream] = app.store.listReadyUnitRuns(run.id, upstream.id);
      expect(downstream.inputs).toEqual([`material://${material.id}`]);
      app.store.db.query("UPDATE unit_runs SET inputs=? WHERE id=?").run(JSON.stringify([...downstream.inputs, forged.ref]), downstream.id);
      await app.operator.dispatch(run.id, downstream.id);
      expect(consumerReads).toBe(1);
      expect(app.store.getBusinessRun(run.id)?.status).toBe("completed");
      const path = `/api/v1/projects/${producer.id}/artifacts/${material.id}/files/${grant.digest}`;
      const get = (target: string) => app.fetch(new Request("http://localhost" + target));
      const downloaded = await get(path);
      expect(downloaded.status).toBe(200);
      expect(downloaded.headers.get("content-disposition")).toContain("report.bin");
      expect([...new Uint8Array(await downloaded.arrayBuffer())]).toEqual([...bytes]);
      expect((await get(path.replace(producer.id, consumer.id))).status).toBe(404);
      expect((await get(`/api/v1/projects/${consumer.id}/artifacts/${forged.id}/files/${grant.digest}`)).status).toBe(404);
      const assets = await (await get("/api/web/assets")).json() as Array<{ id: string; files: object[] }>;
      expect(assets.find(({ id }) => id === material.id)?.files[0]).toMatchObject({ download: path });
      expect(assets.find(({ id }) => id === forged.id)?.files[0]).not.toHaveProperty("download");

      failProducer = true;
      const failed = app.store.createBusinessRun(system.id)!;
      await app.operator.dispatch(failed.id, app.store.listReadyUnitRuns(failed.id)[0].id);
      expect(app.store.getBusinessRun(failed.id)?.status).toBe("failed");
      expect(app.store.listArtifacts(producer.id)).toHaveLength(1);
      for (const stage of stages) expect(existsSync(stage)).toBe(false);
    } finally {
      await app.stop();
      await rm(root, { recursive: true, force: true });
    }
  });

  test("stages only the Operator project's registered sources and clears private upload copies", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-operator-sources-"));
    const storageRoot = join(root, "factory");
    const folder = join(root, "chosen");
    mkdirSync(folder);
    await Bun.write(join(folder, "readme.txt"), "chosen project");
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", storageRoot, dispatchWork: false });
    const warehouse = new Warehouse(storageRoot, app.store.db, app.store.clock);
    let mounts: PreparedProjectSources["mounts"] | undefined;
    const operator = new OperatorCoordinator({
      store: app.store, events: app.events,
      prepareSources: (project) => prepareProjectSources(project, (digest) => {
        const row = app.store.db.query<{ size: number }>("SELECT size FROM intake_uploads WHERE digest=?").get(digest);
        return row ? { path: warehouse.blobPath(digest), size: row.size } : undefined;
      }, [storageRoot]),
      spawn: async (argv, options) => {
        mounts = options.projectSources;
        expect(options.foremanMailbox).toBeUndefined();
        expect(mounts?.length).toBe(2);
        const upload = mounts?.find(({ source }) => source !== folder);
        expect(upload).toBeDefined();
        expect(await Bun.file(upload!.source).text()).toBe("upload bytes");
        expect(await Bun.file(join(mounts!.find(({ source }) => source === folder)!.source, "readme.txt")).text()).toBe("chosen project");
        const prompt = argv.at(-1);
        expect(prompt).toContain(`/mao/sources/${upload!.index}`);
        expect(prompt).toContain("fixture upload.txt");
        expect(prompt).not.toContain(root);
        expect(prompt).not.toContain("other-project.invalid");
        return processForRecord();
      },
    });
    try {
      const system = app.store.createBusinessSystem({ name: "Source isolation" });
      const form = new FormData();
      form.set("name", "Source project");
      form.set("business_system_id", system.id);
      form.set("local_folder", folder);
      form.set("files", new File(["upload bytes"], "fixture upload.txt"));
      const created = await app.fetch(new Request("http://localhost/api/v1/projects/intake", { method: "POST", body: form }));
      expect(created.status).toBe(201);
      const { project_id } = await created.json() as { project_id: string };
      const submitted = app.store.getProject(project_id)?.sources.find(({ kind }) => kind === "uploaded_file");
      const blob = warehouse.blobPath(submitted!.fileRef!.slice(6));
      chmodSync(blob, 0o600);
      app.store.createIntake({ name: "Unrelated", business_system_id: system.id, repo_url: "https://other-project.invalid/repo" });
      app.store.saveHarnessProfile({
        runtime: "codex", role: "operator", baseUrl: "", model: "fixture", mode: "session",
        image: `registry.example/operator@sha256:${"a".repeat(64)}`,
      });
      const run = app.store.createBusinessRun(system.id)!;
      const own = app.store.listUnitRuns(run.id).find(({ unitId }) => unitId === `${project_id}:operator`)!;
      await operator.dispatch(run.id, own.id);
      expect(app.store.getUnitRun(run.id, own.id)?.status).toBe("completed");
      const temporary = mounts?.find(({ source }) => source !== folder);
      if (process.platform !== "win32") {
        expect(temporary?.source).not.toBe(blob);
        expect(existsSync(temporary!.source)).toBe(false);
      }
      expect(await Bun.file(blob).text()).toBe("upload bytes");
      expect(await Bun.file(join(folder, "readme.txt")).text()).toBe("chosen project");
    } finally {
      await operator.close();
      app.stop();
      await rm(root, { recursive: true, force: true });
    }
  });

  test("discards a project source preparation that finishes after the Operator deadline", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
    let started = false;
    let closed = 0;
    let prepared!: (value: PreparedProjectSources) => void;
    const preparing = new Promise<PreparedProjectSources>((resolve) => { prepared = resolve; });
    const operator = new OperatorCoordinator({
      store: app.store, events: app.events,
      prepareSources: () => { started = true; return preparing; },
      spawn: () => { throw new Error("Cannot start an expired Operator"); },
    });
    try {
      const system = app.store.createBusinessSystem({ name: "Late source cleanup" });
      app.store.createIntake({ name: "Source", business_system_id: system.id, repo_url: "https://example.invalid/repo" });
      app.store.saveHarnessProfile({
        runtime: "codex", role: "operator", baseUrl: "", model: "fixture", mode: "session",
        image: `registry.example/operator@sha256:${"a".repeat(64)}`, sessionTimeoutSeconds: 1,
      });
      const run = app.store.createBusinessRun(system.id)!;
      await operator.dispatch(run.id);
      expect(started).toBe(true);
      expect(app.store.listUnitRuns(run.id)[0]).toMatchObject({ status: "failed", error: "SESSION_TIMEOUT" });
      prepared({ facts: [], mounts: [], close: () => { closed += 1; } });
      await preparing;
      expect(closed).toBe(1);
    } finally {
      prepared({ facts: [], mounts: [], close() {} });
      await operator.close();
      app.stop();
    }
  });
  test("times out an Operator during ACP startup and releases the child", async () => {
    let killed = 0;
    let readsCancelled = 0;
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false, operator: { acpSpawn: () => ({
      stdout: new ReadableStream({ cancel() { readsCancelled++; } }), stdin: { write() {} },
      kill: () => { killed++; },
    }) } });
    try {
      const system = app.store.createBusinessSystem({ name: "ACP startup" });
      const project = app.store.createIntake({ business_system_id: system.id, name: "Example", repo_url: "https://example.test/project.git" });
      app.store.saveHarnessProfile({ runtime: "kimi-code", role: "operator", baseUrl: "", model: "fixture", mode: "session", sessionTimeoutSeconds: 1 });
      app.store.updateOperatorBinding(system.id, `${project.id}:operator`, { harnessRuntime: "kimi-code" });
      const run = app.store.createBusinessRun(system.id)!;
      await app.operator.dispatch(run.id);
      expect(app.store.getBusinessRun(run.id)?.status).toBe("failed");
      expect(app.store.listUnitRuns(run.id)[0]).toMatchObject({ status: "failed", error: "SESSION_TIMEOUT" });
      expect(app.events.after().filter(({ type }) => type === "operator.run.failed")).toHaveLength(1);
      expect(killed).toBe(1);
      expect(readsCancelled).toBe(1);
    } finally { app.stop(); }
  });

  test("reserves engine connection and registry configuration for the deployment", () => {
    for (const name of ["CONTAINER_HOST", "CONTAINER_CONNECTION", "CONTAINERS_CONF", "REGISTRY_AUTH_FILE"]) {
      expect(projectCredentialName(name)).toBe(false);
    }
    expect(projectCredentialName("PUBLISH_TOKEN")).toBe(true);
  });

  test("persists a scoped Operator binding, projects it in the graph and invokes the exact model", async () => {
    const invocations: string[][] = [];
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", operator: { spawn: (argv) => {
      invocations.push(argv);
      return processForRecord();
    } } });
    try {
      const system = app.store.createBusinessSystem({ name: "Binding" });
      const other = app.store.createBusinessSystem({ name: "Other" });
      const project = app.store.createIntake({ business_system_id: system.id, name: "Model", repo_url: "https://example.test/project.git" });
      const unitId = `${project.id}:operator`;
      app.store.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "", model: "codex-default", mode: "session" });
      app.store.saveHarnessProfile({ runtime: "OperatorRuntime:claude-code@4", role: "operator", baseUrl: "", model: "claude-default", mode: "session" });
      const bind = (body: unknown, systemId = system.id, id = unitId) =>
        app.fetch(new Request(`http://localhost/api/v1/business-systems/${systemId}/units/${encodeURIComponent(id)}/binding`, {
          method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
        }));
      const binding = { harness_runtime: "claude-code", model: "custom/model@v2" };
      expect((await bind({ ...binding, harness_runtime: "code" })).status).toBe(400);
      expect((await bind(binding, other.id)).status).toBe(404);
      expect((await bind(binding, system.id, `${project.id}:machine`)).status).toBe(404);
      const response = await bind(binding);
      expect(response.status).toBe(200);
      expect(await response.json()).toMatchObject({ binding: { harnessRuntime: "claude-code", model: binding.model, effectiveModel: binding.model } });
      const graph = await (await app.fetch(new Request(`http://localhost/api/web/graph?scope=workarea&workarea=${system.id}`))).json() as { nodes: unknown[] };
      expect(graph.nodes.find((node) => (node as { id: string }).id === unitId)).toMatchObject({ operatorBinding: { effectiveModel: binding.model } });
      const run = app.store.createBusinessRun(system.id)!;
      await app.operator.dispatch(run.id);
      expect(invocations).toHaveLength(1);
      expect(invocations[0][0]).toBe("claude");
      expect(invocations[0][invocations[0].indexOf("--model") + 1]).toBe(binding.model);
      expect(app.store.getBusinessRun(run.id)?.status).toBe("completed");
      expect((await bind({ harness_runtime: "codex", model: null })).status).toBe(200);
      const next = app.store.createBusinessRun(system.id)!;
      await app.operator.dispatch(next.id);
      expect(invocations[1][0]).toBe("codex");
      expect(invocations[1][invocations[1].indexOf("--model") + 1]).toBe("codex-default");
    } finally { app.stop(); }
  });

  test("resumes the requesting project only after all of its required credentials are available", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-credentials-"));
    const credential = "local-test-credential";
    const invocations: Record<string, string>[] = [];
    const app = createApp({
      dbPath: ":memory:", legacyPath: ":memory:",
      vault: new VaultStore(join(root, "vault.json"), join(root, "vault.key")),
      operator: { spawn: (_args, { env }) => {
        invocations.push(env);
        if (env.VIDEO_API_KEY && env.PUBLISH_TOKEN) return processForRecord("cas://configured-project", `Produced with ${credential}`);
        const requests = ["VIDEO_API_KEY", "VIDEO_API_KEY", "PUBLISH_TOKEN"].map((name) => JSON.stringify({ type: "mao.secret_request", name, purpose: "Fixture operation" })).join("\n");
        const body = new TextEncoder().encode(`${requests}\n`);
        return { stdout: new ReadableStream({ start(controller) { controller.enqueue(body); controller.close(); } }), kill() {} };
      } },
    });
    const post = async (path: string, body: unknown) => app.fetch(new Request(`http://localhost${path}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
    }));
    const waitFor = async (check: () => boolean) => {
      for (let attempt = 0; attempt < 40 && !check(); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 5));
      expect(check()).toBe(true);
    };
    try {
      const system = await (await post("/api/v1/business-systems", { name: "项目资源" })).json() as { id: string };
      const project = await (await post("/api/v1/projects/intake", { business_system_id: system.id, name: "视频项目", repo_url: "https://example.test/video.git" })).json() as { id: string };
      app.store.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "https://example.invalid", model: "fixture", mode: "session" });
      const run = await (await post(`/api/v1/business-systems/${system.id}/runs`, {})).json() as { id: string };
      await waitFor(() => app.store.listUnitRuns(run.id)[0]?.status === "waiting_resources");
      const requests = app.store.listSecretRequests();
      expect(requests.map(({ name }) => name).sort()).toEqual(["PUBLISH_TOKEN", "VIDEO_API_KEY"]);
      expect(requests.every(({ projectId }) => projectId === project.id)).toBe(true);
      const [first, second] = requests;
      expect((await post(`/api/v1/secrets/requests/${first.id}/value`, { value: credential })).status).toBe(200);
      expect(app.store.listUnitRuns(run.id)[0]?.status).toBe("waiting_resources");
      expect(invocations).toHaveLength(1);
      expect((await post(`/api/v1/secrets/requests/${second.id}/value`, { value: credential })).status).toBe(200);
      await waitFor(() => app.store.getBusinessRun(run.id)?.status === "completed");
      expect(invocations).toHaveLength(2);
      expect(invocations[1]).toMatchObject({ VIDEO_API_KEY: credential, PUBLISH_TOKEN: credential });
      expect(JSON.stringify(app.events.after())).not.toContain(credential);
      expect(app.store.listArtifacts(project.id)).toHaveLength(1);
      expect(app.store.listArtifacts(project.id)[0].summary).toBe("Produced with [REDACTED]");
    } finally {
      await app.operator.close();
      await app.foreman.close();
      app.store.close();
      await rm(root, { recursive: true, force: true });
    }
  });

  test("keeps the dependency edges of an existing run when the system is edited", () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:" });
    try {
      const system = app.store.createBusinessSystem({ name: "DAG 快照" });
      const first = app.store.createIntake({ business_system_id: system.id, name: "First", repo_url: "https://example.test/first.git" });
      const second = app.store.createIntake({ business_system_id: system.id, name: "Second", repo_url: "https://example.test/second.git" });
      const source = `${first.id}:operator`;
      const target = `${second.id}:operator`;
      app.store.updateBusinessSystemMetadata(system.id, { graph: { edges: [{ source, target }] } });
      const earlier = app.store.createBusinessRun(system.id)!;
      app.store.updateBusinessSystemMetadata(system.id, { graph: { edges: [] } });
      const later = app.store.createBusinessRun(system.id)!;
      expect(app.store.listReadyUnitRuns(earlier.id).map(({ unitId }) => unitId)).toEqual([source]);
      expect(app.store.listReadyUnitRuns(later.id).map(({ unitId }) => unitId).sort()).toEqual([source, target].sort());
    } finally {
      app.stop();
    }
  });

  test("dispatches a join once all predecessors complete in a historical frozen graph", () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:" });
    const { store } = app;
    try {
      const system = store.createBusinessSystem({ name: "DAG fan-in" });
      const operator = (name: string) => `${store.createIntake({ business_system_id: system.id, name }).id}:operator`;
      const root = operator("Root");
      const left = operator("Left");
      const right = operator("Right");
      const join = operator("Join");
      const edges = [
        { source: root, target: left }, { source: root, target: right },
        { source: right, target: join }, { source: left, target: join },
      ];
      store.updateBusinessSystemMetadata(system.id, { graph: { edges } });
      const run = store.createBusinessRun(system.id, "test", {}, ["cas://original"])!;
      const finish = (unitId: string, outputs: string[] = []) => {
        const unit = store.claimUnitRun(run.id, unitId, "fixture-session")!;
        store.completeUnitRun(unit.id, "completed", outputs);
        return unit.id;
      };

      expect(store.listReadyUnitRuns(run.id).map(({ unitId }) => unitId)).toEqual([root]);
      const completedRoot = finish(root, ["cas://root"]);
      store.db.exec("DELETE FROM business_run_edges; ALTER TABLE business_run_graphs DROP COLUMN edges_indexed");
      store.init();
      store.updateBusinessSystemMetadata(system.id, { graph: { edges: [] } });

      const ready = store.listReadyUnitRuns(run.id, completedRoot);
      expect(ready.map(({ unitId }) => unitId).sort()).toEqual([left, right].sort());
      expect(ready.map(({ inputs }) => inputs)).toEqual([["cas://original", "cas://root"], ["cas://original", "cas://root"]]);
      expect(store.db.query("SELECT edges_indexed FROM business_run_graphs WHERE run_id=?").get(run.id)).toEqual({ edges_indexed: 1 });

      const completedLeft = finish(left, ["cas://shared", "cas://left"]);
      expect(store.listReadyUnitRuns(run.id, completedLeft)).toEqual([]);
      const completedRight = finish(right, ["cas://right", "cas://shared", "cas://original"]);
      const joinInputs = ["cas://original", "cas://right", "cas://shared", "cas://left"];
      expect(store.listReadyUnitRuns(run.id, completedRight)).toMatchObject([{ unitId: join, inputs: joinInputs }]);
      expect(store.listReadyUnitRuns(run.id, completedRight)).toMatchObject([{ unitId: join, inputs: joinInputs }]);
      expect(store.claimUnitRun(run.id, join, "join-session")?.inputs).toEqual(joinInputs);
      expect(store.claimUnitRun(run.id, join, "duplicate-session")).toBeUndefined();
      expect(store.listReadyUnitRuns(run.id, completedRight)).toEqual([]);
    } finally { app.stop(); }
  });

  test("ensures the selected container runtime before a containerized Operator starts", async () => {
    const dbPath = ":memory:";
    let ensured = 0;
    let spawned = 0;
    const app = createApp({
      dbPath,
      legacyPath: dbPath,
      runtime: {
        health: async () => ({ available: false, platform: "linux", executable: "kiri", machine: "linux-host", rootless: null }),
        ensureReady: async () => { ensured += 1; return { available: true, platform: "linux", executable: "kiri", machine: "linux-host", rootless: null }; },
      },
      operator: { spawn: () => { spawned += 1; return processForRecord("cas://runtime-ready"); } },
    });
    const headers = { "content-type": "application/json" };
    try {
      const system = await (await app.fetch(new Request("http://localhost/api/v1/business-systems", { method: "POST", headers, body: JSON.stringify({ name: "运行时就绪业务" }) }))).json() as { id: string };
      const project = await (await app.fetch(new Request("http://localhost/api/v1/projects/intake", { method: "POST", headers, body: JSON.stringify({ business_system_id: system.id, name: "容器步骤", repo_url: "https://example.test/runtime.git" }) }))).json() as { id: string };
      app.store.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "https://example.invalid", model: "codex-test", mode: "session", image: `registry.example/harness@sha256:${"a".repeat(64)}` });
      const run = await (await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/runs`, { method: "POST", headers, body: JSON.stringify({}) }))).json() as { id: string };
      for (let attempt = 0; attempt < 30 && app.store.listArtifacts(project.id).length === 0; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 5));
      expect(app.store.listArtifacts(project.id)).toHaveLength(1);
      expect(ensured).toBe(1);
      expect(spawned).toBe(1);
      expect(app.store.getBusinessRun(run.id)?.status).toBe("completed");
    } finally {
      await app.operator.close();
      await app.foreman.close();
      app.store.close();
    }
  });

  test("creates operator and machine units and records a Harness material", async () => {
    const dbPath = `.factory/business-runtime-${randomUUID()}.db`;
    const app = createApp({ dbPath, legacyPath: dbPath, operator: { spawn: () => processForRecord() } });
    const headers = { "content-type": "application/json" };
    try {
      const system = await (await app.fetch(new Request("http://localhost/api/v1/business-systems", { method: "POST", headers, body: JSON.stringify({ name: "闭环业务" }) }))).json() as { id: string };
      const project = await (await app.fetch(new Request("http://localhost/api/v1/projects/intake", { method: "POST", headers, body: JSON.stringify({ business_system_id: system.id, name: "构建项目", repo_url: "https://example.test/repo.git" }) }))).json() as { id: string };
      const units = await (await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/units`))).json() as Array<{ kind: string }>;
      expect(units.map((unit) => unit.kind).sort()).toEqual(["machine", "operator"]);
      app.store.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "https://example.invalid", model: "codex-test", mode: "session" });
      const runResponse = await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/runs`, { method: "POST", headers, body: JSON.stringify({ trigger: "test" }) }));
      expect(runResponse.status).toBe(201);
      const run = await runResponse.json() as { id: string };
      for (let attempt = 0; attempt < 30 && app.store.listArtifacts(project.id).length === 0; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 5));
      const artifacts = app.store.listArtifacts(project.id);
      expect(artifacts).toHaveLength(1);
      expect(artifacts[0].recordType).toBe("material");
      const details = await (await app.fetch(new Request(`http://localhost/api/v1/runs/${run.id}`))).json() as { status: string; units: Array<{ status: string }> };
      expect(details.status).toBe("completed");
      expect(details.units.some((unit) => unit.status === "completed")).toBe(true);
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });

  test("waits for DAG predecessors and forwards their material references", async () => {
    const dbPath = `.factory/business-dag-${randomUUID()}.db`;
    let calls = 0;
    const tasks: string[] = [];
    const app = createApp({ dbPath, legacyPath: dbPath, operator: {
      spawn: (argv) => { tasks.push(argv.join("\n")); return processForRecord(`cas://report-${++calls}`); },
    } });
    const headers = { "content-type": "application/json" };
    try {
      const system = await (await app.fetch(new Request("http://localhost/api/v1/business-systems", { method: "POST", headers, body: JSON.stringify({ name: "DAG 业务" }) }))).json() as { id: string };
      const first = await (await app.fetch(new Request("http://localhost/api/v1/projects/intake", { method: "POST", headers, body: JSON.stringify({ business_system_id: system.id, name: "第一步", repo_url: "https://example.test/one.git" }) }))).json() as { id: string };
      // An older project may already contain credentials in its repository URL.
      const second = app.store.createIntake({ business_system_id: system.id, name: "第二步", repo_url: "https://fixture:repo-password@example.test/two.git?access_token=repo-secret" });
      const firstOperator = `${first.id}:operator`;
      const secondOperator = `${second.id}:operator`;
      const graph = { nodes: [{ id: firstOperator, type: "operator" }, { id: secondOperator, type: "operator" }], edges: [{ source: firstOperator, target: secondOperator }] };
      const graphResponse = await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/graph`, { method: "POST", headers, body: JSON.stringify({ graph }) }));
      expect(graphResponse.status).toBe(200);
      const other = app.store.createBusinessSystem({ name: "Unrelated system" });
      const otherProject = app.store.createIntake({ business_system_id: other.id, name: "Unrelated project" });
      app.store.addArtifact({ projectId: otherProject.id, kind: "private", ref: "cas://input", summary: "外部业务私有内容", metadata: {} });
      app.store.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "https://example.invalid", model: "codex-test", mode: "session" });
      const run = await (await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/runs`, { method: "POST", headers, body: JSON.stringify({ inputs: ["cas://input"] }) }))).json() as { id: string };
      for (let attempt = 0; attempt < 40 && app.store.getBusinessRun(run.id)?.status !== "completed"; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 5));
      expect(calls).toBe(2);
      expect(app.store.getBusinessRun(run.id)?.status).toBe("completed");
      const unitRuns = app.store.listUnitRuns(run.id);
      expect(unitRuns.find((item) => item.unitId === secondOperator)?.inputs).toContain("cas://report-1");
      expect(tasks[0]).toContain("cas://input");
      expect(tasks[0]).not.toContain("外部业务私有内容");
      expect(tasks[1]).toContain('"title":"构建报告"');
      expect(tasks[1]).toContain('"path":"build/report.md"');
      expect(tasks[1]).toContain('"repository":"https://example.test/two.git"');
      expect(tasks[1]).not.toContain("repo-password");
      expect(tasks[1]).not.toContain("repo-secret");
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });

  test("blocks downstream operators when a predecessor fails", async () => {
    const dbPath = `.factory/business-dag-failure-${randomUUID()}.db`;
    const app = createApp({ dbPath, legacyPath: dbPath, operator: { spawn: () => processForFailure() } });
    const headers = { "content-type": "application/json" };
    try {
      const system = await (await app.fetch(new Request("http://localhost/api/v1/business-systems", { method: "POST", headers, body: JSON.stringify({ name: "失败传播业务" }) }))).json() as { id: string };
      const first = await (await app.fetch(new Request("http://localhost/api/v1/projects/intake", { method: "POST", headers, body: JSON.stringify({ business_system_id: system.id, name: "失败步骤", repo_url: "https://example.test/fail.git" }) }))).json() as { id: string };
      const second = await (await app.fetch(new Request("http://localhost/api/v1/projects/intake", { method: "POST", headers, body: JSON.stringify({ business_system_id: system.id, name: "下游步骤", repo_url: "https://example.test/downstream.git" }) }))).json() as { id: string };
      const source = `${first.id}:operator`;
      const target = `${second.id}:operator`;
      const graphResponse = await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/graph`, { method: "POST", headers, body: JSON.stringify({ graph: { nodes: [{ id: source }, { id: target }], edges: [{ source, target }] } }) }));
      expect(graphResponse.status).toBe(200);
      app.store.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "https://example.invalid", model: "codex-test", mode: "session" });
      const run = await (await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/runs`, { method: "POST", headers, body: JSON.stringify({}) }))).json() as { id: string };
      for (let attempt = 0; attempt < 40 && app.store.getBusinessRun(run.id)?.status !== "failed"; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 5));
      expect(app.store.getBusinessRun(run.id)?.status).toBe("failed");
      const unitRuns = app.store.listUnitRuns(run.id);
      expect(unitRuns.find((item) => item.unitId === source)?.status).toBe("failed");
      expect(unitRuns.find((item) => item.unitId === target)?.status).toBe("blocked");
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });
});
