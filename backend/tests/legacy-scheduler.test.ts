import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rmSync } from "node:fs";
import { join } from "node:path";
import { Context } from "../src/legacy/context";
import { CounterIds, FakeClock } from "../src/legacy/foundation";
import { Runner } from "../src/legacy/runner";
import { LocalProvider } from "../src/legacy/sandbox";
import { ScheduledExecutor, Scheduler } from "../src/legacy/scheduler";
import { loadSnapshot } from "../src/legacy/snapshot";

const pools = { cpu: 1, memory_mb: 256, gpu: 0 };

async function fixture() {
  const root = join("tmp", "legacy-scheduler-" + randomUUID());
  const snapshot = await loadSnapshot("tests/fixtures/registry-m2");
  const clock = new FakeClock();
  const context = Context.open(root, { clock, ids: new CounterIds() });
  const runner = new Runner(context, snapshot, new LocalProvider());
  const job = (inputs: Record<string, string[]> = {}) =>
    runner.jobs.create({ station: "Station:upper@1", epoch: snapshot.epoch, inputs, params: {} });
  return {
    context, snapshot, clock, runner, job,
    close() { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 75 }); },
  };
}

describe("Bun scheduler", () => {
  test("persists the queue, reserves resources and dispatches jobs in ID order", async () => {
    const { context, snapshot, runner, job, close } = await fixture();
    let unblock: () => void = () => {};
    try {
      const first = job();
      const second = job();
      const scheduler = new Scheduler(context, snapshot, runner, { config: { pools } });
      const executor = new ScheduledExecutor(scheduler);
      executor.dispatch([second.id, first.id, second.id]);
      expect(scheduler.queued()).toEqual([first.id, second.id]);
      expect(runner.jobs.get(first.id).state).toBe("PENDING");
      expect(scheduler.demand("upper@1")).toEqual({ cpu: 1, memoryMb: 256, gpu: 0, pool: "*" });

      const gate = new Promise<void>((resolve) => { unblock = resolve; });
      runner.run = async (id) => {
        if (id === first.id) await gate;
        return runner.jobs.transition(id, "SEALED");
      };
      const pumping = scheduler.pump();
      const resumed = new Scheduler(context, snapshot, runner, { config: { pools } });
      expect(await resumed.pump()).toEqual([]);
      unblock();
      expect(await pumping).toEqual([first.id]);
      expect(await resumed.pump()).toEqual([second.id]);
      expect(resumed.queued()).toEqual([]);
    } finally { unblock(); close(); }
  });

  test("queues jobs blocked by capacity or quarantine and stores redacted dead letters", async () => {
    const { context, snapshot, runner, job, close } = await fixture();
    try {
      const bean = await context.warehouse.add({ type: "note.text@1.0", files: {}, meta: {} });
      const pending = job({ note: [bean.id] });
      const scheduler = new Scheduler(context, snapshot, runner, { config: { pools: { cpu: null } } });
      scheduler.enqueue([pending.id]);
      runner.run = async (id) => runner.jobs.transition(id, "DEAD", {
        error: { code: "OPERATOR_FAILED", message: "token=fake", detail: { password: "fake" } },
      });
      expect(await scheduler.pump()).toEqual([]);
      expect(scheduler.queued()).toEqual([pending.id]);
      scheduler.configure({ pools });
      scheduler.quarantine(bean.id, { by: "operator", reason: "manual review" });
      expect(await scheduler.pump()).toEqual([]);
      scheduler.release(bean.id, { by: "operator" });
      const tick = await scheduler.tick();
      expect(tick.pumped).toEqual([pending.id]);
      expect(tick.dead).toEqual([pending.id]);
      expect(scheduler.queued()).toEqual([]);
      expect(scheduler.dead(pending.id)).toMatchObject({
        errorCode: "OPERATOR_FAILED", error: { message: "token=[REDACTED]", detail: { password: "[REDACTED]" } },
      });
      expect((await scheduler.tick()).dead).toEqual([]);
    } finally { close(); }
  });

  test("applies a common run cap across Bean and cron triggers", async () => {
    const { context, snapshot, runner, close } = await fixture();
    try {
      const bean = await context.warehouse.add({ type: "note.text@1.0", files: {}, meta: {} });
      context.warehouse.sealMany([[bean.id, { chars: 1 }]], null, "sha256:" + "a".repeat(64));
      context.warehouse.label(bean.id, "line", "note-trigger", "operator");
      const scheduler = new Scheduler(context, snapshot, runner, { config: { run_cap: 1, cron: true } });
      const first = await scheduler.tick({ pump: false });
      expect(first.triggered).toHaveLength(1);
      expect(first.cron).toEqual([]);
      expect((await scheduler.tick({ pump: false })).triggered).toEqual([]);
      context.db.query("UPDATE runs SET state = 'CANCELLED' WHERE id = ?").run(first.triggered[0]);
      const next = await scheduler.tick({ pump: false });
      expect(next.triggered).toEqual([]);
      expect(next.cron).toHaveLength(1);
      expect((await scheduler.tick({ pump: false })).cron).toEqual([]);
    } finally { close(); }
  });

  test("settles expired approval records during a tick without pumping jobs", async () => {
    const { context, snapshot, clock, runner, close } = await fixture();
    try {
      const job = runner.jobs.create({ station: "Station:writer@1", epoch: snapshot.epoch, inputs: {}, params: {} });
      runner.jobs.transition(job.id, "PROVISIONING");
      runner.jobs.transition(job.id, "RUNNING");
      const request = runner.approvals.openRequest({
        jobId: job.id, attempt: job.attempt, payload: { kind: "expense", summary: "Draft", cost_usd: 2 },
      });
      const scheduler = new Scheduler(context, snapshot, runner, { config: { run_cap: 0 } });
      expect((await scheduler.tick({ pump: false })).expired).toEqual([]);
      clock.advance(600);
      expect((await scheduler.tick({ pump: false })).expired).toEqual([request.id]);
      expect(runner.approvals.get(request.id)).toMatchObject({ status: "denied", decidedBy: "timeout" });
    } finally { close(); }
  });
});
