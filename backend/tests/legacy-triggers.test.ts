import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { randomUUID } from "node:crypto";
import { rmSync } from "node:fs";
import { join } from "node:path";
import { CounterIds, FakeClock, utcIso } from "../src/legacy/foundation";
import { Context } from "../src/legacy/context";
import { JobStore } from "../src/legacy/jobs";
import { compileRunPlan } from "../src/legacy/runplan";
import { RunStore } from "../src/legacy/runs";
import { loadSnapshot } from "../src/legacy/snapshot";
import { TriggerEngine } from "../src/legacy/triggers";
import { Warehouse } from "../src/legacy/warehouse";

async function fixture() {
  const root = join("tmp", "legacy-triggers-" + randomUUID());
  const db = new Database(":memory:");
  const ids = new CounterIds();
  const clock = new FakeClock();
  const context = { db, ids, clock };
  const warehouse = new Warehouse(root, db, clock, ids);
  const runs = new RunStore(db, clock, ids);
  const jobs = new JobStore(db, clock, ids);
  const snapshot = await loadSnapshot("tests/fixtures/registry-m2");
  const lines = {
    start(line: string, options: Parameters<RunStore["create"]>[1]) {
      return runs.create(compileRunPlan(snapshot, line), options);
    },
  };
  return {
    context, warehouse, runs, jobs, snapshot, lines,
    close() { db.close(); rmSync(root, { recursive: true, force: true, maxRetries: 4 }); },
  };
}

