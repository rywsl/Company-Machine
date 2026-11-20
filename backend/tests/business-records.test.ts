import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rm } from "node:fs/promises";
import { createApp } from "../src/server";
import { VaultStore } from "../src/vault";

async function body(response: Response) { return response.json() as Promise<any>; }
const headers = { "content-type": "application/json" };

describe("business record and DAG boundaries", () => {
  test("imports an executable DAG and remaps portable project, unit, source and record references", async () => {
    const vaultPath = ".factory/export-test-" + randomUUID();
    const vault = new VaultStore(vaultPath + ".json", vaultPath + ".key");
    const source = createApp({ dbPath: ":memory:", legacyPath: ":memory:", vault });
    const destination = createApp({ dbPath: ":memory:", legacyPath: ":memory:", vault });
    try {
      const { store } = source;
      const system = store.createBusinessSystem({
        name: "Portable factory", metadata: { workspaceRoot: "/srv/private/work", OPENAI_API_KEY: "unregistered-key" },
      });
      const first = store.createIntake({
        business_system_id: system.id, name: "Producer", repo_url: "https://reader:repository-password@example.test/team/repo.git?token=repository-token",
        portable_sources: [{ kind: "uploaded_file", ref: "cas://source-fixture", metadata: { revision: "v1" } }],
        metadata: { operator_runtime: "claude-code", operator_model: "test-model" },
      });
      const second = store.createIntake({ business_system_id: system.id, name: "Consumer", local_folder: "D:\\private\\checkout" });
      const firstOperator = `${first.id}:operator`;
      const secondOperator = `${second.id}:operator`;
      const graph = {
        id: system.id,
        nodes: [{ id: firstOperator, group: first.id }, { id: secondOperator, group: second.id }],
        edges: [{ id: "produce-consume", source: firstOperator, target: secondOperator, metadata: { sourceProject: first.id } }],
      };
      store.updateBusinessSystemMetadata(system.id, { graph });
      const secret = store.createSecretRequest({
        projectId: first.id, name: "CLIP_TOKEN", vaultRef: "vault://secret/old",
        metadata: { scope: "transcription", password: "plain-secret", purpose: "Generate clip" },
      });
      vault.put(secret.vaultRef, "plain-secret");
      store.markSecretAvailable(secret.id);
      const raw = store.addArtifact({
        projectId: first.id, sourceId: first.sources[1].id, kind: "script", recordType: "material", ref: "cas://script",
        title: "Script", summary: "A script for plain-secret",
        producer: { operator: firstOperator, machine: first.id + ":machine" },
        files: [{ ref: "cas://script-file", path: "storyboard.md" }, { ref: "file:///srv/private/work/storyboard.md" }],
        metadata: { run_id: "old-run", path: "/srv/private/work/storyboard.md" },
      });
      expect(store.listArtifacts(first.id)[0]).toMatchObject({
        recordType: "material", producer: { operator: firstOperator }, files: [{ ref: "cas://script-file" }, { ref: "file:///srv/private/work/storyboard.md" }],
      });
      const approved = store.addArtifact({
        projectId: second.id, kind: "approved-script", recordType: "artifact", ref: "D:\\private\\checkout\\approved.md",
        summary: "Approved", lineage: ["artifact://" + raw.id], inputs: [raw.id], metadata: {},
      });
      store.createBusinessConversion({
        businessSystemId: system.id, kind: "review", summary: "Approve the script", status: "completed",
        inputs: ["artifact://" + raw.id], outputs: [approved.id], producer: { operator: secondOperator }, metadata: {},
      });
      const oldRun = store.createBusinessRun(system.id)!;
      store.claimUnitRun(oldRun.id, firstOperator, "host-harness-session");

      const exported = await source.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/export`));
      expect(exported.status).toBe(200);
      const document = await body(exported);
      const serialized = JSON.stringify(document);
      for (const privateValue of ["plain-secret", "unregistered-key", "host-harness-session", "repository-password", "repository-token", "/srv/private", "private\\\\checkout", "vault://secret/old"]) {
        expect(serialized).not.toContain(privateValue);
      }
      const repository = document.projects.find((item: any) => item.id === first.id).sources.find((item: any) => item.kind === "repo_url");
      expect(repository.ref).toBe("https://example.test/team/repo.git");
      expect(document.records.find((item: any) => item.kind === "script")).toMatchObject({
        recordType: "material", producer: { operator: firstOperator }, files: [{ ref: "cas://script-file", path: "storyboard.md" }],
      });
      const imported = await destination.fetch(new Request("http://localhost/api/v1/business-systems/import", {
        method: "POST", headers, body: serialized,
      }));
      expect(imported.status).toBe(201);
      const migrated = await body(imported);
      const producerProject = migrated.projects.find((item: any) => item.name === "Producer");
      const consumerProject = migrated.projects.find((item: any) => item.name === "Consumer");
      const producerUnit = migrated.units.find((item: any) => item.projectId === producerProject.id && item.kind === "operator");
      const consumerUnit = migrated.units.find((item: any) => item.projectId === consumerProject.id && item.kind === "operator");
      expect(producerProject.id).not.toBe(first.id);
      expect(producerUnit.id).not.toBe(firstOperator);
      expect(producerUnit).toMatchObject({ harnessRuntime: "claude-code", model: "test-model" });
      expect(migrated.graph).toMatchObject({
        id: migrated.system.id, nodes: [{ id: producerUnit.id, group: producerProject.id }, { id: consumerUnit.id, group: consumerProject.id }],
        edges: [{ source: producerUnit.id, target: consumerUnit.id, metadata: { sourceProject: producerProject.id } }],
      });
      const copiedRaw = migrated.records.find((item: any) => item.kind === "script");
      const copiedApproved = migrated.records.find((item: any) => item.kind === "approved-script");
      expect(copiedRaw).toMatchObject({
        sourceId: producerProject.sources.find((item: any) => item.ref === "cas://source-fixture").id,
        ref: "cas://script", files: [{ ref: "cas://script-file", path: "storyboard.md" }],
        producer: { operator: producerUnit.id },
        metadata: { origin: { run_id: "old-run" } },
      });
      expect(copiedApproved).toMatchObject({ ref: "artifact://" + copiedApproved.id, lineage: ["artifact://" + copiedRaw.id], inputs: [copiedRaw.id] });
      expect(migrated.conversions[0]).toMatchObject({
        inputs: ["artifact://" + copiedRaw.id], outputs: [copiedApproved.id], producer: { operator: consumerUnit.id },
      });
      expect(producerProject.secretRequests).toMatchObject([{ name: "CLIP_TOKEN", status: "requested", metadata: { scope: "transcription" } }]);
      const newSecret = destination.store.listSecretRequests()[0];
      expect(newSecret.vaultRef).not.toBe(secret.vaultRef);
      expect(vault.has(newSecret.vaultRef)).toBe(false);
      expect(migrated.runs).toHaveLength(1);
      expect(migrated.runs[0]).toMatchObject({ id: oldRun.id, businessSystemId: migrated.system.id });
      expect(migrated.runs[0].units.some((unit: any) => unit.unitId === producerUnit.id)).toBe(true);
      expect(destination.store.listBusinessRuns(migrated.system.id)).toHaveLength(0);
      const run = destination.store.createBusinessRun(migrated.system.id)!;
      const [ready] = destination.store.listReadyUnitRuns(run.id);
      expect(ready.unitId).toBe(producerUnit.id);
      expect(destination.store.listReadyUnitRuns(run.id)).toHaveLength(1);
      destination.store.completeUnitRun(ready.id, "completed", [copiedRaw.ref]);
      expect(destination.store.listReadyUnitRuns(run.id)).toMatchObject([{ unitId: consumerUnit.id, inputs: [copiedRaw.ref] }]);
    } finally {
      source.stop();
      destination.stop();
      await Promise.all([rm(vaultPath + ".json", { force: true }), rm(vaultPath + ".key", { force: true })]);
    }
  });

  test("lists semantic materials at business-system scope", async () => {
    const dbPath = `.factory/records-${randomUUID()}.db`;
    const app = createApp({ dbPath, legacyPath: dbPath });
    try {
      const system = await body(await app.fetch(new Request("http://localhost/api/v1/business-systems", { method: "POST", headers, body: JSON.stringify({ name: "制作业务" }) })));
      const project = await body(await app.fetch(new Request("http://localhost/api/v1/projects/intake", { method: "POST", headers, body: JSON.stringify({ business_system_id: system.id, name: "项目", local_folder: "C:/work" }) })));
      expect(JSON.stringify(project)).not.toContain("C:/work");
      const created = await body(await app.fetch(new Request(`http://localhost/api/v1/projects/${project.id}/artifacts`, { method: "POST", headers, body: JSON.stringify({ record_type: "material", kind: "script", ref: "cas://script-1", title: "分镜脚本", description: "给视频 Operator 使用的分镜脚本", summary: "已审核脚本", files: [{ ref: "cas://file-1", path: "storyboard.md" }] }) })));
      expect(created.recordType).toBe("material");
      expect(created.title).toBe("分镜脚本");
      const webAssets = await body(await app.fetch(new Request("http://localhost/api/web/assets")));
      expect(webAssets.some((asset: any) => asset.id === created.id && asset.record_type === "material")).toBe(true);
      const materials = await body(await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/materials`)));
      expect(materials).toHaveLength(1);
      expect(materials[0].files[0].path).toBe("storyboard.md");
      const conversion = await body(await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/conversions`, { method: "POST", headers, body: JSON.stringify({ kind: "review-to-material", summary: "审核脚本转换为可执行物料", inputs: [created.ref], outputs: ["cas://approved-script"] }) })));
      expect(conversion.status).toBe("planned");
      const exported = await body(await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/export`)));
      expect(exported.schema).toBe("mao.business-system/1");
      expect(exported.conversions).toHaveLength(1);
      expect(JSON.stringify(exported)).not.toContain("C:/work");
      const imported = await body(await app.fetch(new Request("http://localhost/api/v1/business-systems/import", { method: "POST", headers, body: JSON.stringify(exported) })));
      expect(imported.schema).toBe("mao.business-system/1");
      expect(imported.system.name).toBe("制作业务");
      expect(imported.records).toHaveLength(1);
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

      const imported = await app.fetch(new Request("http://localhost/api/v1/business-systems/import", {
        method: "POST",
        headers,
        body: JSON.stringify({ schema: "mao.business-system/1", system: { name: "坏图" }, graph: cyclic }),
      }));
      expect(imported.status).toBe(400);
      expect((await body(await app.fetch(new Request("http://localhost/api/v1/business-systems"))))).toHaveLength(1);
      const partial = await app.fetch(new Request("http://localhost/api/v1/business-systems/import", {
        method: "POST", headers,
        body: JSON.stringify({
          schema: "mao.business-system/1", system: { name: "Invalid record" },
          projects: [{ id: "old-project", name: "Valid project" }],
          records: [{ kind: "material", projectId: "missing-project" }],
        }),
      }));
      expect(partial.status).toBe(400);
      expect(app.store.listBusinessSystems()).toHaveLength(1);
      expect(app.store.listProjects()).toHaveLength(0);
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });

  test("accepts a long chain and detects a cycle without recursive graph traversal", async () => {
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:" });
    try {
      const system = app.store.createBusinessSystem({ name: "Long chain" });
      const nodes = Array.from({ length: 16_000 }, (_, index) => ({ id: "node-" + index }));
      const edges = nodes.slice(1).map(({ id }, index) => ({ source: nodes[index].id, target: id }));
      const submit = () => app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/graph`, {
        method: "POST", headers, body: JSON.stringify({ graph: { nodes, edges } }),
      }));
      expect((await submit()).status).toBe(200);
      edges.push({ source: nodes[nodes.length - 1].id, target: nodes[0].id });
      expect((await submit()).status).toBe(400);
    } finally { app.stop(); }
  });
});
