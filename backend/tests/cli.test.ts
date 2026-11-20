import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { cpSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { main } from "../src/cli";

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
      await Bun.write(join(registry, "lines", "uppercase.yaml"), [
        "apiVersion: mao/v1", "kind: Line", "metadata: {name: uppercase, version: 1}", "spec:",
        "  interface:", "    inputs: {note: {type: note.text@1, requires: [non_empty]}}", "    outputs: {result: {type: note.text@1, from: upper.note}}",
        "  budget: {cap_usd: 1}", "  steps:", "    upper: {station: upper@1, in: {note: $inputs.note}}",
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
    } finally {
      rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    }
  });
});
