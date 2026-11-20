import { expect, test } from "bun:test";
import { rm } from "node:fs/promises";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createApp } from "../src/server";
import { LocalProvider, Warehouse } from "../src/legacy";

async function waitForTerminal(app: ReturnType<typeof createApp>, runId: string): Promise<void> {
  for (let attempt = 0; attempt < 160; attempt += 1) {
    const status = app.store.getBusinessRun(runId)?.status;
    if (status === "completed" || status === "failed") return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error(`business run ${runId} did not reach a terminal state`);
}

test("bridges Business Material into a sealed legacy Bean and keeps the conversion idempotent", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-material-kernel-"));
  const provider = new LocalProvider();
  const app = createApp({
    storageRoot: root,
    dbPath: join(root, "business.db"),
    legacyPath: join(root, "business.db"),
    runtime: provider,
  });
  try {
    const system = app.store.createBusinessSystem({ name: "Material kernel" });
    const project = app.store.createIntake({ business_system_id: system.id, name: "Kernel project", skip_default_units: true });
    const warehouse = new Warehouse(root, app.store.db, app.store.clock);
    const payload = JSON.stringify({ steps: 2 });
    const source = join(root, "task.json");
    await Bun.write(source, payload);
    const digest = await warehouse.putFile(source);
    const artifact = app.store.addArtifact({
      projectId: project.id,
      recordType: "material",
      kind: "agent.task@1.0",
      ref: "material://kernel-task",
      summary: "Kernel input task",
      files: [{ ref: `cas://${digest}`, path: "task.json", mediaType: "application/json", digest }],
      metadata: { legacy_type: "agent.task@1.0", legacy_facts: { steps: 2 } },
    });
    app.store.registerArtifactFiles(artifact.id, [{ position: 0, digest, size: new TextEncoder().encode(payload).byteLength }]);
    app.store.updateBusinessSystemMetadata(system.id, {
      kernel: {
        registry_root: resolve("tests/fixtures/registry-m2"),
        line: "note-pipeline@1",
        factory_root: root,
        inputs: { task: `material://${artifact.id}` },
      },
    });
    app.store.createBusinessUnit({
      id: `${project.id}:machine`, businessSystemId: system.id, projectId: project.id,
      kind: "machine", name: "Kernel machine", status: "ready", machineRef: "studio@1", metadata: {},
    });
    const response = await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/runs`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({}),
    }));
    expect(response.status).toBe(201);
    const run = await response.json() as { id: string };
    await waitForTerminal(app, run.id);
    const converted = app.store.listArtifacts(project.id).find(({ id }) => id === artifact.id)!;
    const firstBeanId = converted.metadata.legacy_bean_id;
    expect(typeof firstBeanId).toBe("string");
    expect(app.store.listBusinessConversions(system.id).filter(({ kind }) => kind === "material-to-legacy-bean")).toHaveLength(1);

    const retried = app.store.retryBusinessRun(run.id)!;
    expect(retried.status).toBe("queued");
    app.recoverQueuedWork();
    await waitForTerminal(app, run.id);
    const afterRetry = app.store.listArtifacts(project.id).find(({ id }) => id === artifact.id)!;
    expect(afterRetry.metadata.legacy_bean_id).toBe(firstBeanId);
    expect(app.store.listBusinessConversions(system.id).filter(({ kind }) => kind === "material-to-legacy-bean")).toHaveLength(1);
  } finally {
    app.stop();
    await rm(root, { recursive: true, force: true, maxRetries: 4, retryDelay: 50 });
  }
});
