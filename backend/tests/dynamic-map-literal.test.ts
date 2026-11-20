import { expect, test } from "bun:test";
import { cpSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { compileDynamicRunPlan } from "../src/legacy/dynamic-switch";
import { Context, LineRunner, LocalProvider, RunPlan, Runner, loadSnapshot } from "../src/legacy";

const line = "literal-map@1";
const upperConverter = "shout.in.note~note-to-text@1";

async function fixture(over: string[], options: { overflow?: "truncate"; skip?: boolean } = {}) {
  const root = mkdtempSync(join(tmpdir(), "mao-map-literal-"));
  const context = Context.open(root);
  const close = () => { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); };
  try {
    const registry = join(root, "registry");
    cpSync("tests/fixtures/registry-m2", registry, { recursive: true });
    const toolbox = join(registry, "machines", "toolbox", "machine.yaml");
    await Bun.write(toolbox, (await Bun.file(toolbox).text()) + `
    tag:
      inputs: {note: {type: note.text@1}}
      params: {tag: {type: string}}
      outputs: {note: {type: note.text@1}}
      run: [tag-note, /factory/in/note/note.txt, /factory/out/note/note.txt, "{{ params.tag }}"]
`);
    await Bun.write(join(registry, "stations", "tag.yaml"), `apiVersion: mao/v1
kind: Station
metadata: {name: tag, version: 1}
spec:
  role: transform
  machine: toolbox@1
  recipe: tag
  operator: {runtime: script@1}
  params: {tag: {type: string}}
  inputs: {note: {type: note.text@1, requires: [non_empty]}}
  outputs: {note: {type: note.text@1, guarantees: [non_empty]}}
  limits: {timeout: 60s, budget_usd: 0.25, retries: {rejected: 0, failed: 0, mode: fresh}}
  policy: tool-default@1
`);
    await Bun.write(join(registry, "lines", "literal-map.yaml"), Bun.YAML.stringify({
      apiVersion: "mao/v1", kind: "Line", metadata: { name: "literal-map", version: 1 },
      spec: {
        interface: { inputs: { raw: { type: "note.raw@1", requires: ["non_empty"] } }, outputs: { notes: { from: "shout.note", type: "list<note.text@1>" } } },
        budget: { cap_usd: 1 },
        ...(options.skip ? { keep: ["plain.note"] } : {}),
        steps: {
          shout: { station: "tag@1", in: { note: "$inputs.raw" }, params: { tag: "{{ label }}" }, map: { over, as: "label", max_items: 2, max_parallel: 1, overflow: options.overflow ?? "reject" } },
          ...(options.skip ? {
            choose: { switch: { on: "$inputs.raw", cases: [{ when: "false", goto: "shout" }], default: "plain" } },
            plain: { station: "upper@1", in: { note: "$inputs.raw" } },
          } : {}),
        },
      },
    }));
    const snapshot = await loadSnapshot(registry);
    const tool = join(root, "tag.ts");
    await Bun.write(tool, `
const [source, destination, tag] = Bun.argv.slice(2);
await Bun.write(destination, (await Bun.file(source).text()).trim() + ":" + tag + "\\n");
`);
    const provider = new LocalProvider({
      "tag-note": [Bun.argv[0], tool],
      ...Object.fromEntries(["count-text", "upper-text"].map((name) => [name, [Bun.argv[0], join(import.meta.dir, `../../tests/fakes/tools/${name.replaceAll("-", "_")}.ts`)]])),
    });
    const source = join(root, "raw.txt");
    await Bun.write(source, "alpha\n");
    const bean = await context.warehouse.add({ type: "note.raw@1.0", files: { "raw.txt": source } });
    context.warehouse.sealMany([[bean.id, { chars: 6, lines: 1 }]], null, `sha256:${"0".repeat(64)}`);
    const runner = new Runner(context, snapshot, provider);
    const lines = new LineRunner(runner);
    const inputs = { raw: bean.id };
    const read = (id: string) => Bun.file(context.warehouse.blobPath(context.warehouse.get(id).payload[0].digest)).text();
    return { snapshot, runner, lines, inputs, read, close };
  } catch (error) { close(); throw error; }
}

test("compiles literal Map families and persists ordered, single-submission execution", { timeout: 60_000 }, async () => {
  const task = await fixture(["EN", "JA"]);
  try {
    const plan = compileDynamicRunPlan(task.snapshot, line);
    expect(plan.nodes.map(({ id }) => id)).toEqual([`${upperConverter}#0`, `${upperConverter}#1`, "shout#0", "shout#1", "shout"]);
    expect(plan.node("shout#0")).toMatchObject({ deps: [`${upperConverter}#0`], inputs: [{ node: `${upperConverter}#0` }], mapInstance: { owner: "shout", index: 0 } });
    expect(plan.node("shout").deps).toEqual(["shout#0", "shout#1"]);
    expect(plan.budgetItems).toContainEqual(["shout", 0.5]);

    const run = task.lines.start(line, { inputs: task.inputs, useCache: false });
    expect(task.lines.runs.plan(run.id).digest()).toBe(plan.digest());
    expect(task.lines.runs.nodes(run.id).map(({ nodeId, state }) => [nodeId, state])).toEqual(plan.nodes.map(({ id }) => [id, "WAITING"]));
    for (const job of task.lines.tick(run.id)) await task.runner.run(job);
    expect(task.lines.tick(run.id)).toHaveLength(1);
    expect(task.lines.runs.node(run.id, "shout#1").state).toBe("WAITING");
    expect(task.lines.runs.get(run.id).reservedUsd).toBe(0.5);

    const done = await task.lines.run(run.id);
    expect(done.state).toBe("SUCCEEDED");
    expect(done.reservedUsd).toBe(0);
    expect(await Promise.all(done.outputs.notes.map(task.read))).toEqual(["ALPHA:EN\n", "ALPHA:JA\n"]);
    for (const node of plan.nodes.filter(({ mapInstance }) => mapInstance)) {
      expect(task.lines.runs.jobHistory(run.id, node.id)).toHaveLength(1);
    }
  } finally { task.close(); }
});

