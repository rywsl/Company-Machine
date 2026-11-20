import { expect, test } from "bun:test";
import { cpSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Context, LineRunner, LocalProvider, Runner, Snapshot, compileRunPlan, loadSnapshot } from "../src/legacy";
import { compileDynamicRunPlan } from "../src/legacy/dynamic-switch";
import { TriggerEngine } from "../src/legacy/triggers";
import { runStationCli } from "../src/station-cli";

const fixture = "tests/fixtures/registry-m2";
const commands = Object.fromEntries(["count-text", "upper-text"].map((name) => [
  name, [Bun.argv[0], join(import.meta.dir, `../../tests/fakes/tools/${name.replaceAll("-", "_")}.ts`)],
]));
const line = "note-switch@1";

const station = (source: string) => ({ station: "upper@1", in: { note: source } });
const loud = { when: `has(bean.meta.title) && bean.meta.title == "loud"`, goto: "loud" };

async function registry(root: string, cases = [loud], deriveFromJob = false) {
  const path = join(root, "registry");
  cpSync(fixture, path, { recursive: true });
  const origin = deriveFromJob ? "source.note" : "$inputs.note";
  const document = {
    apiVersion: "mao/v1", kind: "Line", metadata: { name: "note-switch", version: 1 },
    spec: {
      interface: { inputs: { note: { type: "note.text@1", requires: ["non_empty"] } } }, budget: { cap_usd: 1 },
      trigger: [{ on: "bean", type: "note.text@1", label: "line=note-switch" }],
      keep: ["loud.note", "calm.note"],
      steps: { ...(deriveFromJob ? { source: station("$inputs.note") } : {}),
        choose: { switch: { on: origin, cases, default: "calm" } }, loud: station(origin), calm: station(origin) },
    },
  };
  writeFileSync(join(path, "lines", "note-switch.yaml"), Bun.YAML.stringify(document));
  return loadSnapshot(path);
}

async function seal(context: Context, text: string, title: string) {
  const file = join(context.root, "material.txt");
  await Bun.write(file, text);
  const bean = await context.warehouse.add({ type: "note.text@1.0", meta: { title }, files: { "note.txt": file } });
  context.warehouse.sealMany([[bean.id, { chars: text.trim().length, lines: 1, has_lower: true }]], null, `sha256:${"0".repeat(64)}`);
  return bean.id;
}

