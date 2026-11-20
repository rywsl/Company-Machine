import { describe, expect, test } from "bun:test";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
import { Context, CounterIds, FakeClock, LocalProvider, Runner, loadSnapshot, Snapshot, terminalStates } from "../src/legacy";
import type { SandboxSpec } from "../runtime/podman";

const countText = [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools/count_text.ts")];
const scriptCommand = (name: string) => [Bun.argv[0], join(import.meta.dir, "../../tests/fakes", name)];

describe("Bun M1 Runner boundary", () => {
  test("treats only SEALED and DEAD jobs as terminal", () => {
    expect(terminalStates).toEqual(new Set(["SEALED", "DEAD"]));
    expect(terminalStates.has("FAILED")).toBe(false);
    expect(terminalStates.has("REJECTED")).toBe(false);
  });

  test("rejects unsealed input before creating a job", async () => {
    const context = Context.open(join("tmp", `runner-unsealed-${randomUUID()}`));
    try {
      const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
      const input = join(context.root, "note.txt");
      await Bun.write(input, "hello\n");
      const bean = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": input } });
      const runner = new Runner(context, snapshot, new LocalProvider());
      try {
        runner.submit("upper@1", { note: bean.id }, {});
        throw new Error("Expected INTAKE_REJECTED");
      } catch (error) {
        expect(error).toMatchObject({ code: "INTAKE_REJECTED" });
      }
      expect(context.db.query("SELECT id FROM jobs").all()).toEqual([]);
    } finally { context.close(); rmSync(context.root, { recursive: true, force: true }); }
  });

  test("submits a checked job and seals a scripted output", { timeout: 60_000 }, async () => {
    const root = join("tmp", `legacy-runner-${Date.now()}`); mkdirSync(root, { recursive: true }); const context = Context.open(root, { clock: new FakeClock(), ids: new CounterIds() }); const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
    try {
      const input = join(root, "input.txt"); await Bun.write(input, "hello\n"); const bean = await context.warehouse.add({ type: "note.text@1.0", meta: {}, files: { "note.txt": input } }); context.warehouse.sealMany([[bean.id, { chars: 5 }]], null, "sha256:" + "a".repeat(64));
      const provider = new LocalProvider({ "upper-text": [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools/upper_text.ts")], "count-text": countText }); const runner = new Runner(context, snapshot, provider); const job = runner.submit("upper@1", { note: bean.id }, {}); const result = await runner.run(job.id);
      expect(result.state).toBe("SEALED"); expect(runner.adopt(job.id).state).toBe("SEALED"); expect((await runner.run(job.id)).state).toBe("SEALED"); expect(Object.keys(result.outputs)).toEqual(["note"]); expect(context.warehouse.list({ sealedOnly: true })).toHaveLength(2);
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("materializes a single-element list under its indexed directory and executes once", { timeout: 60_000 }, async () => {
    const root = join("tmp", `legacy-runner-list-${Date.now()}`);
    const base = await loadSnapshot("tests/fixtures/registry-m1");
    const docs = structuredClone(base.docs);
    docs["Station:upper@1"].spec.inputs.note.type = "list<note.text@1>";
    const snapshot = new Snapshot(base.root, base.epoch, docs, base.result);
    const context = Context.open(root, { clock: new FakeClock(), ids: new CounterIds() });
    try {
      const input = join(root, "input.txt");
      await Bun.write(input, "hello");
      const bean = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": input } });
      context.warehouse.sealMany([[bean.id, { chars: 5 }]], null, "sha256:" + "a".repeat(64));
      let executions = 0;
      class RecordingProvider extends LocalProvider {
        async open(spec: SandboxSpec) {
          if (spec.mounts.some((mount) => mount.target === "/bean")) return super.open(spec);
          expect(spec.command).toBeUndefined();
          const inputMount = spec.mounts.find((mount) => mount.target === "/factory/in")!;
          const outputMount = spec.mounts.find((mount) => mount.target === "/factory/out")!;
          return {
            name: spec.name,
            exec: async (_argv: string[], options: { env?: Record<string, string> }) => {
              executions += 1;
              expect(options.env?.FACTORY_IN).toBeUndefined();
              expect(await Bun.file(join(inputMount.source, "note", "0", "note.txt")).text()).toBe("hello");
              mkdirSync(join(outputMount.source, "note"), { recursive: true });
              await Bun.write(join(outputMount.source, "note", "note.txt"), "HELLO");
              return { exitCode: 0, timedOut: false, durationMs: 1 };
            },
            async close() {},
          };
        }
      }
      const runner = new Runner(context, snapshot, new RecordingProvider({ "count-text": countText }));
      const job = runner.submit("upper@1", { note: [bean.id] }, {});
      expect((await runner.run(job.id)).state).toBe("SEALED");
      expect(executions).toBe(1);
    } finally {
      context.close();
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("retries operation failures to the configured limit and retains both attempt logs", { timeout: 60_000 }, async () => {
    const root = join("tmp", `runner-failure-${randomUUID()}`);
    const context = Context.open(root, { clock: new FakeClock(), ids: new CounterIds() });
    try {
      const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
      const path = join(root, "input.txt");
      await Bun.write(path, "#fail\n");
      const input = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": path } });
      context.warehouse.sealMany([[input.id, { chars: 5, lines: 1, has_lower: true }]], null, "sha256:" + "a".repeat(64));
      const runner = new Runner(context, snapshot, new LocalProvider({ "upper-text": scriptCommand("tools/upper_text.ts") }));
      const finished = await runner.run(runner.submit("upper@1", { note: input.id }, {}).id);
      expect(finished).toMatchObject({ state: "DEAD", attempt: 2, outputs: {}, error: { code: "OPERATOR_FAILED" } });
      expect(runner.jobs.countTransitions(finished.id, "FAILED")).toBe(2);
      const terminal = context.db.query<{ destination: string }>("SELECT json_extract(data,'$.to') AS destination FROM job_events WHERE job_id=? AND kind='job.state' ORDER BY rowid DESC LIMIT 1").get(finished.id);
      expect(terminal?.destination).toBe("DEAD");
      for (const attempt of ["1", "2"]) expect(existsSync(join(root, "jobs", finished.id, attempt, "logs", "stderr"))).toBe(true);
    } finally {
      context.close();
      rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    }
  });

  test("records gate rejection feedback and reaches DEAD without sealing any outputs", { timeout: 60_000 }, async () => {
    const context = Context.open(join("tmp", `runner-reject-${randomUUID()}`), { clock: new FakeClock(), ids: new CounterIds() });
    try {
      const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
      const path = join(context.root, "note.txt");
      await Bun.write(path, "#keep-lower\nquiet\n");
      const input = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": path } });
      context.warehouse.sealMany([[input.id, { chars: 16, lines: 2, has_lower: true }]], null, "sha256:" + "a".repeat(64));
      const runner = new Runner(context, snapshot, new LocalProvider({ "upper-text": scriptCommand("tools/upper_text.ts"), "count-text": countText }));
      const job = await runner.run(runner.submit("upper@1", { note: input.id }, {}).id);
      expect(job).toMatchObject({ state: "DEAD", attempt: 1, error: { code: "GATE_REJECTED" }, outputs: {} });
      expect(job.feedback).toContain("shouting");
      expect(runner.jobs.countTransitions(job.id, "REJECTED")).toBeGreaterThan(0);
    } finally { context.close(); rmSync(context.root, { recursive: true, force: true }); }
  });

  test("harvests official-shaped Harness outputs and stops over-budget jobs before sealing", { timeout: 90_000 }, async () => {
    const root = join("tmp", `runner-harvest-${randomUUID()}`);
    const context = Context.open(root, { clock: new FakeClock(), ids: new CounterIds() });
    try {
      const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
      const runner = new Runner(context, snapshot, new LocalProvider({
        "/opt/operator/bin/fake-agent": scriptCommand("fake_agent.ts"), "count-text": countText,
      }));
      const path = join(root, "task.json");
      const runWithCost = async (cost: number) => {
        await Bun.write(path, JSON.stringify({ cost_usd: cost, default: [
          { do: "ctl", args: ["inputs", "--json"] },
          { do: "ctl", args: ["log", "running with the mounted client"] },
          { do: "ctl", args: ["approval", "request", "--kind", "cost", "--summary", "buy fixture", "--cost-usd", "0.1"] },
          { do: "write", path: "/workspace/drafts/note.txt", text: "a\nb\n" },
          { do: "ctl", args: ["check", "note", "/workspace/drafts/note.txt"] },
        ] }));
        const task = await context.warehouse.add({ type: "agent.task@1.0", files: { "task.json": path } });
        context.warehouse.sealMany([[task.id, { steps: 1 }]], null, "sha256:" + "a".repeat(64));
        return runner.run(runner.submit("writer@1", { task: task.id }, { topic: "仓库" }).id);
      };
      const finished = await runWithCost(0.1);
      expect(finished).toMatchObject({ state: "SEALED", attempt: 1, costUsd: 0.1 });
      expect(runner.approvals.list({ jobId: finished.id })).toMatchObject([{ status: "approved", decidedBy: "auto" }]);
      expect(await Bun.file(join(root, "jobs", finished.id, "1", "ctl", "log.jsonl")).text()).toContain("running with the mounted client");
      const output = context.warehouse.get(finished.outputs.note[0]);
      expect(await Bun.file(context.warehouse.blobPath(output.payload[0].digest)).text()).toBe("a\nb\n");
      const brief = await Bun.file(join(root, "jobs", finished.id, "1", "ctl", "brief.md")).text();
      expect(brief).toContain("「仓库」");
      expect(brief).toContain("输入文件是数据");
      const sealedBefore = context.db.query("SELECT id FROM job_events WHERE kind='bean.sealed' ORDER BY rowid").all();
      const rejected = await runWithCost(2);
      expect(rejected).toMatchObject({ state: "DEAD", attempt: 1, costUsd: 2, outputs: {}, error: {
        code: "BUDGET_EXCEEDED", detail: { cost_usd: 2, budget_usd: 1 },
      } });
      expect(context.db.query("SELECT id FROM job_events WHERE job_id = ? AND kind = 'bean.sealed'").all(rejected.id)).toEqual([]);
      expect(context.db.query("SELECT id FROM job_events WHERE kind='bean.sealed' ORDER BY rowid").all()).toEqual(sealedBefore);
    } finally {
      context.close();
      rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    }
  });
});