test("rejects a literal Map overflow before creating instances", async () => {
  const task = await fixture(["EN", "JA", "DE"]);
  try {
    const run = task.lines.start(line, { inputs: task.inputs, useCache: false });
    expect(task.lines.runs.plan(run.id).nodes.map(({ id }) => id)).toEqual(["shout"]);
    expect(task.lines.tick(run.id)).toEqual([]);
    expect(task.lines.runs.get(run.id).state).toBe("FAILED");
    expect(task.lines.runs.nodes(run.id)).toMatchObject([{ nodeId: "shout", state: "FAILED", error: { code: "DYNAMIC_OVERFLOW" } }]);
  } finally { task.close(); }
});

test("compiles and executes only the original literal Map prefix when truncating", { timeout: 60_000 }, async () => {
  const task = await fixture(["EN", "JA", "DE"], { overflow: "truncate" });
  try {
    const run = task.lines.start(line, { inputs: task.inputs, useCache: false });
    const plan = task.lines.runs.plan(run.id);
    expect(plan.nodes.filter(({ kind }) => kind === "station").map(({ id }) => id)).toEqual(["shout#0", "shout#1"]);
    expect(plan.budgetItems).toContainEqual(["shout", 0.5]);
    const done = await task.lines.run(run.id);
    expect(done.state).toBe("SUCCEEDED");
    expect(await Promise.all(done.outputs.notes.map(task.read))).toEqual(["ALPHA:EN\n", "ALPHA:JA\n"]);
  } finally { task.close(); }
});

test("skips the compiled members of an unselected literal Map branch", { timeout: 60_000 }, async () => {
  const task = await fixture(["EN", "JA"], { skip: true });
  try {
    const run = task.lines.start(line, { inputs: task.inputs, useCache: false });
    expect(task.lines.tick(run.id)).toHaveLength(1);
    const states = task.lines.runs.nodes(run.id).filter(({ nodeId }) => nodeId.startsWith("shout"));
    expect(states).toHaveLength(5);
    expect(states.every(({ state, jobId }) => state === "SKIPPED" && jobId === null)).toBe(true);
    expect((await task.lines.run(run.id)).state).toBe("SUCCEEDED");
  } finally { task.close(); }
});

test("resumes a saved literal Map plan without compiled members", { timeout: 60_000 }, async () => {
  const task = await fixture(["EN", "JA"]);
  try {
    const plan = compileDynamicRunPlan(task.snapshot, line);
    const controller = plan.node("shout");
    const olderPlan = new RunPlan({ ...plan, nodes: [{ ...controller, deps: [] }] });
    const run = task.lines.runs.create(olderPlan, { inputs: task.inputs, useCache: false });
    expect(task.lines.runs.nodes(run.id).map(({ nodeId }) => nodeId)).toEqual(["shout"]);
    const done = await task.lines.run(run.id);
    expect(done.state).toBe("SUCCEEDED");
    expect(await Promise.all(done.outputs.notes.map(task.read))).toEqual(["ALPHA:EN\n", "ALPHA:JA\n"]);
  } finally { task.close(); }
});

test("reuses Map stations and converters without reserving money or submitting jobs", { timeout: 60_000 }, async () => {
  const task = await fixture(["EN", "JA"]);
  try {
    const original = task.lines.start(line, { inputs: task.inputs, useCache: false });
    const completed = await task.lines.run(original.id);
    expect(completed.state).toBe("SUCCEEDED");
    const rerun = task.lines.start(line, { inputs: task.inputs, budgetCapUsd: 0.1 });
    expect(task.lines.tick(rerun.id)).toEqual([]);
    expect(task.lines.runs.get(rerun.id)).toMatchObject({ state: "SUCCEEDED", outputs: completed.outputs, spentUsd: 0, reservedUsd: 0 });
    const first = task.lines.runs.nodes(original.id).filter(({ nodeId }) => nodeId !== "shout");
    const reused = task.lines.runs.nodes(rerun.id).filter(({ nodeId }) => nodeId !== "shout");
    expect(reused.map(({ jobId, cacheHit }) => [jobId, cacheHit])).toEqual([
      [first[0].jobId, true], [first[0].jobId, true], [first[2].jobId, true], [first[3].jobId, true],
    ]);
  } finally { task.close(); }
});
