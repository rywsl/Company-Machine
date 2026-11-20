import { expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { SqliteStore } from "../src/db";
import { startServer } from "../src/server";
import { createUnitRunOwner, unitRunProcessScope } from "../src/unit-run-owner";

test("fences Operator outcomes and official session updates to the active claim", () => {
  const store = new SqliteStore(":memory:", ":memory:");
  try {
    const system = store.createBusinessSystem({ name: "Owner test" });
    const project = store.createIntake({ business_system_id: system.id, name: "Project" });
    const unitId = `${project.id}:operator`;
    const run = store.createBusinessRun(system.id)!;
    const owner = createUnitRunOwner();
    const unit = store.claimUnitRun(run.id, unitId, "session", owner)!;
    const stranger = createUnitRunOwner();

    store.noteUnitSession(unit.id, "wrong session", stranger.token);
    expect(store.getUnitRun(run.id, unit.id)?.sessionId).toBe("session");
    expect(store.completeUnitRun(unit.id, "completed")).toBeUndefined();
    expect(store.completeUnitRun(unit.id, "completed", [], undefined, stranger.token)).toBeUndefined();
    expect(store.interruptUnitRun(unit.id, stranger.token)).toBeUndefined();
    store.noteUnitSession(unit.id, "official session", owner.token);
    expect(store.completeUnitRun(unit.id, "completed", ["material://result"], undefined, owner.token)).toMatchObject({
      status: "completed", sessionId: "official session", outputs: ["material://result"],
    });
    expect(store.completeUnitRun(unit.id, "failed", [], "late failure", owner.token)).toBeUndefined();
  } finally { store.close(); }
});

test("startup settles only Operator claims whose local owner has exited", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-operator-recovery-"));
  const dbPath = join(root, "factory.db");
  const seed = new SqliteStore(dbPath, dbPath);
  let app: ReturnType<typeof startServer> | undefined;
  try {
    seed.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "", model: "fixture", mode: "session" });
    const system = seed.createBusinessSystem({ name: "Recovery DAG" });
    const source = seed.createIntake({ business_system_id: system.id, name: "Interrupted branch" });
    const target = seed.createIntake({ business_system_id: system.id, name: "Dependent branch" });
    const independent = seed.createIntake({ business_system_id: system.id, name: "Independent branch" });
    const sourceId = `${source.id}:operator`, targetId = `${target.id}:operator`, independentId = `${independent.id}:operator`;
    seed.updateBusinessSystemMetadata(system.id, { graph: { edges: [{ source: sourceId, target: targetId }] } });

    const dead = createUnitRunOwner();
    const run = seed.createBusinessRun(system.id, "test", {}, [], true)!;
    const interrupted = seed.claimUnitRun(run.id, sourceId, "session-dead", { ...dead, pid: 2147483647 })!;
    seed.db.query("UPDATE unit_runs SET outputs=? WHERE id=?").run(JSON.stringify(["material://retained"]), interrupted.id);
    const retained: string[] = [];
    for (const owner of [createUnitRunOwner(), { ...createUnitRunOwner(), pid: 2147483647, scope: "foreign-scope" }, undefined]) {
      const other = seed.createBusinessRun(system.id, "test", {}, [], true)!;
      retained.push(seed.claimUnitRun(other.id, sourceId, "session-retained", owner)!.id);
    }
    seed.close();

    const launches: string[] = [];
    app = startServer({
      port: 0, dbPath, legacyPath: dbPath,
      operator: { spawn: (argv) => {
        launches.push(argv.at(-1) ?? "");
        const body = new TextEncoder().encode(JSON.stringify({ type: "result", text: "Queued branch completed" }) + "\n");
        return { stdout: new ReadableStream({ start(controller) { controller.enqueue(body); controller.close(); } }), kill() {} };
      } },
    });
    const store = app.store;
    if (!unitRunProcessScope) {
      expect(store.getUnitRun(run.id, interrupted.id)).toMatchObject({ status: "running", sessionId: "session-dead" });
      expect(store.listReadyUnitRuns(run.id).map(({ unitId }) => unitId)).toEqual([independentId]);
      expect(app.events.after().filter(({ type }) => type === "operator.run.failed")).toEqual([]);
      expect(launches).toEqual([]);
      return;
    }
    const deadline = Date.now() + 2000;
    while (store.listUnitRuns(run.id).find(({ unitId }) => unitId === independentId)?.status !== "completed" && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    expect(launches).toHaveLength(1);
    const statuses = new Map(store.listUnitRuns(run.id).map((unit) => [unit.unitId, unit]));
    expect(statuses.get(sourceId)).toMatchObject({ status: "failed", error: "CONTROL_PLANE_RESTART", sessionId: "session-dead", outputs: ["material://retained"] });
    expect(statuses.get(targetId)).toMatchObject({ status: "blocked", error: "predecessor-failed" });
    expect(statuses.get(independentId)?.status).toBe("completed");
    expect(store.getBusinessRun(run.id)?.status).toBe("failed");
    for (const id of retained) expect(store.db.query("SELECT status FROM unit_runs WHERE id=?").get(id)).toEqual({ status: "running" });
    app.operator.reconcileInterruptedRuns();
    const failures = app.events.after().filter(({ type }) => type === "operator.run.failed");
    expect(failures.map(({ data }) => data)).toMatchObject([{ id: interrupted.id, error: "CONTROL_PLANE_RESTART" }]);
  } finally {
    if (app) { await app.operator.close(); app.stop(); }
    seed.close();
    await rm(root, { recursive: true, force: true });
  }
});
