import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rmSync } from "node:fs";
import { join } from "node:path";
import { JobCache } from "../src/legacy/cache";
import { Context } from "../src/legacy/context";
import { FactoryError } from "../src/legacy/errors";
import { CounterIds, FakeClock } from "../src/legacy/foundation";
import { LineRunner } from "../src/legacy/line-runner";
import { Runner } from "../src/legacy/runner";
import { LocalProvider } from "../src/legacy/sandbox";
import { ScheduledExecutor, Scheduler, type SchedulerConfig } from "../src/legacy/scheduler";
import { loadSnapshot } from "../src/legacy/snapshot";

const pools = { cpu: 1, memory_mb: 256, gpu: 0 };

function expectCode(run: () => unknown, code: string): void {
  try { run(); }
  catch (error) {
    expect(error).toBeInstanceOf(FactoryError);
    expect((error as FactoryError).code).toBe(code);
    return;
  }
  throw new Error("expected " + code);
}

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
  test("drives a Line to completion through ScheduledExecutor dispatch and wait", async () => {
    const { context, snapshot, close } = await fixture();
    try {
      const file = join(context.root, "scheduler-note.txt");
      await Bun.write(file, "hello\n");
      const bean = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": file }, meta: {} });
      context.warehouse.sealMany([[bean.id, { chars: 5 }]], null, "sha256:" + "a".repeat(64));
      const tools = join(import.meta.dir, "../../tests/fakes/tools");
      const runner = new Runner(context, snapshot, new LocalProvider({
        "upper-text": [Bun.argv[0], join(tools, "upper_text.ts")],
        "count-text": [Bun.argv[0], join(tools, "count_text.ts")],
      }));
      const scheduler = new Scheduler(context, snapshot, runner, { config: { pools } });
      const executor = new ScheduledExecutor(scheduler);
      const line = new LineRunner(runner);
      const run = line.start("note-trigger@1", { inputs: { note: bean.id } });
      const submitted = line.tick(run.id, (ids) => executor.dispatch(ids));
      expect(submitted).toHaveLength(1);
      expect(runner.jobs.get(submitted[0]).state).toBe("PENDING");
      await executor.wait(1);
      expect(line.tick(run.id, (ids) => executor.dispatch(ids))).toEqual([]);
      expect(line.runs.get(run.id)).toMatchObject({ id: run.id, state: "SUCCEEDED" });
      expect(line.runs.nodes(run.id)[0]).toMatchObject({ state: "DONE" });
    } finally { close(); }
  });

  test("detects missing configuration and leaves jobs queued for absent capacity", async () => {
    const { context, snapshot, runner, job, close } = await fixture();
    try {
      const scheduler = new Scheduler(context, snapshot, runner);
      expectCode(() => scheduler.loadConfig(), "SCHEDULER_CONFIG_MISSING");
      await Bun.write(join(context.root, "scheduler.json"), JSON.stringify({ pools: { memory_mb: 256 } }));
      scheduler.loadConfig();
      expectCode(() => scheduler.demand("upper@1"), "SCHEDULER_CAPACITY_UNSET");
      const pending = job();
      scheduler.enqueue([pending.id]);
      expect(await scheduler.pump()).toEqual([]);
      expect(scheduler.queued()).toEqual([pending.id]);
      const queued = context.db.query<{ data: string }>("SELECT data FROM events WHERE kind = 'scheduler.queued' AND job_id = ?").get(pending.id);
      expect(JSON.parse(queued!.data)).toMatchObject({ reason: "capacity", code: "SCHEDULER_CAPACITY_UNSET" });
      await Bun.write(join(context.root, "scheduler.json"), JSON.stringify({ pools: { cpu: null, memory_mb: 256 } }));
      scheduler.loadConfig();
      runner.run = async (id) => runner.jobs.transition(id, "SEALED");
      expect(await scheduler.pump()).toEqual([pending.id]);
    } finally { close(); }
  });

  test("enforces a zero WIP limit by station name until it is removed", async () => {
    const { context, snapshot, runner, job, close } = await fixture();
    try {
      const scheduler = new Scheduler(context, snapshot, runner, { config: { pools, wip: [{ station: "upper", max: 0 }] } });
      const waiting = job();
      scheduler.enqueue([waiting.id]);
      runner.run = async (id) => runner.jobs.transition(id, "SEALED");
      expect(await scheduler.pump()).toEqual([]);
      expect(scheduler.queued()).toEqual([waiting.id]);
      expect(runner.jobs.get(waiting.id).state).toBe("PENDING");
      scheduler.configure({ pools });
      expect(await scheduler.pump()).toEqual([waiting.id]);
      expect(runner.jobs.get(waiting.id).state).toBe("SEALED");
    } finally { close(); }
  });

  test("leaves breakers and automatic quarantine idle when these controls are unconfigured", async () => {
    const { context, snapshot, runner, job, close } = await fixture();
    try {
      const bean = await context.warehouse.add({ type: "note.text@1.0", files: {}, meta: {} });
      const withBean = () => job({ note: [bean.id] });
      const scheduler = new Scheduler(context, snapshot, runner, { config: { pools, breakers: [], toxic_failures: null } });
      for (let counter = 0; counter < 3; counter += 1) runner.jobs.transition(withBean().id, "DEAD");
      const next = withBean();
      scheduler.enqueue([next.id]);
      runner.run = async (id) => runner.jobs.transition(id, "SEALED");
      expect(await scheduler.pump()).toEqual([next.id]);
      expect(scheduler.isQuarantined(bean.id)).toBe(false);
      expect(context.db.query("SELECT subject FROM breaker_state").all()).toEqual([]);
      expect(context.db.query("SELECT seq FROM scheduler_toxic_cursor").all()).toEqual([]);
    } finally { close(); }
  });

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
      expect(scheduler.demand("upper@1", snapshot, firstLine)).toMatchObject({ cpu: 1, pool: "pipeline" });
      expect(scheduler.demand("idle@1", snapshot, secondLine)).toMatchObject({ cpu: 0, memoryMb: 0, pool: "trigger" });
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
      const tied = job();
      scheduler.enqueue([tied.id]);
      runner.run = async (id) => runner.jobs.transition(id, "SEALED");
      expect(await scheduler.pump()).toEqual([tied.id]);
      expect(state()).toBeUndefined();
      runner.jobs.transition(job().id, "DEAD");
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

  test("reports dead jobs without writing history and never redispatches them", async () => {
    const { context, snapshot, clock, runner, job, close } = await fixture();
    try {
      const scheduler = new Scheduler(context, snapshot, runner, { config: { pools } });
      const first = runner.jobs.transition(job().id, "DEAD", { error: { code: "FIRST_FAILURE" } });
      scheduler.enqueue([first.id]);
      clock.advance(1);
      const second = runner.jobs.transition(job().id, "DEAD", { error: { code: "ANOTHER_FAILURE", message: "token=fixture" } });
      const current = scheduler.listDead();
      expect(current.map(({ jobId }) => jobId)).toEqual([first.id, second.id]);
      expect(scheduler.dead(second.id)).toMatchObject({ errorCode: "ANOTHER_FAILURE", error: { message: "token=[REDACTED]" }, at: second.updatedAt });
      expect(context.db.query("SELECT job_id FROM dead_letters ORDER BY job_id").all()).toEqual([{ job_id: first.id }]);
      expectCode(() => scheduler.dead("unknown"), "DLQ_NOT_FOUND");
      runner.run = async () => { throw new Error("dead job was dispatched"); };
      clock.advance(1);
      scheduler.enqueue([second.id, first.id]);
      expect(await scheduler.pump()).toEqual([]);
      expect(runner.jobs.get(second.id).attempt).toBe(second.attempt);
      expect(scheduler.listDead()).toEqual(current);
    } finally { close(); }
  });

  test("shares scheduler quarantine with cache lookup and restores hits after release", async () => {
    const { context, snapshot, runner, job, close } = await fixture();
    try {
      const scheduler = new Scheduler(context, snapshot, runner, { config: { pools } });
      const cache = new JobCache(context, runner.jobs);
      const input = await context.warehouse.add({ type: "note.text@1.0", files: {}, meta: { source: "cache test" } });
      const output = await context.warehouse.add({ type: "note.text@1.0", files: {}, meta: { source: "scheduler test" } });
      context.warehouse.sealMany([[input.id, { chars: 1 }], [output.id, { chars: 1 }]], null, "sha256:" + "a".repeat(64));
      const material = cache.material(snapshot, "upper@1", { note: input.id }, {});
      const result = job({ note: [input.id] });
      cache.bind(result.id, material);
      runner.jobs.transition(result.id, "SEALED", { outputs: { note: [output.id] } });
      expect(scheduler.isQuarantined(output.id)).toBe(false);
      expect(cache.lookup(material.key())?.id).toBe(result.id);
      scheduler.quarantine(output.id, { by: "acceptance", reason: "toxic" });
      expect(scheduler.isQuarantined(output.id)).toBe(true);
      expect(cache.lookup(material.key())).toBeNull();
      scheduler.release(output.id, { by: "acceptance" });
      expect(cache.lookup(material.key())?.id).toBe(result.id);
    } finally { close(); }
  });

  test("parses Gi memory demand in MiB and rejects nonzero unitless sizes", async () => {
    const { context, snapshot, runner, close } = await fixture();
    try {
      const station = snapshot.spec("Station:upper@1");
      const declaration = snapshot.docs[snapshot.key("Machine", station.machine)].spec.runtime.resources;
      declaration.memory = "8Gi";
      const scheduler = new Scheduler(context, snapshot, runner);
      await Bun.write(join(context.root, "scheduler.json"), JSON.stringify({ pools: { cpu: 1, memory: 8192 } }));
      scheduler.loadConfig();
      expect(scheduler.demand("upper@1").memoryMb).toBe(8192);
      for (const memory of [8, "8GiB"]) {
        declaration.memory = memory;
        scheduler.loadConfig();
        expectCode(() => scheduler.demand("upper@1"), "SCHEDULER_RESOURCE_INVALID");
      }
    } finally { close(); }
  });

  test("fires a one-minute cron at its configured interval and diagnoses input-bound lines", async () => {
    const { context, snapshot, clock, runner, close } = await fixture();
    try {
      const scheduler = new Scheduler(context, snapshot, runner, { config: { pools } });
      expect((await scheduler.tick({ pump: false })).cron).toEqual([]);
      const eventRows = context.db.query<{ data: string }>("SELECT data FROM events WHERE kind = 'scheduler.cron'").all();
      expect(eventRows.map(({ data }) => JSON.parse(data))).toContainEqual({ line: "Line:note-cron@1", code: "CRON_INPUT_REQUIRED" });
      scheduler.configure({ pools, cron: true });
      const first = (await scheduler.tick({ pump: false })).cron;
      expect(first).toHaveLength(1);
      expect((await scheduler.tick({ pump: false })).cron).toEqual([]);
      clock.advance(59);
      expect((await scheduler.tick({ pump: false })).cron).toEqual([]);
      clock.advance(1);
      const next = (await scheduler.tick({ pump: false })).cron;
      expect(next).toHaveLength(1);
      expect(next[0]).not.toBe(first[0]);
      const lines = new LineRunner(runner);
      expect(lines.runs.get(next[0]).line).toBe("Line:note-every@1");
    } finally { close(); }
  });

  test("applies a common run cap across Bean and cron triggers", async () => {
    const { context, snapshot, runner, close } = await fixture();
    try {
      const bean = await context.warehouse.add({ type: "note.text@1.0", files: {}, meta: {} });
      context.warehouse.sealMany([[bean.id, { chars: 1 }]], null, "sha256:" + "a".repeat(64));
      context.warehouse.label(bean.id, "line", "note-trigger", "operator");
      const scheduler = new Scheduler(context, snapshot, runner, { config: { run_cap: 0, cron: true } });
      expect(await scheduler.tick({ pump: false })).toMatchObject({ triggered: [], cron: [] });
      expect(new LineRunner(runner).runs.list()).toEqual([]);
      scheduler.configure({ run_cap: 1, cron: true });
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
