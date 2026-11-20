import { expect, test } from "bun:test";
import { cpSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { attachedPlanNodes, compilePlan } from "../src/legacy/dynamic-plan";
import { compileDynamicRunPlan } from "../src/legacy/dynamic-switch";
import { Context } from "../src/legacy/context";
import { FactoryError } from "../src/legacy/errors";
import type { Job } from "../src/legacy/jobs";
import { LineRunner } from "../src/legacy/line-runner";
import { recordPlanResolutions } from "../src/legacy/plan-resolutions";
import { Runner } from "../src/legacy/runner";
import { RunStore } from "../src/legacy/runs";
import { LocalProvider } from "../src/legacy/sandbox";
import { loadSnapshot, Snapshot } from "../src/legacy/snapshot";

const plannerKey = "Station:planner@1";
const parent = "Line:note-plan@1";
const shout = { station: "upper@1", in: { note: "$inputs.note" } };

function plan(steps: Record<string, unknown>, metadata: Record<string, unknown> = {}) {
  return { apiVersion: "mao/v1", kind: "Plan", metadata: { name: "note-plan", ...metadata }, spec: { steps } };
}

async function fixture(planBudget: number | string = 2) {
  const root = mkdtempSync(join(tmpdir(), "mao-plan-"));
  try {
    cpSync("tests/fixtures/registry-m2", root, { recursive: true });
    const studio = join(root, "machines", "studio", "machine.yaml");
    await Bun.write(studio, (await Bun.file(studio).text()) + `
    plan:
      inputs: {task: {type: agent.task@1}}
      outputs: {plan: {type: plan.doc@1, harvest: plans/plan.json}}
      task_template: Write plans/plan.json.
`);
    await Bun.write(join(root, "types", "plan-doc.yaml"), `apiVersion: mao/v1
kind: Type
metadata: {name: plan.doc, version: "1.0"}
spec:
  payload: [{path: plan.json, media_type: application/json, required: true}]
`);
    await Bun.write(join(root, "stations", "planner.yaml"), `apiVersion: mao/v1
kind: Station
metadata: {name: planner, version: 1}
spec:
  role: planner
  machine: studio@1
  recipe: plan
  operator: {runtime: fake-agent@1, model: fake-model-1, max_turns: 5}
  inputs: {task: {type: agent.task@1, requires: [has_steps]}}
  outputs: {plan: {type: plan.doc@1}}
  limits: {timeout: 120s, budget_usd: 0.5, retries: {rejected: 1, failed: 0, mode: fresh}}
  policy: agent-default@1
  plan_bounds:
    allowed_stations: [upper@1, write@1, publish-local@1, planner@1]
    allowed_sinks: [publish-local@1]
    max_steps: 4
    max_fanout: 3
    max_depth: 1
    budget_usd: ${JSON.stringify(planBudget)}
`);
    await Bun.write(join(root, "lines", "note-plan.yaml"), `apiVersion: mao/v1
kind: Line
metadata: {name: note-plan, version: 1}
spec:
  interface:
    inputs:
      task: {type: agent.task@1, requires: [has_steps]}
      note: {type: note.text@1, requires: [non_empty]}
      raw: {type: note.raw@1, requires: [non_empty]}
  budget: {cap_usd: 2.5}
  keep: [plan.plan]
  steps:
    plan: {station: planner@1, in: {task: $inputs.task}}
`);
    const snapshot = await loadSnapshot(root);
    const run = { id: "r-plan", line: parent, epoch: snapshot.epoch, budgetCapUsd: 2.5, spentUsd: 0, reservedUsd: 0 };
    return { snapshot, run, check: (document: unknown, remainingUsd?: number) => compilePlan(snapshot, document, { plannerKey, run, remainingUsd }) };
  } finally { rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
}

test("compiles generated Plans against the frozen parent interface and converter library", async () => {
  const { snapshot, check } = await fixture();
  const document = plan({ shout: { ...shout, in: { note: "$inputs.raw" } } });
  const checked = check(document);
  expect(checked.diagnostics).toEqual([]);
  expect(checked.ok).toBe(true);
  expect(checked.plan).toMatchObject({ line: parent, epoch: snapshot.epoch });
  expect(checked.plan?.nodes.map(({ kind }) => kind)).toEqual(["converter", "station"]);
  expect(checked.resolutions).toMatchObject([{ step: "shout", port: "note", status: "convert" }]);
  expect(check(document).plan?.digest()).toBe(checked.plan?.digest());
});

test("rejects non-Plan records, nonexistent references, invalid ports, and provenance mismatches", async () => {
  const { check } = await fixture();
  const kind = check({ ...plan({ shout }), kind: "Type" });
  expect(kind.diagnostics.map(({ code }) => code)).toEqual(["PLAN_INVALID", "E-SCHEMA"]);
  const invalid = check(plan({
    ghost: { ...shout, station: "ghost@1" },
    shout: { ...shout, in: { note: "$inputs.ghost" } },
  }, { run: "r-other", generated_by: "Station:upper@1" }));
  expect(invalid.ok).toBe(false);
  expect(invalid.plan).toBeNull();
  expect(invalid.diagnostics.map(({ code }) => code)).toContain("PLAN_INVALID");
  expect(invalid.diagnostics.map(({ code }) => code)).toContain("E-PORT");
  for (const issue of invalid.diagnostics) expect(invalid.feedback).toContain(issue.message);
});

test("enforces Plan allowlists, nesting, expanded converter steps, and remaining Run budget", async () => {
  const { check } = await fixture();
  const write = plan({ draft: { station: "write@1", in: { task: "$inputs.task" } } });
  expect(check(write, 1).ok).toBe(true);
  expect(check(write, 0.8).feedback).toContain("PLAN_BUDGET");

  const nested = check(plan({ again: { station: "planner@1", in: { task: "$inputs.task" } } }));
  expect(nested.feedback).toContain("Plan 不能再产出 Plan");
  const wrongStation = check(plan({ quote: { station: "quote@1", in: { note: "$inputs.note" } } }));
  expect(wrongStation.feedback).toContain("allowed_stations");
  const expanded = check(plan({
    first: { ...shout, in: { note: "$inputs.raw" } },
    second: { ...shout, in: { note: "$inputs.raw" } },
    third: { ...shout, in: { note: "$inputs.raw" } },
  }));
  expect(expanded.feedback).toContain("展开 6 步");

  const fanout = check(plan({ shout: { ...shout, map: { over: ["en", "ja", "de", "fr"], as: "lang", max_items: 4 } } }));
  expect(fanout.feedback).toContain("超过有效上限 3");
});

test("rejects a planner budget template at Registry validation", async () => {
  try {
    await fixture("{{ run.budget.remaining_usd }}");
    throw new Error("planner with a budget template unexpectedly loaded");
  } catch (error) {
    if (!(error instanceof FactoryError)) throw error;
    expect(error.code).toBe("REGISTRY_INVALID");
    const diagnostics = error.detail.diags as Array<{ code: string; subject: string; path: string }>;
    expect(diagnostics.find(({ code, subject }) => code === "E-SCHEMA" && subject === plannerKey)?.path).toBe("spec.plan_bounds.budget_usd");
  }
});

test("returns a missing conversion diagnostic and the unresolved frozen contract", async () => {
  const { check } = await fixture();
  const checked = check(plan({ publish: { station: "publish-local@1", in: { note: "$inputs.task" }, params: { dir: "/tmp/plan" } } }));
  expect(checked.plan).toBeNull();
  expect(checked.feedback).toContain("E-NO-CONVERTER");
  expect(checked.resolutions).toMatchObject([{ step: "publish", port: "note", status: "no-converter" }]);
});

test("appends new runtime resolutions once and preserves a conflicting sidecar", async () => {
  const { snapshot, check } = await fixture();
  const root = mkdtempSync(join(tmpdir(), "mao-plan-resolution-"));
  const context = Context.open(root);
  try {
    const checked = check(plan({ shout: { ...shout, in: { note: "$inputs.raw" } } }));
    const resolutions = snapshot.result.resolutions.filter((row) => row.from !== "note.raw@1" || row.to !== "note.text@1");
    const runtimeOnly = new Snapshot(snapshot.root, snapshot.epoch, snapshot.docs, { ...snapshot.result, resolutions });
    const directory = join(root, "epochs", snapshot.epoch);
    const path = join(directory, "resolution-runtime.json");
    mkdirSync(directory, { recursive: true });
    await Bun.write(path, "[]");
    recordPlanResolutions(context, runtimeOnly, checked.resolutions);
    const initial = await Bun.file(path).text();
    recordPlanResolutions(context, runtimeOnly, checked.resolutions);
    expect(await Bun.file(path).text()).toBe(initial);
    const existing = JSON.parse(initial);
    expect(existing).toMatchObject([{ from: "note.raw@1", to: "note.text@1", origin: "runtime", chain: ["note-to-text@1"] }]);
    expect(() => recordPlanResolutions(context, snapshot, checked.resolutions)).toThrow("Planner 转换解析表不一致");
    existing[0].chain = ["other@1"];
    const conflicting = JSON.stringify(existing);
    await Bun.write(path, conflicting);
    expect(() => recordPlanResolutions(context, runtimeOnly, checked.resolutions)).toThrow("Planner 转换解析表不一致");
    expect(await Bun.file(path).text()).toBe(conflicting);
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

test("rejects wiring the planner Plan port into an ordinary station", async () => {
  const { snapshot } = await fixture();
  const document = snapshot.doc(parent);
  document.spec.steps.read = { station: "upper@1", in: { note: "plan.plan" } };
  const badShape = new Snapshot(snapshot.root, snapshot.epoch, { ...snapshot.docs, [parent]: document }, snapshot.result);
  try {
    compileDynamicRunPlan(badShape, parent);
    throw new Error("planner Plan port unexpectedly accepted as a station input");
  } catch (error) {
    if (!(error instanceof FactoryError)) throw error;
    expect(error.code).toBe("PLAN_SHAPE");
    expect(error.detail).toMatchObject({ step: "read", port: "note", source: "plan.plan" });
  }
});

test("qualifies generated dependencies and switch branches under their owning planner", async () => {
  const { check } = await fixture();
  const checked = check(plan({
    choose: { switch: { on: "$inputs.note", cases: [{ when: "true", goto: "shout" }], default: "fallback" } },
    shout,
    fallback: shout,
  }));
  expect(checked.diagnostics).toEqual([]);
  const nodes = attachedPlanNodes("plan", checked.plan!);
  expect(nodes[0]).toMatchObject({
    id: "plan/choose", deps: ["plan"],
    switch: { on: { source: "input", node: "$inputs", port: "note" }, cases: [{ goto: "plan/shout" }], default: "plan/fallback" },
  });
  expect(nodes.find((node) => node.id === "plan/shout")).toMatchObject({
    step: "plan/shout", deps: ["plan/choose"], inputs: [{ source: "input", outPort: "note" }],
  });
});

test("publishes generated nodes and their immutable attachment together", async () => {
  const { snapshot, check } = await fixture();
  const root = mkdtempSync(join(tmpdir(), "mao-plan-attachment-"));
  const context = Context.open(root);
  try {
    const store = new RunStore(context.db, context.clock, context.ids);
    const original = compileDynamicRunPlan(snapshot, parent);
    const run = store.create(original, { inputs: { task: "b-task", note: "b-note", raw: "b-raw" } });
    store.transition(run.id, "RUNNING");
    const node = store.submitNode(run.id, "plan", { params: {}, reservedUsd: 0.5 }, () => ({ id: "j-planner" }));
    const job: Job = {
      id: "j-planner", state: "SEALED", phase: "execute", station: plannerKey, epoch: snapshot.epoch,
      params: {}, inputs: {}, outputs: { plan: ["b-plan"] }, costUsd: 0.25, attempt: 1,
      createdAt: node.updatedAt, updatedAt: node.updatedAt,
    };
    const generated = check(plan({ draft: { station: "write@1", in: { task: "$inputs.task" } } })).plan!;
    store.attachPlannerOutput(store.get(run.id), node, job, "b-plan", generated);
    expect(store.plan(run.id).digest()).toBe(run.planDigest);
    expect(store.executionPlan(run).node("plan/draft").deps).toEqual(["plan"]);
    expect(store.nodes(run.id).map(({ nodeId, state }) => [nodeId, state])).toEqual([["plan", "DONE"], ["plan/draft", "WAITING"]]);
    expect(store.get(run.id)).toMatchObject({ spentUsd: 0.25, reservedUsd: 1 });
    expect(store.events(run.id).filter(({ kind }) => kind === "run.plan")).toMatchObject([{ data: { plan: "b-plan", digest: generated.digest() } }]);
    store.submitNode(run.id, "plan/draft", { params: {}, reservedUsd: 1 }, () => ({ id: "j-draft" }));
    expect(store.get(run.id).reservedUsd).toBe(1);
    store.settleNode(run.id, "plan/draft", { id: "j-draft", state: "SEALED", costUsd: 0.5, outputs: {} });
    expect(store.get(run.id)).toMatchObject({ spentUsd: 0.75, reservedUsd: 0 });
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

async function runWorld(documents: unknown[], note = "quiet\n") {
  const { snapshot } = await fixture();
  const root = mkdtempSync(join(tmpdir(), "mao-run-plan-"));
  const context = Context.open(root);
  const close = () => { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); };
  try {
    const command = (file: string) => [Bun.argv[0], join(import.meta.dir, `../../tests/fakes/${file}`)];
    const provider = new LocalProvider({
      "/opt/operator/bin/fake-agent": command("fake_agent.ts"),
      "count-text": command("tools/count_text.ts"), "upper-text": command("tools/upper_text.ts"),
    });
    const add = async (type: string, file: string, text: string, facts: Record<string, unknown>) => {
      const path = join(root, file);
      await Bun.write(path, text);
      const bean = await context.warehouse.add({ type, files: { [file]: path } });
      context.warehouse.sealMany([[bean.id, facts]], null, `sha256:${"0".repeat(64)}`);
      return bean.id;
    };
    const write = (document: unknown) => [{ do: "write", path: "plans/plan.json", text: JSON.stringify(document) }];
    const task = JSON.stringify({ attempts: Object.fromEntries(documents.map((document, index) => [index + 1, write(document)])), default: write(documents.at(-1)) });
    const inputs = {
      task: await add("agent.task@1.0", "task.json", task, { steps: 1 }),
      note: await add("note.text@1.0", "note.txt", note, { chars: note.length, lines: 1, has_lower: true }),
      raw: await add("note.raw@1.0", "raw.txt", "quiet\n", { chars: 6, lines: 1 }),
    };
    const runner = new Runner(context, snapshot, provider);
    const lines = new LineRunner(runner);
    const restart = () => new LineRunner(new Runner(context, snapshot, provider));
    return { context, runner, lines, inputs, restart, close };
  } catch (error) { close(); throw error; }
}

test("retries rejected Plan output then seals and executes its child after restart", { timeout: 60_000 }, async () => {
  const world = await runWorld([plan({ quote: { ...shout, station: "quote@1" } }), plan({ shout })]);
  try {
    const run = world.lines.start(parent, { inputs: world.inputs });
    const [plannerId] = world.lines.tick(run.id);
    const planner = await world.runner.run(plannerId);
    expect(planner).toMatchObject({ attempt: 2, state: "SEALED" });
    expect(world.runner.jobs.countTransitions(planner.id, "REJECTED")).toBe(1);
    const bean = world.context.warehouse.get(planner.outputs.plan[0]);
    expect(bean.sealed && bean.type.startsWith("plan.doc@1")).toBe(true);
    const lines = world.restart();
    expect(lines.runs.plan(run.id).digest()).toBe(run.planDigest);
    const result = await lines.run(run.id);
    const child = lines.runs.node(run.id, "plan/shout");
    expect(result.state).toBe("SUCCEEDED");
    expect(child.state).toBe("DONE");
    expect(world.runner.jobs.get(child.jobId!).epoch).toBe(run.epoch);
    const [file] = world.context.warehouse.get(child.outputs.note[0]).payload;
    expect(await Bun.file(world.context.warehouse.blobPath(file.digest)).text()).toBe("QUIET\n");
    expect(lines.runs.events(run.id).filter(({ kind }) => kind === "run.plan")).toMatchObject([{ data: { plan: bean.id, node: "plan" } }]);
  } finally { world.close(); }
});

test("reports rejected bounds in both attempts without a sealed Plan or attached nodes", { timeout: 60_000 }, async () => {
  const document = plan(Object.fromEntries(Array.from({ length: 5 }, (_, index) => [`step${index}`, shout])));
  const world = await runWorld([document]);
  try {
    const run = world.lines.start(parent, { inputs: world.inputs });
    expect((await world.lines.run(run.id)).state).toBe("FAILED");
    expect(world.lines.runs.nodes(run.id).map(({ nodeId, state }) => [nodeId, state])).toEqual([["plan", "FAILED"]]);
    const job = world.runner.jobs.get(world.lines.runs.node(run.id, "plan").jobId!);
    expect(job).toMatchObject({ attempt: 2, state: "DEAD", outputs: {} });
    expect(job.feedback).toContain("PLAN_BOUNDS");
  } finally { world.close(); }
});

test("merges missing conversion requests without blocking planner retries", { timeout: 60_000 }, async () => {
  const invalid = plan({ publish: { station: "publish-local@1", in: { note: "$inputs.task" }, params: { dir: "/tmp/plan" } } });
  const world = await runWorld([invalid]);
  try {
    const run = world.lines.start(parent, { inputs: world.inputs });
    expect((await world.lines.run(run.id)).state).toBe("FAILED");
    const job = world.runner.jobs.get(world.lines.runs.node(run.id, "plan").jobId!);
    expect(job).toMatchObject({ attempt: 2, state: "DEAD" });
    expect(job.feedback).toContain("E-NO-CONVERTER");
    expect(world.context.db.query("SELECT from_type, to_type, status, blocked_runs FROM conversion_requests").all()).toEqual([
      { from_type: "agent.task@1", to_type: "note.text@1", status: "open", blocked_runs: "[]" },
    ]);
  } finally { world.close(); }
});

test("propagates child failure and rejects Planner jobs submitted without a parent Run", { timeout: 60_000 }, async () => {
  const world = await runWorld([plan({ shout })], "#fail\n");
  try {
    const orphan = world.runner.submit(plannerKey, { task: world.inputs.task }, {});
    expect(await world.runner.run(orphan.id)).toMatchObject({ state: "DEAD", error: { code: "RUN_PLAN_INVALID", detail: { reason: "planner-outside-run" } } });
    const run = world.lines.start(parent, { inputs: world.inputs });
    expect(await world.lines.run(run.id)).toMatchObject({ state: "FAILED", error: { code: "NODE_FAILED", node: "plan/shout" } });
    expect(world.lines.runs.node(run.id, "plan").state).toBe("DONE");
    expect(world.lines.runs.node(run.id, "plan/shout").state).toBe("FAILED");
  } finally { world.close(); }
});
