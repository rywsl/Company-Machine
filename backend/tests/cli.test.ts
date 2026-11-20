import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { cpSync, mkdirSync, rmSync } from "node:fs";
import { join, resolve } from "node:path";
import { main } from "../src/cli";
import { SqliteStore } from "../src/db";
import { Context } from "../src/legacy/context";

async function capture(run: () => Promise<number>): Promise<{ code: number; output: string[]; errors: string[] }> {
  const output: string[] = [];
  const errors: string[] = [];
  const log = console.log;
  const error = console.error;
  console.log = (...values: unknown[]) => output.push(values.join(" "));
  console.error = (...values: unknown[]) => errors.push(values.join(" "));
  try {
    return { code: await run(), output, errors };
  } finally {
    console.log = log;
    console.error = error;
  }
}

describe("Bun factory CLI", () => {
  test("provides scoped help for active business commands", async () => {
    const project = await capture(() => main(["project", "help"]));
    const business = await capture(() => main(["business", "help"]));
    const foreman = await capture(() => main(["foreman", "help"]));
    const runtime = await capture(() => main(["runtime", "help"]));
    expect(project.code).toBe(0);
    expect(project.output.join("\n")).toContain("intake");
    expect(business.output.join("\n")).toContain("conversion");
    expect(foreman.output.join("\n")).toContain("inbox");
    expect(runtime.output.join("\n")).toContain("ensure-ready");
  });

  test("returns stable usage errors instead of exiting during import", async () => {
    const unknown = await capture(() => main(["missing"]));
    const missingSource = await capture(() => main(["project", "intake"]));
    expect(unknown.code).toBe(2);
    expect(unknown.errors[0]).toContain("unknown factory command");
    expect(missingSource.code).toBe(2);
    expect(missingSource.errors[0]).toContain("one source is required");
  });

  test("runs registry checks through the Bun CLI", async () => {
    const result = await capture(() => main(["registry", "check", "tests/fixtures/registry-m1", "--json"]));
    expect(result.code).toBe(0);
    expect(JSON.parse(result.output.join("\n"))).toMatchObject({ ok: true });
  });

  test("binds an Operator through the business CLI and preserves the binding across processes", { timeout: 30_000 }, async () => {
    const root = resolve("tmp", "operator-binding-" + randomUUID());
    mkdirSync(root, { recursive: true });
    const dbPath = join(root, "factory.db");
    const initial = new SqliteStore(dbPath, dbPath);
    const system = initial.createBusinessSystem({ name: "Binding CLI" });
    const project = initial.createIntake({ name: "Project", business_system_id: system.id, repo_url: "https://example.test/project" });
    const unitId = project.id + ":operator";
    initial.close();
    const run = async (args: string[]) => {
      const bun = Bun as unknown as { spawn(argv: string[], options: object): { stdout: ReadableStream; stderr: ReadableStream; exited: Promise<number> } };
      const child = bun.spawn([Bun.argv[0], "run", "backend/src/cli.ts", "business", ...args], {
        env: { ...process.env, FACTORY_ROOT: root, FACTORY_DB_PATH: dbPath, BUSINESS_DB_PATH: dbPath }, stdout: "pipe", stderr: "pipe",
      });
      const [code, body, errors] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()]);
      if (code !== 0) throw new Error(errors);
      return JSON.parse(body);
    };
    try {
      const changed = await run(["bind", system.id, unitId, "--harness", "claude-code", "--model", "custom/model"]);
      expect(changed).toMatchObject({ unit: { id: unitId, harnessRuntime: "claude-code", model: "custom/model" } });
      const units = await run(["units", system.id]) as Array<{ id: string }>;
      expect(units.find(({ id }) => id === unitId)).toMatchObject({ harnessRuntime: "claude-code", model: "custom/model" });
    } finally { rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 75 }); }
  });

  test("imports material, seals a scripted job and returns its lineage through Bun CLI", { timeout: 60_000 }, async () => {
    const root = join("tmp", `station-cli-${randomUUID()}`);
    mkdirSync(root, { recursive: true });
    try {
      const input = join(root, "note.txt");
      const commands = join(root, "commands.json");
      await Bun.write(input, "hello\n");
      await Bun.write(commands, JSON.stringify({
        "upper-text": [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools/upper_text.ts")],
        "count-text": [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools/count_text.ts")],
      }));
      const options = ["--root", root, "--registry", "tests/fixtures/registry-m1", "--provider", "local", "--local-commands", commands, "--json"];
      const imported = await capture(() => main([...options, "bean", "import", "--type", "note.text@1", "--file", `note.txt=${input}`]));
      expect(imported).toMatchObject({ code: 0, errors: [] });
      const { id: beanId } = JSON.parse(imported.output[0]);
      const executed = await capture(() => main([...options.slice(0, -1), "job", "run", "upper@1", "--input", `note=${beanId}`]));
      expect(executed).toMatchObject({ code: 0, errors: [] });
      expect(executed.output).toHaveLength(1);
      expect(executed.output[0]).toMatch(/^note\tsha256:[a-f0-9]{64}$/);
      const outputId = executed.output[0].split("\t")[1];
      expect(outputId).not.toBe(beanId);
      const lineage = await capture(() => main(["--root", root, "bean", "lineage", outputId, "--direction", "up"]));
      expect(lineage.code).toBe(0);
      const upstream = JSON.parse(lineage.output[0]);
      expect(upstream.beans).toEqual([beanId]);
      const shown = await capture(() => main(["job", "show", upstream.jobs[0].jobId, "--root", root]));
      expect(JSON.parse(shown.output[0])).toMatchObject({ state: "SEALED", attempt: 1, cost_usd: 0, outputs: { note: [outputId] } });
      const runAgain = (withCache: boolean) => capture(() => main([...options, "job", "run", "upper@1", "--input", `note=${beanId}`, ...(withCache ? ["--cache"] : [])]));
      const reused = await runAgain(true);
      expect(reused).toMatchObject({ code: 0, errors: [] });
      expect(JSON.parse(reused.output[0])).toMatchObject({ id: upstream.jobs[0].jobId, cache_hit: true });
      const newer = JSON.parse((await runAgain(false)).output[0]);
      expect(newer.id).not.toBe(upstream.jobs[0].jobId);
      const inspectCache = (args: string[]) => capture(() => main(["--root", root, "cache", ...args]));
      const entries = JSON.parse((await inspectCache(["ls", "--station", "upper@1"])).output[0]);
      expect(entries.map(({ jobId }: { jobId: string }) => jobId)).toEqual([upstream.jobs[0].jobId, newer.id]);
      const explained = JSON.parse((await inspectCache(["explain", newer.id])).output[0]);
      expect(explained.hits).toEqual(entries.map(({ jobId }: { jobId: string }) => jobId));
      const invalidated = await inspectCache(["invalidate", "--job", upstream.jobs[0].jobId, "--reason", "acceptance"]);
      expect(invalidated.code).toBe(0);
      expect(JSON.parse((await runAgain(true)).output[0])).toMatchObject({ id: newer.id, cache_hit: true });
    } finally {
      rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    }
  });

  test("executes a durable Line through Bun CLI and exposes its node outputs", { timeout: 60_000 }, async () => {
    const root = join("tmp", `line-cli-${randomUUID()}`);
    const registry = join(root, "registry");
    mkdirSync(root, { recursive: true });
    try {
      cpSync("tests/fixtures/registry-m1", registry, { recursive: true });
      mkdirSync(join(registry, "lines"), { recursive: true });
      const linePath = join(registry, "lines", "uppercase.yaml");
      await Bun.write(linePath, [
        "apiVersion: mao/v1", "kind: Line", "metadata: {name: uppercase, version: 1}", "spec:",
        "  interface:", "    inputs: {note: {type: note.text@1, requires: [non_empty]}}", "    outputs: {result: {type: note.text@1, from: upper.note}}",
        "  budget: {cap_usd: 0}", "  steps:", "    upper: {station: upper@1, in: {note: $inputs.note}}",
      ].join("\n"));
      const note = join(root, "note.txt");
      const commands = join(root, "commands.json");
      await Bun.write(note, "hello");
      await Bun.write(commands, JSON.stringify({
        "upper-text": [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools/upper_text.ts")],
        "count-text": [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools/count_text.ts")],
      }));
      const options = ["--root", root, "--registry", registry, "--provider", "local", "--local-commands", commands, "--json"];
      const plan = await capture(() => main([...options, "line", "plan", "uppercase@1"]));
      expect(plan).toMatchObject({ code: 0, errors: [] });
      expect(JSON.parse(plan.output[0]).outputs).toEqual({ result: ["upper", "note"] });
      const imported = await capture(() => main([...options, "bean", "import", "--type", "note.text@1", "--file", `note.txt=${note}`]));
      expect(imported).toMatchObject({ code: 0, errors: [] });
      const input = JSON.parse(imported.output[0]).id;
      const executed = await capture(() => main([...options, "line", "run", "uppercase@1", "--in", `note=${input}`]));
      expect(executed).toMatchObject({ code: 0, errors: [] });
      const run = JSON.parse(executed.output[0]);
      expect(run).toMatchObject({ state: "SUCCEEDED", spentUsd: 0, reservedUsd: 0 });
      const nodes = await capture(() => main(["--root", root, "line", "nodes", run.id]));
      expect(nodes.code).toBe(0);
      expect(JSON.parse(nodes.output[0])).toMatchObject([{ nodeId: "upper", state: "DONE", outputs: { note: run.outputs.result } }]);
      expect(run.outputs.result[0]).not.toBe(input);
      const reused = await capture(() => main([...options, "line", "run", "uppercase@1", "--in", `note=${input}`]));
      expect(reused).toMatchObject({ code: 0, errors: [] });
      const reusedRun = JSON.parse(reused.output[0]);
      const reusedNodes = await capture(() => main(["--root", root, "line", "nodes", reusedRun.id]));
      expect(reusedRun.outputs).toEqual(run.outputs);
      expect(JSON.parse(reusedNodes.output[0])).toMatchObject([{ nodeId: "upper", state: "DONE", cacheHit: true }]);

      await Bun.write(linePath, (await Bun.file(linePath).text()).replace("cap_usd: 0", "cap_usd: 1"));
      const rebuiltResponse = await capture(() => main([...options, "line", "rebuild", run.id]));
      expect(rebuiltResponse).toMatchObject({ code: 0, errors: [] });
      const rebuilt = JSON.parse(rebuiltResponse.output[0]);
      expect(rebuilt).toMatchObject({ state: "PENDING", rebuiltFrom: run.id, budgetCapUsd: 0, inputs: run.inputs });
      expect(rebuilt.epoch).not.toBe(run.epoch);
      const result = await capture(() => main([...options, "line", "exec", rebuilt.id]));
      expect(JSON.parse(result.output[0])).toMatchObject({ state: "SUCCEEDED", outputs: run.outputs, spentUsd: 0 });
    } finally {
      rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    }
  });

  test("approves and resumes a pending Job through the CLI after its Registry is removed", { timeout: 60_000 }, async () => {
    const root = join("tmp", `approval-cli-${randomUUID()}`);
    const registry = join(root, "registry");
    mkdirSync(root, { recursive: true });
    try {
      cpSync("tests/fixtures/registry-m2", registry, { recursive: true });
      const station = join(registry, "stations", "publish-staged.yaml");
      const spec = await Bun.file(station).text();
      await Bun.write(station, spec.replace(/propose: \{auto_if: .*\}/, "propose: foreman"));
      const note = join(root, "note.txt");
      const commands = join(root, "commands.json");
      const publication = resolve(root, "published");
      await Bun.write(note, "data to publish\n");
      await Bun.write(commands, JSON.stringify(Object.fromEntries(
        ["write-proposal", "count-text", "publish-copy", "local-file-check"].map((name) =>
          [name, [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools", name.replaceAll("-", "_") + ".ts")]]),
      )));
      const cli = async (...args: string[]) => {
        const result = await capture(() => main(["--root", root, "--registry", registry,
          "--provider", "local", "--local-commands", commands, "--json", ...args]));
        expect(result).toMatchObject({ code: 0, errors: [] });
        return JSON.parse(result.output[0]);
      };
      const bean = await cli("bean", "import", "--type", "note.text@1", "--file", `note.txt=${note}`);
      const job = await cli("job", "run", "publish-staged@1", "--input", `note=${bean.id}`, "--param", `dir=${JSON.stringify(publication)}`);
      expect(job.state).toBe("AWAITING_APPROVAL");
      const [approval] = await cli("approval", "ls", "--job", job.id);
      rmSync(registry, { recursive: true });
      expect(await cli("approval", "approve", approval.id)).toMatchObject({ status: "approved" });
      expect(await cli("job", "exec", job.id)).toMatchObject({ state: "SEALED", epoch: job.epoch });
      const [receipt] = await cli("receipt", "ls");
      expect(await cli("receipt", "verify", receipt.id)).toMatchObject({ status: "verified" });
      expect(await Bun.file(join(publication, "note.txt")).text()).toBe("data to publish\n");
    } finally {
      rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    }
  });

  test("advances a triggered Line through the scheduler after the Registry changes", { timeout: 60_000 }, async () => {
    const root = join("tmp", `scheduled-cli-${randomUUID()}`);
    const registry = join(root, "registry");
    mkdirSync(root, { recursive: true });
    try {
      cpSync("tests/fixtures/registry-m2", registry, { recursive: true });
      const note = join(root, "note.txt");
      const config = join(root, "scheduler.json");
      const commands = join(root, "commands.json");
      await Bun.write(note, "hello\n");
      await Bun.write(config, JSON.stringify({ pools: { cpu: 0, memory_mb: 256, gpu: 0 } }));
      await Bun.write(commands, JSON.stringify(Object.fromEntries(
        ["upper-text", "count-text"].map((name) =>
          [name, [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools", name.replaceAll("-", "_") + ".ts")]]),
      )));
      const cli = async (...args: string[]) => {
        const result = await capture(() => main(["--root", root, "--registry", registry,
          "--provider", "local", "--local-commands", commands, "--json", ...args]));
        expect(result).toMatchObject({ code: 0, errors: [] });
        return JSON.parse(result.output[0]);
      };
      const bean = await cli("bean", "import", "--type", "note.text@1", "--file", `note.txt=${note}`);
      const context = Context.open(root);
      try { context.warehouse.label(bean.id, "line", "note-trigger", "test"); }
      finally { context.close(); }
      const first = await cli("schedule", "tick", "--config", config);
      expect(first.triggered).toHaveLength(1);
      expect(first.pumped).toEqual([]);
      const [runId] = first.triggered;
      const [node] = await cli("line", "nodes", runId);
      expect(node.state).toBe("SUBMITTED");
      const pending = await cli("job", "show", node.jobId);

      await Bun.write(config, JSON.stringify({ pools: { cpu: 1, memory_mb: 256, gpu: 0 } }));
      const machine = join(registry, "machines", "toolbox", "machine.yaml");
      const original = await Bun.file(machine).text();
      await Bun.write(machine, original.replace("upper-text", "changed-executable"));
      expect(await cli("schedule", "tick", "--config", config)).toMatchObject({ triggered: [], pumped: [node.jobId] });
      expect(await cli("line", "show", runId)).toMatchObject({ state: "SUCCEEDED", epoch: pending.epoch });
      expect(await cli("job", "show", node.jobId)).toMatchObject({ state: "SEALED", epoch: pending.epoch });
    } finally {
      rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    }
  });
});
