import { expect, test } from "bun:test";
import { cpSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { main } from "../src/cli";
import { Context, FactoryError, LocalProvider, Runner, loadSnapshot } from "../src/legacy";
import type { Run, RunNode } from "../src/legacy/runs";

const notePort = { type: "note.text@1", requires: ["non_empty"] };
const pair = { note: notePort, reference: { ...notePort, requires: ["shouting"] } };
const list = { note: notePort, parts: { type: "list<note.text@1>" } };
const params = { length: { type: "int", default: 3 } };
const tools = {
  "count-text": [Bun.argv[0], resolve("tests/fakes/tools/count_text.ts")],
  "upper-text": [Bun.argv[0], resolve("tests/fakes/tools/upper_text.ts")],
  "pull-notes": [Bun.argv[0], resolve("tests/fakes/tools/pull_notes.ts")],
};
const admit = (index: number) => ({ port: "", index, reason: "admit", detail: `admit[${index}]`, bean_id: "" });

function expectIntakeRejected(submit: () => unknown, problems: Array<ReturnType<typeof admit>>): void {
  try { submit(); }
  catch (error) {
    if (!(error instanceof FactoryError)) throw error;
    expect(error.code).toBe("INTAKE_REJECTED");
    expect(error.detail.problems).toEqual(problems);
    return;
  }
  throw new Error("Expected intake rejection");
}

type JobResult = { state: string; outputs: Record<string, string[]> };
type JobEvidence = { jobs: Array<{ id: string }>; created: Array<{ job_id: string }> };

async function admitWorld(run: (world: {
  cli<T>(code: number, ...args: string[]): Promise<T>;
  note(body: string, type?: "note.text" | "note.raw"): Promise<string>;
  runner: Runner;
  context: Context;
  evidence(): JobEvidence;
}) => Promise<void>): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), "mao-admit-"));
  const registry = join(root, "registry");
  const source = join(root, "upstream");
  const commands = join(root, "commands.json");
  const factory = join(root, "factory");
  try {
    mkdirSync(source, { recursive: true });
    cpSync("tests/fixtures/registry-m2", registry, { recursive: true });
    await Bun.write(commands, JSON.stringify(tools));
    await Bun.write(join(source, "source.txt"), "abc");
    const machinery = join(registry, "machines", "toolbox", "machine.yaml");
    const recipes = Object.entries({
      "admit-pair": { inputs: pair },
      "admit-params": { inputs: pair, params },
      "admit-list": { inputs: list },
    });
    const output = { note: { type: "note.text@1", guarantees: ["non_empty", "shouting"] } };
    let machine = (await Bun.file(machinery).text()).trimEnd() + "\n";
    for (const [name, spec] of recipes) {
      machine += `    ${name}: ${JSON.stringify({
        ...spec,
        inputs: Object.fromEntries(Object.entries(spec.inputs).map(([port, { type }]) => [port, { type }])),
        outputs: { note: { type: output.note.type } },
        run: ["upper-text", "/factory/in/note/note.txt", "/factory/out/note/note.txt"],
      })}\n`;
    }
    await Bun.write(machinery, machine);
    const stationAdmit: Record<string, string[]> = {
      "admit-pair": [
        "inputs.note.facts.chars == inputs.reference.facts.chars",
        "inputs.note.facts.lines == inputs.reference.facts.lines",
      ],
      "admit-params": [
        "inputs.note.facts.chars == params.length",
        "inputs.reference.facts.chars == params.length",
      ],
      "admit-list": [
        "inputs.parts.all(p, p.facts.lines == inputs.note.facts.lines)",
        "size(inputs.parts) <= inputs.note.facts.chars",
      ],
    };
    for (const [name, spec] of recipes) {
      await Bun.write(join(registry, "stations", `${name}.yaml`), JSON.stringify({
        apiVersion: "mao/v1",
        kind: "Station",
        metadata: { name, version: 1 },
        spec: {
          ...spec,
          role: "transform",
          machine: "toolbox@1",
          recipe: name,
          operator: { runtime: "script@1" },
          outputs: output,
          admit: stationAdmit[name],
          limits: { timeout: "60s", budget_usd: 0, retries: { rejected: 0, failed: 0, mode: "fresh" } },
          policy: "tool-default@1",
        },
      }));
    }
    for (const fromSource of [false, true]) {
      const name = fromSource ? "admit-from-source" : "admit-pair";
      await Bun.write(join(registry, "lines", `${name}.yaml`), JSON.stringify({
        apiVersion: "mao/v1",
        kind: "Line",
        metadata: { name, version: 1 },
        spec: {
          interface: { inputs: fromSource ? { reference: pair.reference } : pair },
          budget: { cap_usd: 1 },
          steps: {
            ...(fromSource ? { pull: { station: "pull-notes@1", params: { from_dir: source } } } : {}),
            match: {
              station: "admit-pair@1",
              in: { note: fromSource ? "pull.note" : "$inputs.note", reference: "$inputs.reference" },
            },
          },
          keep: fromSource ? ["pull.note", "match.note"] : ["match.note"],
        },
      }));
    }

    async function cli<T>(code: number, ...args: string[]): Promise<T> {
      const argv = [
        "--root", factory,
        "--registry", registry, "--provider", "local", "--local-commands", commands, "--json", ...args,
      ];
      const output: string[] = [];
      const errors: string[] = [];
      const { log, error } = console;
      try {
        console.log = (...values: unknown[]) => output.push(values.join(" "));
        console.error = (...values: unknown[]) => errors.push(values.join(" "));
        const actual = await main(argv);
        if (actual !== code || output.length !== 1) {
          throw new Error(`${args.join(" ")} expected exit ${code} and JSON, got exit ${actual}\n${[...output, ...errors].join("\n")}`);
        }
        return JSON.parse(output[0]) as T;
      } finally {
        console.log = log;
        console.error = error;
      }
    }

    const context = Context.open(factory);
    try {
      const runner = new Runner(context, await loadSnapshot(registry), new LocalProvider(tools));
      await run({
        cli, context, runner,
        async note(body, type = "note.text") {
          const path = join(root, "material.txt");
          await Bun.write(path, body);
          const { id } = await cli<{ id: string }>(0, "bean", "import", "--type", `${type}@1`, "--file",
            `${type === "note.raw" ? "raw.txt" : "note.txt"}=${path}`);
          return id;
        },
        evidence: () => ({
          jobs: context.db.query<{ id: string }>("SELECT id FROM jobs ORDER BY id").all(),
          created: context.db.query<{ job_id: string }>("SELECT job_id FROM job_events WHERE kind = 'job.created' ORDER BY job_id").all(),
        }),
      });
    } finally { context.close(); }
  } finally { rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
}