describe("Bun line triggers", () => {
  test("deduplicates sealed events, resumes the cursor and detects late labels", async () => {
    const { context, warehouse, jobs, runs, snapshot, lines, close } = await fixture();
    try {
      const triggers = new TriggerEngine(context, snapshot, lines);
      const note = await warehouse.add({ type: "note.text@1.0", files: {}, meta: {} });
      warehouse.sealMany([[note.id, { chars: 1 }]], null, "sha256:" + "a".repeat(64));
      const job = jobs.create({ station: "Station:upper@1", epoch: snapshot.epoch, inputs: {}, params: {} });
      jobs.transition(job.id, "SEALED", { outputs: { note: [note.id] } });
      warehouse.label(note.id, "line", "other", "operator");
      expect(triggers.process()).toEqual([]);
      expect(context.db.query<{ seq: number }>("SELECT seq FROM cursors WHERE name = 'trigger.bean.job-events'").get()?.seq)
        .toBeGreaterThan(0);

      warehouse.label(note.id, "line", "note-trigger", "operator");
      const resumed = new TriggerEngine(context, snapshot, lines);
      const [id] = resumed.process();
      expect(id).toBeDefined();
      expect(runs.get(id)).toMatchObject({
        line: "Line:note-trigger@1", state: "PENDING",
        inputs: { note: [note.id] }, trigger: { on: "bean", bean: note.id, label: "line=note-trigger" },
      });
      expect(runs.events(id).map((event) => event.kind)).toEqual(["run.created", "run.trigger"]);
      expect(triggers.process()).toEqual([]);
    } finally { close(); }
  });

  test("records TRIGGER_INVALID for a matching rule with multiple interface inputs", async () => {
    const { context, warehouse, runs, snapshot, lines, close } = await fixture();
    try {
      const triggers = new TriggerEngine(context, snapshot, lines);
      const bean = await warehouse.add({ type: "note.text@1.0", files: {}, meta: {} });
      warehouse.sealMany([[bean.id, { chars: 1 }]], null, "sha256:" + "a".repeat(64));
      warehouse.label(bean.id, "line", "note-two", "acceptance");
      expect(triggers.process()).toEqual([]);
      expect(runs.list()).toEqual([]);
      const events = context.db.query<{ data: string }>("SELECT data FROM events WHERE kind='run.trigger'").all();
      expect(events.map(({ data }) => JSON.parse(data))).toMatchObject([
        { line: "Line:note-two@1", code: "TRIGGER_INVALID" },
      ]);
    } finally { close(); }
  });

  test("preserves recorded trigger cursors across database reopening", async () => {
    const root = join("tmp", "legacy-trigger-reopen-" + randomUUID());
    const snapshot = await loadSnapshot("tests/fixtures/registry-m2");
    let context = Context.open(root);
    const trigger = (runs: RunStore) => new TriggerEngine(context, snapshot, {
      start: (line, options) => runs.create(compileRunPlan(snapshot, line), options),
    });
    const cursor = () => context.db.query<{ seq: number }>("SELECT seq FROM cursors WHERE name='trigger.bean'").get()?.seq;
    try {
      const runs = new RunStore(context.db, context.clock, context.ids);
      const triggers = trigger(runs);
      const bean = await context.warehouse.add({ type: "note.text@1.0", files: {}, meta: {} });
      context.warehouse.sealMany([[bean.id, { chars: 1 }]], null, "sha256:" + "a".repeat(64));
      context.warehouse.label(bean.id, "line", "note-trigger", "acceptance");
      context.db.query("INSERT INTO events (id, at, kind, data) VALUES (?, ?, 'bean.sealed', ?)")
        .run(context.ids.new("ev"), utcIso(context.clock.now()), JSON.stringify({ bean: bean.id }));
      const [fired] = triggers.process();
      expect(fired).toBeDefined();
      const before = cursor();
      expect(before).toBeGreaterThan(0);
      context.close();

      context = Context.open(root);
      const restored = new RunStore(context.db, context.clock, context.ids);
      expect(cursor()).toBe(before);
      expect(trigger(restored).process()).toEqual([]);
      expect(restored.list().map(({ id }) => id)).toEqual([fired]);
    } finally {
      context.close();
      rmSync(root, { recursive: true, force: true, maxRetries: 4 });
    }
  });

  test("rolls back a Run and cursor after a failed transaction, and honors admission limits", async () => {
    const { context, warehouse, runs, snapshot, lines, close } = await fixture();
    try {
      const notes = [];
      for (const marker of ["first", "second"]) {
        const bean = await warehouse.add({ type: "note.text@1.0", files: {}, meta: { marker } });
        notes.push(bean);
        warehouse.sealMany([[bean.id, { chars: 1 }]], null, "sha256:" + "a".repeat(64));
        warehouse.label(bean.id, "line", "note-trigger", "operator");
        context.db.query("INSERT INTO events (id, at, kind, data) VALUES (?, ?, 'bean.sealed', ?)")
          .run(context.ids.new("ev"), utcIso(context.clock.now()), JSON.stringify({ bean: bean.id }));
      }
      const broken = new TriggerEngine(context, snapshot, {
        start(line, options) {
          lines.start(line, options);
          throw new Error("storage check");
        },
      });
      expect(() => broken.process()).toThrow("storage check");
      expect(runs.list()).toEqual([]);
      expect(context.db.query("SELECT seq FROM cursors WHERE name = 'trigger.bean'").get()).toBeNull();
      const recovered = new TriggerEngine(context, snapshot, lines);
      expect(recovered.process({ limit: 0 })).toEqual([]);
      expect(recovered.process({ limit: 1 })).toHaveLength(1);
      expect(recovered.process({ limit: 1 })).toHaveLength(1);
      expect(recovered.process()).toEqual([]);
      expect(runs.list().map((run) => run.trigger.bean).sort()).toEqual(notes.map((bean) => bean.id).sort());
    } finally { close(); }
  });

  test("starts valid cron runs immediately and each interval, ignoring inactive or malformed Lines", async () => {
    const { context, runs, snapshot, lines, close } = await fixture();
    try {
      snapshot.docs["Line:note-dead@1"].spec.trigger = [{ on: "cron", schedule: "invalid" }];
      snapshot.docs["Line:note-trigger@1"].metadata.lifecycle = "retired";
      snapshot.docs["Line:note-trigger@1"].spec.trigger = [{ on: "cron", schedule: "invalid" }];
      const triggers = new TriggerEngine(context, snapshot, lines);
      expect(triggers.processCron({ enabled: false })).toEqual([]);
      const [id] = triggers.processCron();
      expect(runs.get(id)).toMatchObject({
        line: "Line:note-every@1", inputs: {}, trigger: { on: "cron", schedule: "every:1m", by: "scheduler" },
      });
      const resumed = new TriggerEngine(context, snapshot, lines);
      expect(resumed.processCron()).toEqual([]);
      context.clock.advance(59);
      expect(resumed.processCron()).toEqual([]);
      context.clock.advance(1);
      const [next] = resumed.processCron();
      expect(next).not.toBe(id);
      expect(runs.events(next).map((event) => event.kind)).toEqual(["run.created", "scheduler.cron"]);
      const diagnostics = context.db.query<{ data: string }>("SELECT data FROM events WHERE kind = 'scheduler.cron' AND run_id IS NULL").all();
      const errors = diagnostics.map(({ data }) => JSON.parse(data));
      expect(errors.some((error) => error.code === "CRON_INVALID" && error.line === "Line:note-dead@1")).toBe(true);
      expect(errors.some((error) => error.code === "CRON_INPUT_REQUIRED" && error.line === "Line:note-cron@1")).toBe(true);
      expect(errors.some((error) => error.line === "Line:note-trigger@1")).toBe(false);
    } finally { close(); }
  });
});
