import { expect, test } from "bun:test";
import { cpSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Context, LineRunner, LocalProvider, Runner, loadSnapshot } from "../src/legacy";

const line = "note-map@1";
const text = "alpha\nbeta\ngamma\n";
const splitTool = `
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
const [source, dest] = Bun.argv.slice(2);
const lines = (await Bun.file(source).text()).split(/\\r?\\n/).filter((line) => line.trim());
for (const [index, line] of lines.entries()) {
  const directory = join(dest, String(index));
  await mkdir(directory, { recursive: true });
  await Bun.write(join(directory, "note.txt"), line + "\\n");
}
`;

async function world(settings: Record<string, unknown>) {
  const root = mkdtempSync(join(tmpdir(), "mao-map-runtime-"));
  const context = Context.open(root);
  const close = () => { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); };
  try {
    const registry = join(root, "registry");
    cpSync("tests/fixtures/registry-m2", registry, { recursive: true });
    const toolbox = join(registry, "machines", "toolbox", "machine.yaml");
    await Bun.write(toolbox, (await Bun.file(toolbox).text()) + `
    split:
      inputs: {note: {type: note.text@1}}
      outputs: {notes: {type: "list<note.text@1>"}}
      run: [split-lines, /factory/in/note/note.txt, /factory/out/notes]
`);
    writeFileSync(join(registry, "stations", "split.yaml"), `apiVersion: mao/v1
kind: Station
metadata: {name: split, version: 1}
spec:
  role: transform
  machine: toolbox@1
  recipe: split
  operator: {runtime: script@1}
  inputs: {note: {type: note.text@1, requires: [non_empty]}}
  outputs: {notes: {type: "list<note.text@1>", guarantees: [non_empty]}}
  limits: {timeout: 60s, budget_usd: 0, retries: {rejected: 0, failed: 0, mode: fresh}}
  policy: tool-default@1
`);
    writeFileSync(join(registry, "lines", "note-map.yaml"), Bun.YAML.stringify({
      apiVersion: "mao/v1", kind: "Line", metadata: { name: "note-map", version: 1 },
      spec: {
        interface: { inputs: { note: { type: "note.text@1", requires: ["non_empty"] } }, outputs: { notes: { from: "shout.note", type: "list<note.text@1>" } } },
        budget: { cap_usd: 1 },
        steps: { split: { station: "split@1", in: { note: "$inputs.note" } }, shout: { station: "upper@1", in: { note: "$item" }, map: { over: "split.notes", ...settings } } },
      },
    }));
    const snapshot = await loadSnapshot(registry);
    const tool = join(root, "split.ts");
    await Bun.write(tool, splitTool);
    const provider = new LocalProvider({
      "split-lines": [Bun.argv[0], tool],
      ...Object.fromEntries(["count-text", "upper-text"].map((name) => [name, [Bun.argv[0], join(import.meta.dir, `../../tests/fakes/tools/${name.replaceAll("-", "_")}.ts`)]])),
    });
    const source = join(root, "input.txt");
    await Bun.write(source, text);
    const bean = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": source } });
    context.warehouse.sealMany([[bean.id, { chars: text.length, lines: 3, has_lower: true }]], null, `sha256:${"0".repeat(64)}`);
    const runner = new Runner(context, snapshot, provider);
    const lines = new LineRunner(runner);
    const run = lines.start(line, { inputs: { note: bean.id }, useCache: false });
    const read = (id: string) => Bun.file(context.warehouse.blobPath(context.warehouse.get(id).payload[0].digest)).text();
    const nodes = () => Object.fromEntries(lines.runs.nodes(run.id).map((node) => [node.nodeId, node]));
    return { runner, lines, run, read, nodes, close };
  } catch (error) { close(); throw error; }
}

test("expands every runtime list item and aggregates outputs in source order", { timeout: 60_000 }, async () => {
  const task = await world({ max_items: 3 });
  try {
    const done = await task.lines.run(task.run.id);
    const nodes = task.nodes();
    const ids = Object.keys(nodes).filter((name) => name.startsWith("shout#")).sort();
    expect(done.state).toBe("SUCCEEDED");
    expect(ids).toEqual(["shout#0", "shout#1", "shout#2"]);
    expect(ids.every((id) => nodes[id].state === "DONE" && nodes[id].jobId)).toBe(true);
    const ordered = ids.map((id) => nodes[id].outputs.note[0]);
    expect(nodes.shout).toMatchObject({ state: "DONE", jobId: null, outputs: { note: ordered } });
    expect(done.outputs.notes).toEqual(ordered);
    expect(await Promise.all(ordered.map(task.read))).toEqual(["ALPHA\n", "BETA\n", "GAMMA\n"]);
  } finally { task.close(); }
});

test("rejects runtime Map overflow before creating any instances", { timeout: 60_000 }, async () => {
  const task = await world({ max_items: 2 });
  try {
    const done = await task.lines.run(task.run.id);
    const nodes = task.nodes();
    expect(done.state).toBe("FAILED");
    expect(nodes.split.state).toBe("DONE");
    expect(nodes.shout).toMatchObject({ state: "FAILED", error: { code: "DYNAMIC_OVERFLOW" } });
    expect(Object.keys(nodes).filter((name) => name.startsWith("shout#"))).toEqual([]);
  } finally { task.close(); }
});

test("truncates Map overflow to the upstream prefix", { timeout: 60_000 }, async () => {
  const task = await world({ max_items: 2, overflow: "truncate" });
  try {
    const done = await task.lines.run(task.run.id);
    expect(done.state).toBe("SUCCEEDED");
    expect(Object.keys(task.nodes()).filter((name) => name.startsWith("shout#")).sort()).toEqual(["shout#0", "shout#1"]);
    expect(await Promise.all(done.outputs.notes.map(task.read))).toEqual(["ALPHA\n", "BETA\n"]);
  } finally { task.close(); }
});

test("limits new Map station jobs per tick and keeps other instances waiting", { timeout: 60_000 }, async () => {
  const task = await world({ max_items: 3, max_parallel: 1 });
  try {
    const [producer] = task.lines.tick(task.run.id);
    await task.runner.run(producer);
    expect(task.lines.tick(task.run.id)).toHaveLength(1);
    const nodes = task.nodes();
    expect(nodes["shout#0"].state).toBe("SUBMITTED");
    expect(nodes["shout#1"].state).toBe("WAITING");
    expect(nodes["shout#2"].state).toBe("WAITING");
    const done = await task.lines.run(task.run.id);
    expect(done.state).toBe("SUCCEEDED");
    expect(done.outputs.notes).toHaveLength(3);
  } finally { task.close(); }
});