test("job run accepts matching measured facts and seals the output", { timeout: 30_000 }, async () => {
  await admitWorld(async ({ cli, context, note }) => {
    const text = await note("abc");
    const reference = await note("XYZ");
    const job = await cli<JobResult>(0, "job", "run", "admit-pair@1", "--input", `note=${text}`, "--input", `reference=${reference}`);
    expect(job.state).toBe("SEALED");
    const [output] = job.outputs.note;
    expect(context.warehouse.get(output)).toMatchObject({ sealed: true, facts: { chars: 3, lines: 1, has_lower: false } });
  });
});

test("job run reports the precise JSON admission error without creating a job", { timeout: 30_000 }, async () => {
  await admitWorld(async ({ cli, note, evidence }) => {
    const text = await note("abc");
    const reference = await note("XY");
    const result = await cli(1, "job", "run", "admit-pair@1", "--input", `note=${text}`, "--input", `reference=${reference}`);
    expect(result).toEqual({ error: { code: "INTAKE_REJECTED", message: "来料检验未通过", detail: { problems: [admit(0)] } } });
    expect(evidence()).toEqual({ jobs: [], created: [] });
  });
});

test("Runner reports port problems before evaluating admission rules", { timeout: 30_000 }, async () => {
  await admitWorld(async ({ runner, note, evidence }) => {
    const text = await note("abc");
    const lower = await note("xy");
    const raw = await note("RAW", "note.raw");
    const reference = await note("XY");
    expectIntakeRejected(() => runner.submit("admit-pair@1", { note: text, reference: lower }, {}), [
      { port: "reference", index: 0, reason: "requires", detail: "shouting", bean_id: lower },
    ]);
    expectIntakeRejected(() => runner.submit("admit-pair@1", { note: raw, reference }, {}), [
      { port: "note", index: 0, reason: "type", detail: "类型不兼容", bean_id: raw },
    ]);
    expect(evidence()).toEqual({ jobs: [], created: [] });
  });
});

