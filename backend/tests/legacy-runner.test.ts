import { describe, expect, test } from "bun:test";
import { mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { Context, CounterIds, FakeClock, LocalProvider, Runner, loadSnapshot, Snapshot } from "../src/legacy";
import type { SandboxSpec } from "../runtime/podman";

describe("Bun M1 Runner boundary", () => {
  test("submits a checked job and seals a scripted output", { timeout: 60_000 }, async () => {
    const root = join("tmp", `legacy-runner-${Date.now()}`); mkdirSync(root, { recursive: true }); const context = Context.open(root, { clock: new FakeClock(), ids: new CounterIds() }); const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
    try {
      const input = join(root, "input.txt"); await Bun.write(input, "hello\n"); const bean = await context.warehouse.add({ type: "note.text@1.0", meta: {}, files: { "note.txt": input } }); await context.warehouse.sealMany([[bean.id, {}]], null, "sha256:" + "a".repeat(64));
      const provider = new LocalProvider({ "upper-text": [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools/upper_text.ts")] }); const runner = new Runner(context, snapshot, provider); const job = runner.submit("upper@1", { note: bean.id }, {}); const result = await runner.run(job.id);
      expect(result.state).toBe("SEALED"); expect(runner.adopt(job.id).state).toBe("SEALED"); expect((await runner.run(job.id)).state).toBe("SEALED"); expect(Object.keys(result.outputs)).toEqual(["note"]); expect(context.warehouse.list({ sealedOnly: true })).toHaveLength(2);
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("materializes a single-element list under its indexed directory and executes once", async () => {
    const root = join("tmp", `legacy-runner-list-${Date.now()}`);
    const base = await loadSnapshot("tests/fixtures/registry-m1");
    const docs = structuredClone(base.docs);
    docs["Station:upper@1"].spec.inputs.note.type = "list<note.text@1>";
    const snapshot = new Snapshot(base.root, base.epoch, docs, base.result);
    const context = Context.open(root, { clock: new FakeClock(), ids: new CounterIds() });
    try {
      const input = join(root, "input.txt");
      await Bun.write(input, "hello");
      const bean = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": input } });
      await context.warehouse.sealMany([[bean.id, {}]], null, "sha256:" + "a".repeat(64));
      let executions = 0;
      class RecordingProvider extends LocalProvider {
        async open(spec: SandboxSpec) {
          expect(spec.command).toBeUndefined();
          const inputMount = spec.mounts.find((mount) => mount.target === "/factory/in")!;
          const outputMount = spec.mounts.find((mount) => mount.target === "/factory/out")!;
          return {
            name: spec.name,
            exec: async (_argv: string[], options: { env?: Record<string, string> }) => {
              executions += 1;
              expect(options.env?.FACTORY_IN).toBeUndefined();
              expect(await Bun.file(join(inputMount.source, "note", "0", "note.txt")).text()).toBe("hello");
              mkdirSync(join(outputMount.source, "note"), { recursive: true });
              await Bun.write(join(outputMount.source, "note", "note.txt"), "HELLO");
              return { exitCode: 0, timedOut: false, durationMs: 1 };
            },
            async close() {},
          };
        }
      }
      const runner = new Runner(context, snapshot, new RecordingProvider());
      const job = runner.submit("upper@1", { note: [bean.id] }, {});
      expect((await runner.run(job.id)).state).toBe("SEALED");
      expect(executions).toBe(1);
    } finally {
      context.close();
      rmSync(root, { recursive: true, force: true });
    }
  });
});
