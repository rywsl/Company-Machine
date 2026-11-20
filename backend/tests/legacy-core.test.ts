import { describe, expect, test } from "bun:test";
import { mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { Context, CounterIds, FakeClock, Snapshot, canonicalJson, digestBytes, loadSnapshot } from "../src/legacy";

function tempRoot(label: string): string {
  const root = join("tmp", `legacy-${label}-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  mkdirSync(root, { recursive: true });
  return root;
}

describe("Bun M1 core boundaries", () => {
  test("canonical JSON, IDs and durations are deterministic", () => {
    expect(canonicalJson({ b: 2, a: 1 })).toBe('{"a":1,"b":2}');
    expect(new CounterIds().new("job")).toBe("job-0001");
    expect(new FakeClock().monotonic()).toBe(0);
  });

  test("stores, seals, materializes and traces Bean lineage", async () => {
    const root = tempRoot("warehouse");
    const input = join(root, "input.txt");
    await Bun.write(input, "hello\n");
    const context = Context.open(root, { clock: new FakeClock(), ids: new CounterIds() });
    try {
      const first = await context.warehouse.add({ type: "note.text@1.0", meta: { title: "hello" }, files: { "note.txt": input } });
      const second = await context.warehouse.add({ type: "note.text@1.0", meta: { title: "out" }, files: { "out.txt": input } });
      const report = digestBytes(new TextEncoder().encode("report"));
      await context.warehouse.sealMany([[first.id, { chars: 6 }], [second.id, { chars: 6 }]], "job-0001", report);
      context.warehouse.recordLineage("job-0001", { input: [first.id] }, { output: [second.id] });
      expect(context.warehouse.get(first.id).sealed).toBe(true);
      expect(context.warehouse.lineage(second.id, "up").beans).toEqual([first.id]);
      const materialized = join(root, "materialized");
      await context.warehouse.materialize(second.id, materialized, "bean");
      expect(await Bun.file(join(materialized, "payload", "out.txt")).text()).toBe("hello\n");
      expect((await context.warehouse.verify()).length).toBe(0);
    } finally {
      context.close();
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("loads a checked Registry snapshot and resolves type declarations", async () => {
    const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
    expect(snapshot.keys("Type")).toContain("Type:note.text@1.0");
    expect(snapshot.typeKey("note.text@1")).toBe("Type:note.text@1.0");
    expect(snapshot.factsDecl("note.text@1")).toMatchObject({ chars: "int", lines: "int" });
    expect(snapshot.always("note.text@1")).toEqual(new Set());
  });
});
