import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rm } from "node:fs/promises";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createApp } from "../src/server";
import type { OfficialProcess } from "../src/harness";
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
      const second = await (await app.fetch(new Request("http://localhost/api/v1/projects/intake", { method: "POST", headers, body: JSON.stringify({ business_system_id: system.id, name: "第二步", repo_url: "https://example.test/two.git" }) }))).json() as { id: string };
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
