import { describe, expect, test } from "bun:test";
import { mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { Context, CounterIds, FakeClock, Gate, loadSnapshot, digestBytes } from "../src/legacy";

describe("Bun M1 Gate boundary", () => {
  test("admits external material and accepts a sealed input Bean", async () => {
    const root = join("tmp", `legacy-gate-${Date.now()}`); mkdirSync(root, { recursive: true }); const snapshot = await loadSnapshot("tests/fixtures/registry-m1"); const context = Context.open(root, { clock: new FakeClock(), ids: new CounterIds() });
    try {
      const source = join(root, "note.txt"); await Bun.write(source, "hello"); const gate = new Gate(snapshot, context.warehouse, join(root, "work"));
      const admitted = await gate.admitExternal("note.text@1", { "note.txt": source }, { title: "hello" });
      await context.warehouse.sealMany([[admitted.bean.id, {}]], null, digestBytes(new TextEncoder().encode("report")));
      expect(gate.checkInputs("upper@1", { note: admitted.bean.id }).note).toBeDefined();
      expect(admitted.report.data.verdict).toBe("pass");
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("registers required output files and rejects missing outputs", async () => {
    const root = join("tmp", `legacy-gate-inspect-${Date.now()}`); mkdirSync(root, { recursive: true }); const snapshot = await loadSnapshot("tests/fixtures/registry-m1"); const context = Context.open(root, { clock: new FakeClock(), ids: new CounterIds() });
    try {
      const gate = new Gate(snapshot, context.warehouse, join(root, "work")); const missing = await gate.inspect("upper@1", { jobId: "job-1", attempt: 1, outDir: join(root, "empty") });
      expect(missing.data.verdict).toBe("reject");
      mkdirSync(join(root, "out", "note"), { recursive: true });
      await Bun.write(join(root, "out", "note", "note.txt"), "HELLO");
      const report = await gate.inspect("upper@1", { jobId: "job-2", attempt: 1, outDir: join(root, "out") });
      expect(report.data.verdict).toBe("pass");
      expect(report.data.outputs.note).toHaveLength(1);
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });
});
