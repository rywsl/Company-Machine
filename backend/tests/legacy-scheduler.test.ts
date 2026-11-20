import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rmSync } from "node:fs";
import { join } from "node:path";
import { Context } from "../src/legacy/context";
import { CounterIds, FakeClock } from "../src/legacy/foundation";
import { Runner } from "../src/legacy/runner";
import { LocalProvider } from "../src/legacy/sandbox";
import { ScheduledExecutor, Scheduler, type SchedulerConfig } from "../src/legacy/scheduler";
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

  test("enforces Line compartment quotas and picks higher priority under global pressure", async () => {
    const { context, snapshot, runner, job, close } = await fixture();
    let unblock: () => void = () => {};
    try {
      const create = (line: string) => runner.jobs.create({
        station: "Station:upper@1", epoch: snapshot.epoch, inputs: {}, params: {}, runValue: { line },
      });
      const firstLine = "Line:note-pipeline@1";
      const secondLine = "Line:note-trigger@1";
      const config: SchedulerConfig = {
        pools: { ...pools, cpu: 2, memory_mb: 512 },
        compartments: [
          { name: "pipeline", match: { line: firstLine }, pools },
          { name: "trigger", match: { line: secondLine }, pools, priority: 2 },
        ],
      };
      const scheduler = new Scheduler(context, snapshot, runner, { config });
      expect(() => scheduler.configure({ ...config, pools })).toThrow(/调度器配置不合法/);
      expect(scheduler.demand("upper@1", snapshot, firstLine).pool).toBe("pipeline");
      expect(scheduler.demand("upper@1", snapshot, secondLine).pool).toBe("trigger");
      const globalJob = job();
      const lower = create(firstLine);
      const nextLower = create(firstLine);
      const higher = create(secondLine);
      const gate = new Promise<void>((resolve) => { unblock = resolve; });
      let entered!: () => void;
      const started = new Promise<void>((resolve) => { entered = resolve; });
      runner.run = async (id) => {
        if (id === globalJob.id) { entered(); await gate; }
        return runner.jobs.transition(id, "SEALED");
      };
      scheduler.enqueue([globalJob.id]);
      const pumping = scheduler.pump();
      try {
        expect(await Promise.race([started.then(() => true), pumping.then(() => false)])).toBe(true);
        scheduler.enqueue([lower.id, nextLower.id, higher.id]);
        const resumed = new Scheduler(context, snapshot, runner, { config });
        expect(await resumed.pump()).toEqual([higher.id]);
      } finally {
        unblock();
        await pumping;
      }
      expect(await scheduler.pump()).toEqual([lower.id]);
      expect(await scheduler.pump()).toEqual([nextLower.id]);
    } finally { unblock(); close(); }
  });

  test("opens only above the configured failure rate and persists capped half-open probes", async () => {
    const { context, snapshot, runner, clock, job, close } = await fixture();
    let unblock: () => void = () => {};
    try {
      const subject = "station:Station:upper@1";
      const config: SchedulerConfig = {
        pools: { ...pools, cpu: 2, memory_mb: 512 },
        breakers: [{ subject, min_samples: 2, failure_rate_gt: 0.5, open_for: "5s", probe_limit: 1 }],
      };
      const scheduler = new Scheduler(context, snapshot, runner, { config });
      const state = () => context.db.query<{ state: string }>("SELECT state FROM breaker_state WHERE subject = ?").get(subject)?.state;
      runner.jobs.transition(job().id, "SEALED");
      runner.jobs.transition(job().id, "DEAD");
      expect(await scheduler.pump()).toEqual([]);
      expect(state()).toBeUndefined();
      runner.jobs.transition(job().id, "DEAD");
      const first = job();
      const second = job();
      scheduler.enqueue([first.id, second.id]);
      expect(await scheduler.pump()).toEqual([]);
      expect(state()).toBe("open");

      clock.advance(5);
      const resumed = new Scheduler(context, snapshot, runner, { config });
      let entered!: () => void;
      const started = new Promise<void>((resolve) => { entered = resolve; });
      const gate = new Promise<void>((resolve) => { unblock = resolve; });
      runner.run = async (id) => {
        if (id === first.id) { entered(); await gate; }
        return runner.jobs.transition(id, "SEALED");
      };
      const probing = resumed.pump();
      try {
        expect(await Promise.race([started.then(() => true), probing.then(() => false)])).toBe(true);
        expect(state()).toBe("half_open");
        expect(await scheduler.pump()).toEqual([]);
      } finally {
        unblock();
        expect(await probing).toEqual([first.id]);
      }
      expect(state()).toBe("closed");
      expect(await scheduler.pump()).toEqual([second.id]);
      expect(state()).toBe("closed");
      expect(context.db.query<{ data: string }>("SELECT data FROM events WHERE kind = 'scheduler.circuit' ORDER BY seq").all()
        .map(({ data }) => JSON.parse(data).to)).toEqual(["open", "half_open", "closed"]);
    } finally { unblock(); close(); }
  });

  test("reopens machine and provider circuits when their half-open probe fails", async () => {
    const { context, snapshot, runner, clock, job, close } = await fixture();
    try {
      const station = snapshot.spec("Station:upper@1");
      const provider = "provider:upstream.example";
      snapshot.docs[snapshot.key("Policy", station.policy)].spec.egress = { providers: ["upstream.example"] };
      const subjects = ["machine:" + snapshot.key("Machine", station.machine), provider];
      const scheduler = new Scheduler(context, snapshot, runner, { config: {
        pools, breakers: subjects.map((subject) => ({ subject, min_samples: 1, failure_rate_gt: 0, open_for: "2s", probe_limit: 1 })),
      } });
      const state = () => context.db.query<{ state: string }>("SELECT state FROM breaker_state ORDER BY subject").all().map(({ state }) => state);
      runner.jobs.transition(job().id, "DEAD");
      const probe = job();
      const waiting = job();
      scheduler.enqueue([probe.id, waiting.id]);
      runner.run = async (id) => runner.jobs.transition(id, "DEAD");
      expect(await scheduler.pump()).toEqual([]);
      expect(state()).toEqual(["open", "open"]);
      clock.advance(2);
      expect(await scheduler.pump()).toEqual([probe.id]);
      expect(state()).toEqual(["open", "open"]);
      expect(await scheduler.pump()).toEqual([]);
      expect(scheduler.queued()).toEqual([waiting.id]);
    } finally { close(); }
  });

  test("automatically quarantines only after the configured consecutive failures", async () => {
    const { context, snapshot, runner, job, close } = await fixture();
    try {
      const config = { pools, toxic_failures: 2 };
      const scheduler = new Scheduler(context, snapshot, runner, { config });
      const bean = await context.warehouse.add({ type: "note.text@1.0", files: {}, meta: {} });
      const next = () => job({ note: [bean.id, bean.id] });
      const fail = () => runner.jobs.transition(next().id, "DEAD");
      fail();
      runner.jobs.transition(next().id, "SEALED");
      fail();
      expect(await scheduler.pump()).toEqual([]);
      expect(scheduler.isQuarantined(bean.id)).toBe(false);
      fail();
      const waiting = next();
      const resumed = new Scheduler(context, snapshot, runner, { config });
      resumed.enqueue([waiting.id]);
      expect(await resumed.pump()).toEqual([]);
      expect(resumed.isQuarantined(bean.id)).toBe(true);
      expect(context.db.query("SELECT source, by FROM quarantines WHERE bean_id = ?").get(bean.id)).toEqual({ source: "auto", by: "scheduler" });
      resumed.release(bean.id, { by: "operator" });
      runner.run = async (id) => runner.jobs.transition(id, "SEALED");
      expect(await resumed.pump()).toEqual([waiting.id]);
      expect(resumed.isQuarantined(bean.id)).toBe(false);
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
