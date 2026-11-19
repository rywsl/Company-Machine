import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rm } from "node:fs/promises";
import { createApp } from "../src/server";
import type { OfficialProcess } from "../src/harness";

function processForRecord(): OfficialProcess {
  const record = JSON.stringify({ type: "mao.record", record_type: "material", kind: "build.report", ref: "cas://report-1", title: "构建报告", summary: "项目已完成构建", files: [{ ref: "cas://report-file", path: "build/report.md" }] });
  const body = new TextEncoder().encode(`${JSON.stringify({ type: "assistant", text: "开始构建" })}\n${record}\n${JSON.stringify({ type: "result", text: "完成" })}\n`);
  return { stdout: new ReadableStream({ start(controller) { controller.enqueue(body); controller.close(); } }), kill() {} };
}

describe("Bun business execution loop", () => {
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
});
