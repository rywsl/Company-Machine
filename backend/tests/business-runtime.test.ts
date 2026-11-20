import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rm } from "node:fs/promises";
import { createApp } from "../src/server";
import type { OfficialProcess } from "../src/harness";

function processForRecord(ref = "cas://report-1"): OfficialProcess {
  const record = JSON.stringify({ type: "mao.record", record_type: "material", kind: "build.report", ref, title: "构建报告", summary: "项目已完成构建", files: [{ ref: `${ref}:file`, path: "build/report.md" }] });
  const body = new TextEncoder().encode(`${JSON.stringify({ type: "assistant", text: "开始构建" })}\n${record}\n${JSON.stringify({ type: "result", text: "完成" })}\n`);
  return { stdout: new ReadableStream({ start(controller) { controller.enqueue(body); controller.close(); } }), kill() {} };
}

function processForFailure(): OfficialProcess {
  const body = new TextEncoder().encode(`${JSON.stringify({ type: "error", text: "构建失败" })}\n`);
  return { stdout: new ReadableStream({ start(controller) { controller.enqueue(body); controller.close(); } }), kill() {} };
}

describe("Bun business execution loop", () => {
  test("ensures the selected container runtime before a containerized Operator starts", async () => {
    const dbPath = `.factory/business-runtime-ready-${randomUUID()}.db`;
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
      app.stop();
      await rm(dbPath, { force: true });
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
    const app = createApp({ dbPath, legacyPath: dbPath, operator: { spawn: () => processForRecord(`cas://report-${++calls}`) } });
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
      app.store.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "https://example.invalid", model: "codex-test", mode: "session" });
      const run = await (await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/runs`, { method: "POST", headers, body: JSON.stringify({ inputs: ["cas://input"] }) }))).json() as { id: string };
      for (let attempt = 0; attempt < 40 && app.store.getBusinessRun(run.id)?.status !== "completed"; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 5));
      expect(calls).toBe(2);
      expect(app.store.getBusinessRun(run.id)?.status).toBe("completed");
      const unitRuns = app.store.listUnitRuns(run.id);
      expect(unitRuns.find((item) => item.unitId === secondOperator)?.inputs).toContain("cas://report-1");
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
