import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { randomUUID } from "node:crypto";
import { rmSync } from "node:fs";
import { join } from "node:path";
import { Context } from "../src/legacy/context";
import { FactoryError } from "../src/legacy/errors";
import { CounterIds, FakeClock } from "../src/legacy/foundation";
import { JobStore } from "../src/legacy/jobs";
import { compileRunPlan } from "../src/legacy/runplan";
import { RunStore } from "../src/legacy/runs";
import { loadSnapshot } from "../src/legacy/snapshot";

function expectCode(run: () => unknown, code: string): void {
  try {
    run();
  } catch (error) {
    if (!(error instanceof FactoryError)) throw error;
    expect(error.code).toBe(code);
    return;
  }
  throw new Error("Expected " + code);
}

describe("Bun RunStore", () => {
  test("completes a cache hit with original Job history, zero cost and one event", async () => {
    const db = new Database(":memory:");
    const ids = new CounterIds();
    const clock = new FakeClock();
    try {
      const snapshot = await loadSnapshot("tests/fixtures/registry-m2");
      const plan = compileRunPlan(snapshot, "note-two@1");
      const store = new RunStore(db, clock, ids);
      const jobs = new JobStore(db, clock, ids);
      const run = store.create(plan, { inputs: { note: "bean-1", task: "task-1" }, budgetCapUsd: 1 });
      const first = plan.nodes[0];
      store.transition(run.id, "RUNNING");
      const job = jobs.create({ station: first.station, epoch: plan.epoch, inputs: {}, params: {} });
      const options = { params: { form: "reuse" } };
      expectCode(() => store.completeCachedNode(run.id, first.id, options, job), "RUN_STATE_INVALID");
      const sealed = jobs.transition(job.id, "SEALED", { outputs: { note: ["bean-2"] }, costUsd: 0.2 });
      const match = store.completeCachedNode(run.id, first.id, options, sealed);
      expect(match).toMatchObject({
        state: "DONE", cacheHit: true, jobId: job.id, outputs: { note: ["bean-2"] }, params: options.params, reservedUsd: 0,
      });
      expect(store.completeCachedNode(run.id, first.id, options, sealed)).toEqual(match);
      expect(store.get(run.id)).toMatchObject({ spentUsd: 0, reservedUsd: 0 });
      expect(store.jobHistory(run.id, first.id)).toEqual([job.id]);
      expect(jobs.get(job.id)).toEqual(sealed);
      expect(store.events(run.id).map(({ kind, data }) => ({ kind, data })).slice(2)).toEqual([{
        kind: "run.node", data: { node: first.id, from: "WAITING", to: "DONE", job: job.id, cache_hit: true },
      }]);
    } finally { db.close(); }
  });

  test("recovers the stored RunPlan and settled job after opening the database again", async () => {
    const root = join("tmp", "legacy-run-" + randomUUID());
    const ids = new CounterIds();
    const clock = new FakeClock();
    const snapshot = await loadSnapshot("tests/fixtures/registry-m2");
    const plan = compileRunPlan(snapshot, "note-two@1");
    let context = Context.open(root, { ids, clock });
    try {
      let store = new RunStore(context.db, clock, ids);
      const run = store.create(plan, { inputs: { note: "bean-1", task: "task-1" }, budgetCapUsd: 1 });
      expect(run.planDigest).toBe(plan.digest());
      expect(store.nodes(run.id).map((node) => node.nodeId)).toEqual(plan.nodes.map((node) => node.id));
      const event = store.events(run.id)[0];
      store.transition(run.id, "RUNNING", { reason: "start" });
      const jobs = new JobStore(context.db, clock, ids);
      const first = plan.nodes[0];
      const node = store.submitNode(run.id, first.id, { params: { topic: "Bun" }, reservedUsd: 0.1 }, () =>
        jobs.create({ station: first.station, epoch: plan.epoch, inputs: { note: ["bean-1"] }, params: {} }));
      expect(store.get(run.id).reservedUsd).toBe(0.1);

      context.close();
      context = Context.open(root, { ids, clock });
      store = new RunStore(context.db, clock, ids);
      expect(store.plan(run.id).toJSON()).toEqual(plan.toJSON());
      expect(store.list("RUNNING").map((item) => item.id)).toEqual([run.id]);
      expect(store.node(run.id, first.id)).toMatchObject({ state: "SUBMITTED", jobId: node.jobId, params: { topic: "Bun" } });
      const resumedJobs = new JobStore(context.db, clock, ids);
      if (!node.jobId) throw new Error("Expected submitted job");
      const job = resumedJobs.transition(node.jobId, "SEALED", { costUsd: 0.025, outputs: { note: ["bean-2"] } });
      expect(store.settleNode(run.id, first.id, job).outputs).toEqual({ note: ["bean-2"] });
      store.settleNode(run.id, first.id, job);
      expect(store.get(run.id)).toMatchObject({ spentUsd: 0.025, reservedUsd: 0 });
      expect(store.jobHistory(run.id, first.id)).toEqual([job.id]);
      expect(store.events(run.id, event.seq).map((entry) => entry.kind)).toEqual([
        "run.state", "run.node", "run.budget", "run.node", "run.budget",
      ]);
      context.db.query("UPDATE runs SET plan = plan || ' ' WHERE id = ?").run(run.id);
      expectCode(() => store.plan(run.id), "RUN_PLAN_INVALID");
    } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 4, retryDelay: 100 }); }
  });

  test("rolls back job creation and budget when submission fails, and persists retry and cancel", async () => {
    const clock = new FakeClock();
    const ids = new CounterIds();
    const snapshot = await loadSnapshot("tests/fixtures/registry-m2");
    const plan = compileRunPlan(snapshot, "note-two@1");
    const db = new Database(":memory:");
    try {
      const store = new RunStore(db, clock, ids);
      const jobs = new JobStore(db, clock, ids);
      const run = store.create(plan, { inputs: { note: "bean-1", task: "task-1" }, budgetCapUsd: 1 });
      const first = plan.nodes[0];
      store.transition(run.id, "RUNNING");
      const createJob = () => jobs.create({ station: first.station, epoch: plan.epoch, inputs: {}, params: {} });
      expectCode(() => store.submitNode(run.id, first.id, { params: {}, reservedUsd: 2 }, createJob), "RUN_BUDGET_EXHAUSTED");
      let rolledBackJobId = "";
      expect(() => store.submitNode(run.id, first.id, { params: {}, reservedUsd: 0.3 }, () => {
        rolledBackJobId = createJob().id;
        throw new Error("test failure");
      })).toThrow("test failure");
      expectCode(() => jobs.get(rolledBackJobId), "JOB_NOT_FOUND");
      expect(store.get(run.id).reservedUsd).toBe(0);
      expect(store.node(run.id, first.id).state).toBe("WAITING");

      const submitted = store.submitNode(run.id, first.id, { params: {}, reservedUsd: 0.3 }, createJob);
      if (!submitted.jobId) throw new Error("Expected submitted job");
      const failed = jobs.transition(submitted.jobId, "DEAD", { error: { code: "GATE_REJECTED" }, costUsd: 0.1 });
      store.settleNode(run.id, first.id, failed);
      store.transition(run.id, "FAILING");
      store.transition(run.id, "FAILED");
      expect(store.nodes(run.id).map((node) => node.state)).toEqual(["FAILED", "SKIPPED"]);
      expect(store.retryNode(run.id, first.id, "operator").state).toBe("RUNNING");
      expect(store.nodes(run.id).map((node) => node.state)).toEqual(["WAITING", "WAITING"]);
      expect(store.jobHistory(run.id, first.id)).toEqual([failed.id]);
      store.cancel(run.id, "operator");
      expect(store.transition(run.id, "CANCELLED")).toMatchObject({ spentUsd: 0.1, reservedUsd: 0, state: "CANCELLED" });
      expect(store.nodes(run.id).every((node) => node.state === "SKIPPED")).toBe(true);
    } finally { db.close(); }
  });
});
