import { describe, expect, test } from "bun:test";
import { mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { Context, CounterIds, FakeClock, LocalProvider, Runner, loadSnapshot } from "../src/legacy";

describe("Bun M1 Runner boundary", () => {
  test("submits a checked job and seals a scripted output", { timeout: 60_000 }, async () => {
    const root = join("tmp", `legacy-runner-${Date.now()}`); mkdirSync(root, { recursive: true }); const context = Context.open(root, { clock: new FakeClock(), ids: new CounterIds() }); const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
    try {
      const input = join(root, "input.txt"); await Bun.write(input, "hello\n"); const bean = await context.warehouse.add({ type: "note.text@1.0", meta: {}, files: { "note.txt": input } }); await context.warehouse.sealMany([[bean.id, {}]], null, "sha256:" + "a".repeat(64));
      const provider = new LocalProvider({ "upper-text": [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools/upper_text.ts")] }); const runner = new Runner(context, snapshot, provider); const job = runner.submit("upper@1", { note: bean.id }, {}); const result = await runner.run(job.id);
      expect(result.state).toBe("SEALED"); expect(runner.adopt(job.id).state).toBe("SEALED"); expect((await runner.run(job.id)).state).toBe("SEALED"); expect(Object.keys(result.outputs)).toEqual(["note"]); expect(context.warehouse.list({ sealedOnly: true })).toHaveLength(2);
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });
});