test("Runner evaluates admission with default and explicit parameters", { timeout: 30_000 }, async () => {
  await admitWorld(async ({ runner, note, evidence }) => {
    const text = await note("abc");
    const reference = await note("XYZ");
    const inputs = { note: text, reference };
    expect(runner.submit("admit-params@1", inputs, {}).params).toEqual({ length: 3 });
    const before = evidence();
    expectIntakeRejected(() => runner.submit("admit-params@1", inputs, { length: 2 }), [admit(0), admit(1)]);
    expect(evidence()).toEqual(before);
    expect(runner.submit("admit-params@1", { note: await note("ab"), reference: await note("XY") }, { length: 2 }).params)
      .toEqual({ length: 2 });
  });
});

test("Runner evaluates admission across a list port and preserves input order", { timeout: 30_000 }, async () => {
  await admitWorld(async ({ runner, note, evidence }) => {
    const text = await note("ab");
    const first = await note("A");
    const second = await note("B");
    const multiline = await note("A\nB");
    expect(runner.submit("admit-list@1", { note: text, parts: [first, second] }, {}).inputs)
      .toEqual({ note: [text], parts: [first, second] });
    const before = evidence();
    expectIntakeRejected(() => runner.submit("admit-list@1", { note: text, parts: [first, multiline] }, {}), [admit(0)]);
    expectIntakeRejected(() => runner.submit("admit-list@1", { note: text, parts: [first, second, first] }, {}), [admit(1)]);
    expect(evidence()).toEqual(before);
  });
});

test("line run records the failed admission on the node before creating a job", { timeout: 30_000 }, async () => {
  await admitWorld(async ({ cli, note, evidence }) => {
    const text = await note("abc");
    const bad = await note("XY");
    const good = await note("XYZ");
    const failed = await cli<Run>(1, "line", "run", "admit-pair@1", "--in", `note=${text}`, "--in", `reference=${bad}`);
    expect(failed.state).toBe("FAILED");
    expect(await cli<RunNode[]>(0, "line", "nodes", failed.id)).toMatchObject([{
      nodeId: "match", state: "FAILED", jobId: null, error: { code: "INTAKE_REJECTED", detail: { problems: [admit(0)] } },
    }]);
    expect(evidence()).toEqual({ jobs: [], created: [] });
    const passed = await cli<Run>(0, "line", "run", "admit-pair@1", "--in", `note=${text}`, "--in", `reference=${good}`);
    expect(passed.state).toBe("SUCCEEDED");
    expect(await cli<RunNode[]>(0, "line", "nodes", passed.id)).toMatchObject([{ nodeId: "match", state: "DONE" }]);
  });
});

test("Source material joins an existing Bean and stays sealed after downstream admission fails", { timeout: 30_000 }, async () => {
  await admitWorld(async ({ cli, context, note, evidence }) => {
    const good = await note("XYZ");
    const bad = await note("XY\nZ");
    const passed = await cli<Run>(0, "line", "run", "admit-from-source@1", "--in", `reference=${good}`);
    expect(passed.state).toBe("SUCCEEDED");
    const nodes = Object.fromEntries((await cli<RunNode[]>(0, "line", "nodes", passed.id)).map((node) => [node.nodeId, node]));
    expect(nodes.pull.state).toBe("DONE");
    expect(nodes.match.state).toBe("DONE");
    const [source] = nodes.pull.outputs.note;
    const [output] = nodes.match.outputs.note;
    expect(context.warehouse.get(source)).toMatchObject({ sealed: true, facts: { chars: 3, lines: 1, has_lower: true } });
    expect(context.warehouse.get(output)).toMatchObject({ sealed: true, facts: { chars: 3, lines: 1, has_lower: false } });

    const before = evidence();
    const failed = await cli<Run>(1, "line", "run", "admit-from-source@1", "--in", `reference=${bad}`);
    expect(failed.state).toBe("FAILED");
    const after = Object.fromEntries((await cli<RunNode[]>(0, "line", "nodes", failed.id)).map((node) => [node.nodeId, node]));
    expect(after.pull).toMatchObject({ state: "DONE", outputs: { note: [source] } });
    expect(after.match).toMatchObject({
      state: "FAILED", jobId: null, error: { code: "INTAKE_REJECTED", detail: { problems: [admit(1)] } },
    });
    expect(context.warehouse.get(source).sealed).toBe(true);
    expect(evidence().jobs.filter(({ id }) => !before.jobs.some((job) => job.id === id))).toEqual([{ id: after.pull.jobId! }]);
    expect(evidence().created.filter(({ job_id }) => !before.created.some((event) => event.job_id === job_id)))
      .toEqual([{ job_id: after.pull.jobId! }]);
  });
});
