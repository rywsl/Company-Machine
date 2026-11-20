import { expect, test } from "bun:test";
import { cpSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Context, LineRunner, LocalProvider, Runner, loadSnapshot } from "../src/legacy";
import { compileDynamicRunPlan } from "../src/legacy/dynamic-switch";

const line = "note-parent@1";
const commands = Object.fromEntries(["count-text", "upper-text"].map((name) => [
  name, [Bun.argv[0], join(import.meta.dir, `../../tests/fakes/tools/${name.replaceAll("-", "_")}.ts`)],
]));

async function fixture(root: string) {
  const registry = join(root, "registry");
  cpSync("tests/fixtures/registry-m2", registry, { recursive: true });
  const step = { station: "upper@1", in: { note: "$inputs.note" } };
  for (const [name, port, steps, cap] of [
    ["note-child", "shout", { shout: step }, 0.5],
    ["note-parent", "child", { child: { subline: "note-child@1", in: { note: "$inputs.note" } } }, 1],
  ] as const) {
    writeFileSync(join(registry, "lines", `${name}.yaml`), Bun.YAML.stringify({
      apiVersion: "mao/v1", kind: "Line", metadata: { name, version: 1 },
      spec: {
        interface: { inputs: { note: { type: "note.text@1", requires: ["non_empty"] } }, outputs: { note: { from: `${port}.note`, type: "note.text@1" } } },
        budget: { cap_usd: cap }, steps,
      },
    }));
  }
  return loadSnapshot(registry);
}

async function seal(context: Context, text = "alpha\nbeta\ngamma\n") {
  const path = join(context.root, "input.txt");
  await Bun.write(path, text);
  const bean = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": path } });
  context.warehouse.sealMany([[bean.id, { chars: text.length, lines: 3, has_lower: true }]], null, `sha256:${"0".repeat(64)}`);
  return bean.id;
}

test("runs one frozen child in the parent epoch and propagates the same output after restart", { timeout: 60_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-subline-resume-"));
  let context = Context.open(root);
  try {
    const snapshot = await fixture(root);
    const provider = new LocalProvider(commands);
    let lines = new LineRunner(new Runner(context, snapshot, provider));
    const parent = lines.start(line, { inputs: { note: await seal(context) }, useCache: false });
    const plan = compileDynamicRunPlan(snapshot, line);
    expect(plan.node("child")).toMatchObject({ kind: "subline", subline: { line: "Line:note-child@1" } });
    expect(plan.node("child").subline?.digest).toMatch(/^sha256:[0-9a-f]{64}$/);
    const repeated: string[] = [];
    expect(lines.tick(parent.id)).toHaveLength(1);
    expect(lines.tick(parent.id, (jobs) => repeated.push(...jobs))).toEqual([]);
    expect(repeated).toHaveLength(1);
    expect(lines.runs.get(parent.id).reservedUsd).toBe(0.5);
    const [child] = lines.runs.list().filter((run) => run.trigger.on === "subline");
    expect(child.trigger).toEqual({ on: "subline", parent: parent.id, step: "child" });
    expect(child.epoch).toBe(parent.epoch);
    expect(child.line).toBe("Line:note-child@1");
    expect(lines.runs.plan(child.id).digest()).toBe(plan.node("child").subline!.digest);
    context.close();
    context = Context.open(root);
    lines = new LineRunner(new Runner(context, snapshot, provider));
    const done = await lines.run(parent.id);
    const childDone = lines.runs.get(child.id);
    expect(done).toMatchObject({ state: "SUCCEEDED", reservedUsd: 0 });
    expect(childDone.state).toBe("SUCCEEDED");
    expect(lines.runs.node(parent.id, "child")).toMatchObject({ state: "DONE", jobId: null, outputs: childDone.outputs });
    expect(lines.runs.list().filter((run) => run.trigger.on === "subline").map((run) => run.id)).toEqual([child.id]);
    expect(done.outputs.note).toEqual(childDone.outputs.note);
    const output = context.warehouse.get(childDone.outputs.note[0]);
    expect(await Bun.file(context.warehouse.blobPath(output.payload[0].digest)).text()).toBe("ALPHA\nBETA\nGAMMA\n");
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

test("reserves the child cap before attachment and cancels the persisted child with its parent", { timeout: 60_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-subline-budget-"));
  const context = Context.open(root);
  try {
    const snapshot = await fixture(root);
    const lines = new LineRunner(new Runner(context, snapshot, new LocalProvider(commands)));
    const input = await seal(context);
    const refused = lines.start(line, { inputs: { note: input }, budgetCapUsd: 0.3 });
    expect(lines.tick(refused.id)).toEqual([]);
    expect(lines.runs.get(refused.id)).toMatchObject({ state: "FAILED", error: { code: "RUN_BUDGET_EXHAUSTED" }, reservedUsd: 0 });
    expect(lines.runs.list()).toHaveLength(1);
    const parent = lines.start(line, { inputs: { note: input }, useCache: false });
    expect(lines.tick(parent.id)).toHaveLength(1);
    const [child] = lines.runs.list().filter((run) => run.trigger.parent === parent.id);
    lines.runs.cancel(parent.id, "human:test");
    expect(await lines.run(parent.id)).toMatchObject({ state: "CANCELLED", reservedUsd: 0 });
    expect(lines.runs.get(child.id)).toMatchObject({ state: "CANCELLED", reservedUsd: 0 });
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

test("settles child costs exactly once and propagates child failure", { timeout: 60_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-subline-failure-"));
  const context = Context.open(root);
  try {
    const snapshot = await fixture(root);
    const provider = new LocalProvider({ ...commands, "upper-text": [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools/exit_three.ts")] });
    const runner = new Runner(context, snapshot, provider);
    const lines = new LineRunner(runner);
    const parent = lines.start(line, { inputs: { note: await seal(context) }, useCache: false });
    const [job] = lines.tick(parent.id);
    const [child] = lines.runs.list().filter((run) => run.trigger.parent === parent.id);
    expect((await runner.run(job)).state).toBe("DEAD");
    context.db.query("UPDATE jobs SET cost_usd = ? WHERE id = ?").run(0.2, job);
    lines.tick(parent.id);
    expect(lines.runs.get(child.id)).toMatchObject({ state: "FAILED", spentUsd: 0.2 });
    expect(lines.runs.node(parent.id, "child")).toMatchObject({ state: "FAILED", error: { code: "SUBLINE_FAILED", detail: { child: child.id, child_state: "FAILED" } } });
    expect(lines.runs.get(parent.id)).toMatchObject({ state: "FAILED", spentUsd: 0.2, reservedUsd: 0 });
    expect(lines.tick(parent.id)).toEqual([]);
    expect(lines.runs.get(parent.id).spentUsd).toBe(0.2);
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});
