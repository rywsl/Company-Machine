import { describe, expect, test } from "bun:test";
import { mkdirSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { LocalProvider } from "../src/legacy/sandbox";
import { submit } from "../src/legacy/factoryctl";

function root(label: string): string { const value = join("tmp", `legacy-${label}-${Date.now()}-${Math.random().toString(16).slice(2)}`); mkdirSync(value, { recursive: true }); return value; }

describe("Bun M1 local sandbox and control mailbox", () => {
  test("maps virtual paths while running a deterministic command map", { timeout: 30_000 }, async () => {
    const directory = root("local");
    try {
      const input = join(directory, "in"); const output = join(directory, "out"); mkdirSync(input); mkdirSync(output); await Bun.write(join(input, "note.txt"), "hello\n");
      const provider = new LocalProvider({ "upper-text": [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools/upper_text.ts")] });
      const sandbox = await provider.open({ name: "local", image: "upper-text", mounts: [{ source: input, target: "/factory/in", readonly: true }, { source: output, target: "/factory/out", readonly: false }], workdir: "/workspace" });
      const result = await sandbox.exec(["upper-text", "/factory/in/note.txt", "/factory/out/note.txt"], { timeoutMs: 10_000, stdout: join(directory, "stdout"), stderr: join(directory, "stderr") });
      expect(result.exitCode).toBe(0);
      expect(await Bun.file(join(output, "note.txt")).text()).toBe("HELLO\n");
      await sandbox.close();
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });

  test("writes submit records through the Bun factoryctl boundary", { timeout: 30_000 }, async () => {
    const directory = root("ctl"); const previous = { ctl: process.env.FACTORY_CTL, out: process.env.FACTORY_OUT };
    try {
      const ctl = join(directory, "ctl"); const out = join(directory, "out"); mkdirSync(ctl, { recursive: true }); mkdirSync(out, { recursive: true }); const source = join(directory, "draft.txt"); await Bun.write(source, "draft");
      process.env.FACTORY_CTL = ctl; process.env.FACTORY_OUT = out;
      const record = submit("note", source, { title: "draft" });
      expect(record.port).toBe("note");
      expect(JSON.parse(readFileSync(join(ctl, "submissions", "note.json"), "utf8")).files).toEqual(["draft.txt"]);
    } finally {
      if (previous.ctl === undefined) delete process.env.FACTORY_CTL; else process.env.FACTORY_CTL = previous.ctl;
      if (previous.out === undefined) delete process.env.FACTORY_OUT; else process.env.FACTORY_OUT = previous.out;
      rmSync(directory, { recursive: true, force: true });
    }
  });

  test("returns a bounded timeout for local commands", { timeout: 30_000 }, async () => {
    const directory = root("timeout");
    try {
      const provider = new LocalProvider({ sleep: [Bun.argv[0], "-e", "await Bun.sleep(1000)"] });
      const sandbox = await provider.open({ name: "local", image: "sleep", mounts: [] });
      const result = await sandbox.exec(["sleep"], { timeoutMs: 20, stdout: join(directory, "stdout"), stderr: join(directory, "stderr") });
      expect(result.timedOut).toBe(true);
      await sandbox.close();
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });

  test("leaves unrelated host environment out of local commands", { timeout: 30_000 }, async () => {
    const directory = root("environment");
    const key = "MAO_TEST_UNRELATED_SECRET";
    const previous = process.env[key];
    process.env[key] = "fixture-only";
    try {
      const provider = new LocalProvider({ probe: [Bun.argv[0], "-e", "console.log(process.env.MAO_TEST_UNRELATED_SECRET ?? 'omitted')"] });
      const sandbox = await provider.open({ name: "local", image: "probe", mounts: [] });
      const stdout = join(directory, "stdout");
      try {
        await sandbox.exec(["probe"], { timeoutMs: 10_000, stdout, stderr: join(directory, "stderr") });
        expect((await Bun.file(stdout).text()).trim()).toBe("omitted");
      } finally { await sandbox.close(); }
    } finally {
      if (previous === undefined) delete process.env[key];
      else process.env[key] = previous;
      rmSync(directory, { recursive: true, force: true });
    }
  });
});
