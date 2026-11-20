import { expect, test } from "bun:test";
import { cpSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Context, LineRunner, LocalProvider, Runner, loadSnapshot } from "../src/legacy";
import { compileDynamicRunPlan } from "../src/legacy/dynamic-switch";

const commands = Object.fromEntries(["count-text", "upper-text"].map((name) => [
  name, [Bun.argv[0], join(import.meta.dir, `../../tests/fakes/tools/${name.replaceAll("-", "_")}.ts`)],
]));
const line = "note-until@1";

async function fixture(root: string, cond: string, maxIter: number, budget = 0) {
  const registry = join(root, "registry");
  cpSync("tests/fixtures/registry-m2", registry, { recursive: true });
  let station = "upper@1";
  if (budget) {
    const path = join(registry, "stations", "upper.yaml");
    const source = await Bun.file(path).text();
    const costed = source.replace("name: upper,", "name: upper-loop,").replace("budget_usd: 0", `budget_usd: ${budget}`);
    writeFileSync(join(registry, "stations", "upper-loop.yaml"), costed);
    station = "upper-loop@1";
  }
  writeFileSync(join(registry, "lines", "note-until.yaml"), Bun.YAML.stringify({
    apiVersion: "mao/v1", kind: "Line", metadata: { name: "note-until", version: 1 },
    spec: {
      interface: { inputs: { note: { type: "note.text@1", requires: ["non_empty"] } }, outputs: { note: { from: "shout.note" } } },
      budget: { cap_usd: 1 },
      steps: { shout: { station, in: { note: "$inputs.note" }, until: { cond, max_iter: maxIter } } },
    },
  }));
  return loadSnapshot(registry);
}

async function seal(context: Context) {
  const file = join(context.root, "material.txt");
  await Bun.write(file, "hello\n");
  const bean = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": file } });
  context.warehouse.sealMany([[bean.id, { chars: 5, lines: 1, has_lower: true }]], null, `sha256:${"0".repeat(64)}`);
  return bean.id;
}

test("stops on the first true until result after restoring a persisted iteration", { timeout: 60_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-until-stop-"));
  let context = Context.open(root);
  try {
    const snapshot = await fixture(root, "gate.attempt >= 2 && out.note.facts.chars > 0", 3, 0.2);
    const provider = new LocalProvider(commands);
    const runner = new Runner(context, snapshot, provider);
    let lines = new LineRunner(runner);
    const bean = await seal(context);
    const run = lines.start(line, { inputs: { note: bean } });
    expect(compileDynamicRunPlan(snapshot, line).node("shout")).toMatchObject({ kind: "until", until: { maxIter: 3 } });
    const [job] = lines.tick(run.id);
    expect(lines.runs.get(run.id).reservedUsd).toBeCloseTo(0.6);
    expect(lines.runs.node(run.id, "shout").reservedUsd).toBeCloseTo(0.4);
    await runner.run(job);
    context.close();
    context = Context.open(root);
    lines = new LineRunner(new Runner(context, snapshot, provider));
    const done = await lines.run(run.id);
    expect(done.state).toBe("SUCCEEDED");
    expect(done.reservedUsd).toBe(0);
    const nodes = Object.fromEntries(lines.runs.nodes(run.id).map((node) => [node.nodeId, node]));
    expect(nodes["shout@1"]).toMatchObject({ state: "DONE", jobId: job });
    expect(nodes["shout@2"].state).toBe("DONE");
    expect(nodes["shout@2"].jobId).toBeString();
    expect(nodes["shout@2"].jobId).not.toBe(job);
    expect(nodes["shout@3"]).toBeUndefined();
    expect(nodes.shout).toMatchObject({ state: "DONE", jobId: null, outputs: nodes["shout@2"].outputs });
    expect(done.outputs.note).toEqual(nodes["shout@2"].outputs.note);
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

test("fails when until is exhausted without passing the final iteration to dependants", { timeout: 60_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-until-limit-"));
  const context = Context.open(root);
  try {
    const snapshot = await fixture(root, "gate.attempt > 5", 2);
    const lines = new LineRunner(new Runner(context, snapshot, new LocalProvider(commands)));
    const run = lines.start(line, { inputs: { note: await seal(context) } });
    const done = await lines.run(run.id);
    const nodes = Object.fromEntries(lines.runs.nodes(run.id).map((node) => [node.nodeId, node]));
    expect(done).toMatchObject({ state: "FAILED", outputs: {}, reservedUsd: 0 });
    expect(nodes["shout@1"].state).toBe("DONE");
    expect(nodes["shout@2"].state).toBe("DONE");
    expect(nodes["shout@3"]).toBeUndefined();
    expect(nodes.shout).toMatchObject({ state: "FAILED", error: { code: "UNTIL_EXHAUSTED", detail: { step: "shout", max_iter: 2 } } });
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

test("reserves all bounded iterations before submitting and releases reservation on cancellation", { timeout: 60_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-until-budget-"));
  const context = Context.open(root);
  try {
    const snapshot = await fixture(root, "false", 3, 0.3);
    const runner = new Runner(context, snapshot, new LocalProvider(commands));
    const lines = new LineRunner(runner);
    const note = await seal(context);
    const short = lines.start(line, { inputs: { note }, budgetCapUsd: 0.5 });
    expect(lines.tick(short.id)).toEqual([]);
    expect(lines.runs.get(short.id)).toMatchObject({ state: "FAILED", reservedUsd: 0, error: { code: "RUN_BUDGET_EXHAUSTED" } });
    expect(lines.runs.nodes(short.id).map((node) => node.nodeId)).toEqual(["shout"]);

    const cancelled = lines.start(line, { inputs: { note } });
    const [first] = lines.tick(cancelled.id);
    expect(lines.runs.get(cancelled.id).reservedUsd).toBeCloseTo(0.9);
    lines.runs.cancel(cancelled.id, "human:test");
    await runner.run(first);
    expect(lines.tick(cancelled.id)).toEqual([]);
    expect(lines.runs.get(cancelled.id)).toMatchObject({ state: "CANCELLED", reservedUsd: 0 });
    expect(lines.runs.nodes(cancelled.id).map((node) => node.nodeId)).toEqual(["shout", "shout@1"]);
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});
