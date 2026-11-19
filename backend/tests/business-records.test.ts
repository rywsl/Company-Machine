import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rm } from "node:fs/promises";
import { createApp } from "../src/server";

async function body(response: Response) { return response.json() as Promise<any>; }
const headers = { "content-type": "application/json" };

describe("business record and DAG boundaries", () => {
  test("lists semantic materials at business-system scope", async () => {
    const dbPath = `.factory/records-${randomUUID()}.db`;
    const app = createApp({ dbPath, legacyPath: dbPath });
    try {
      const system = await body(await app.fetch(new Request("http://localhost/api/v1/business-systems", { method: "POST", headers, body: JSON.stringify({ name: "制作业务" }) })));
      const project = await body(await app.fetch(new Request("http://localhost/api/v1/projects/intake", { method: "POST", headers, body: JSON.stringify({ business_system_id: system.id, name: "项目", local_folder: "C:/work" }) })));
      const created = await body(await app.fetch(new Request(`http://localhost/api/v1/projects/${project.id}/artifacts`, { method: "POST", headers, body: JSON.stringify({ record_type: "material", kind: "script", ref: "cas://script-1", title: "分镜脚本", description: "给视频 Operator 使用的分镜脚本", summary: "已审核脚本", files: [{ ref: "cas://file-1", path: "storyboard.md" }] }) })));
      expect(created.recordType).toBe("material");
      expect(created.title).toBe("分镜脚本");
      const materials = await body(await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/materials`)));
      expect(materials).toHaveLength(1);
      expect(materials[0].files[0].path).toBe("storyboard.md");
      const conversion = await body(await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/conversions`, { method: "POST", headers, body: JSON.stringify({ kind: "review-to-material", summary: "审核脚本转换为可执行物料", inputs: [created.ref], outputs: ["cas://approved-script"] }) })));
      expect(conversion.status).toBe("planned");
      const exported = await body(await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/export`)));
      expect(exported.schema).toBe("mao.business-system/1");
      expect(exported.conversions).toHaveLength(1);
      expect(JSON.stringify(exported)).not.toContain("C:/work");
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });

  test("rejects cyclic or dangling Foreman DAG snapshots", async () => {
    const dbPath = `.factory/graph-${randomUUID()}.db`;
    const app = createApp({ dbPath, legacyPath: dbPath });
    try {
      const system = await body(await app.fetch(new Request("http://localhost/api/v1/business-systems", { method: "POST", headers, body: JSON.stringify({ name: "DAG" }) })));
      const cyclic = { nodes: [{ id: "a" }, { id: "b" }], edges: [{ source: "a", target: "b" }, { source: "b", target: "a" }] };
      expect((await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/graph`, { method: "POST", headers, body: JSON.stringify({ graph: cyclic }) }))).status).toBe(400);
      const dangling = { nodes: [{ id: "a" }], edges: [{ source: "a", target: "missing" }] };
      expect((await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/graph`, { method: "POST", headers, body: JSON.stringify({ graph: dangling }) }))).status).toBe(400);
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });
});