test("compiles ordered switch cases and preserves the static plan document", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-switch-plan-"));
  try {
    const cases = [{ when: "true", goto: "loud" }, { when: "true", goto: "calm" }];
    const snapshot = await registry(root, cases);
    const plan = compileDynamicRunPlan(snapshot, line);
    expect(plan.line.endsWith(line)).toBe(true);
    expect(plan.nodes.map(({ id, kind }) => ({ id, kind }))).toEqual([{ id: "choose", kind: "switch" }, { id: "calm", kind: "station" }, { id: "loud", kind: "station" }]);
    expect(plan.node("choose").switch).toMatchObject({ cases, default: "calm" });
    expect(plan.node("loud").deps).toEqual(["choose"]);
    const original = await loadSnapshot("spec/examples/registry");
    expect(compileDynamicRunPlan(original, "explainer-multilang@5").toJSON()).toEqual(compileRunPlan(original, "explainer-multilang@5").toJSON());
    let staticFailure: unknown;
    try { compileRunPlan(snapshot, line); } catch (error) { staticFailure = error; }
    expect(staticFailure).toMatchObject({ code: "LINE_UNSUPPORTED" });
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("runs one selected branch and skips the alternative with no job", { timeout: 60_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-switch-exec-"));
  const context = Context.open(root);
  try {
    const snapshot = await registry(root);
    const lines = new LineRunner(new Runner(context, snapshot, new LocalProvider(commands)));
    for (const [text, title, other] of [["hello\n", "loud", "calm"], ["hush\n", "calm", "loud"]]) {
      const run = lines.start(line, { inputs: { note: await seal(context, text, title) }, useCache: false });
      expect((await lines.run(run.id)).state).toBe("SUCCEEDED");
      expect(lines.runs.node(run.id, "choose")).toMatchObject({ state: "DONE", jobId: null, params: { selected: title } });
      expect(lines.runs.node(run.id, title).state).toBe("DONE");
      expect(lines.runs.node(run.id, other)).toMatchObject({ state: "SKIPPED", jobId: null });
    }
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

test("commits the first true choice, skip and submission in one tick across restart", { timeout: 60_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-switch-restart-"));
  let context = Context.open(root);
  try {
    const snapshot = await registry(root, [{ when: "true", goto: "loud" }, { when: "true", goto: "calm" }]);
    let lines = new LineRunner(new Runner(context, snapshot, new LocalProvider(commands)));
    const run = lines.start(line, { inputs: { note: await seal(context, "hello\n", "calm") }, useCache: false });
    context.db.exec(`CREATE TRIGGER fail_switch_skip BEFORE UPDATE OF state ON run_nodes
      WHEN NEW.state = 'SKIPPED' BEGIN SELECT RAISE(ABORT, 'forced skip failure'); END;`);
    const before = lines.runs.events(run.id);
    expect(() => lines.tick(run.id)).toThrow("forced skip failure");
    expect(lines.runs.get(run.id).state).toBe("PENDING");
    expect(lines.runs.events(run.id)).toEqual(before);
    expect(lines.runs.nodes(run.id).every((node) => node.state === "WAITING" && node.jobId === null)).toBe(true);
    context.db.exec("DROP TRIGGER fail_switch_skip");
    expect(lines.tick(run.id)).toHaveLength(1);
    expect(lines.runs.node(run.id, "choose").params?.selected).toBe("loud");
    context.close();
    context = Context.open(root);
    lines = new LineRunner(new Runner(context, snapshot, new LocalProvider(commands)));
    expect(lines.runs.plan(run.id).node("choose").switch?.cases).toHaveLength(2);
    expect((await lines.run(run.id)).state).toBe("SUCCEEDED");
    expect(lines.runs.node(run.id, "calm").state).toBe("SKIPPED");
    expect(lines.runs.events(run.id).filter((event) => event.data.node === "choose")).toHaveLength(1);
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

test("leaves a producer-backed switch waiting and fails without selecting a default on evaluation error", { timeout: 60_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-switch-eval-"));
  const context = Context.open(root);
  try {
    const snapshot = await registry(root, [{ when: "bean.facts.chars / 0 > 0", goto: "loud" }], true);
    const runner = new Runner(context, snapshot, new LocalProvider(commands));
    const lines = new LineRunner(runner);
    const run = lines.start(line, { inputs: { note: await seal(context, "hello\n", "loud") }, useCache: false });
    const [job] = lines.tick(run.id);
    expect(job).toBeString();
    expect(lines.runs.node(run.id, "choose").state).toBe("WAITING");
    await runner.run(job);
    expect(lines.tick(run.id)).toEqual([]);
    expect(lines.runs.get(run.id).state).toBe("FAILED");
    expect(lines.runs.node(run.id, "choose")).toMatchObject({ state: "FAILED", jobId: null, error: { code: "SWITCH_EVAL", detail: { step: "choose", index: 0 } } });
    expect(lines.runs.node(run.id, "calm").jobId).toBeNull();
    expect(lines.runs.node(run.id, "loud").jobId).toBeNull();
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

test("scopes implicit converters to their branch while preserving normal wire references", async () => {
  const snapshot = await loadSnapshot("spec/examples/registry");
  const docs = structuredClone(snapshot.docs);
  const lineKey = "Line:explainer-multilang@5";
  const steps = docs[lineKey].spec.steps;
  docs["Station:explainer@3"].spec.outputs.subtitle.type = "subtitle.vtt@1";
  steps.dub_ja = structuredClone(steps.dub_en);
  steps.dub_ja.params.target_language = "ja";
  steps.choose = { switch: { on: "explainer.subtitle", cases: [{ when: "true", goto: "dub_en" }], default: "dub_ja" } };
  const plan = compileDynamicRunPlan(new Snapshot(snapshot.root, snapshot.epoch, docs, snapshot.result), lineKey);
  const en = "dub_en.in.subtitle~subs-convert@1";
  const ja = "dub_ja.in.subtitle~subs-convert@1";
  expect(plan.node(en).deps).toEqual(["choose", "explainer"]);
  expect(plan.node(ja).deps).toEqual(["choose", "explainer"]);
  expect(plan.node("dub_en").inputs).toContainEqual({ port: "subtitle", source: "node", node: en, outPort: "out" });
  expect(plan.node("dub_ja").inputs).toContainEqual({ port: "subtitle", source: "node", node: ja, outPort: "out" });
  expect(plan.node("vertical").deps).toEqual(["dub_en"]);
  expect(plan.node("choose").deps).toEqual(["explainer"]);
});

test("uses the dynamic compiler through factory line plan and run", { timeout: 60_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-switch-cli-"));
  const context = Context.open(root);
  const log = console.log;
  const outputs: unknown[] = [];
  try {
    await registry(root);
    const note = await seal(context, "hello\n", "loud");
    const commandMap = join(root, "commands.json");
    await Bun.write(commandMap, JSON.stringify(commands));
    const opts = ["--root", root, "--registry", join(root, "registry"), "--provider", "local", "--local-commands", commandMap];
    console.log = (value) => outputs.push(JSON.parse(String(value)));
    expect(await runStationCli([...opts, "line", "plan", line])).toBe(0);
    expect(await runStationCli([...opts, "line", "run", line, "--in", `note=${note}`, "--no-cache"])).toBe(0);
    expect(outputs[0]).toMatchObject({ nodes: [{ kind: "switch" }, { kind: "station" }, { kind: "station" }] });
    expect(outputs[1]).toMatchObject({ state: "SUCCEEDED" });
  } finally { console.log = log; context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

test("creates a switch plan when a labeled Bean triggers a Line", { timeout: 60_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-switch-trigger-"));
  const context = Context.open(root);
  try {
    const snapshot = await registry(root);
    const note = await seal(context, "hush\n", "calm");
    context.warehouse.label(note, "line", "note-switch", "human:test");
    const lines = new LineRunner(new Runner(context, snapshot, new LocalProvider(commands)));
    const trigger = new TriggerEngine(context, snapshot, lines);
    const runs = trigger.process();
    expect(runs).toHaveLength(1);
    expect(lines.runs.plan(runs[0]).node("choose").kind).toBe("switch");
    expect((await lines.run(runs[0])).state).toBe("SUCCEEDED");
    expect(lines.runs.node(runs[0], "choose").params?.selected).toBe("calm");
    expect(trigger.process()).toEqual([]);
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});
