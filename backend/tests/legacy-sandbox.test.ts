import { describe, expect, test } from "bun:test";
import { mkdirSync, readFileSync, rmSync } from "node:fs";
import { join, normalize, resolve } from "node:path";
import { LocalProvider } from "../src/legacy/sandbox";
import { submit } from "../src/legacy/factoryctl";
import { runCommand } from "../runtime/podman";

function root(label: string): string { const value = join("tmp", `legacy-${label}-${Date.now()}-${Math.random().toString(16).slice(2)}`); mkdirSync(value, { recursive: true }); return value; }

describe("Bun M1 local sandbox and control mailbox", () => {
  test("submits file metadata and exposes inputs through the sandbox CLI", { timeout: 30_000 }, async () => {
    const directory = resolve(root("ctl-cli"));
    const ctl = join(directory, "ctl");
    const out = join(directory, "out");
    const workspace = join(directory, "workspace");
    const tool = join(import.meta.dir, "../src/legacy/factoryctl.ts");
    const call = (...args: string[]) => runCommand([Bun.argv[0], tool, ...args], {
      env: { FACTORY_CTL: ctl, FACTORY_OUT: out, FACTORY_WORKSPACE: workspace },
    });
    try {
      const job = { job_id: "j-0001", attempt: 1, inputs: { note: { id: "bean-1", type: "note.text@1.0" } }, budget_usd: 1 };
      await Bun.write(join(ctl, "job.json"), JSON.stringify(job));
      await Bun.write(join(workspace, "drafts", "a.txt"), "天\n");
      const source = "/workspace/drafts/a.txt";
      expect((await call("submit", "note", source, "--meta", "title=天")).exitCode).toBe(0);
      expect(readFileSync(join(out, "note", "a.txt"), "utf8")).toBe("天\n");
      const submission = JSON.parse(readFileSync(join(ctl, "submissions", "note.json"), "utf8"));
      expect(submission).toMatchObject({ port: "note", source, files: ["a.txt"], meta: { title: "天" } });
      expect(submission.at.endsWith("Z")).toBe(true);
      expect(JSON.parse(readFileSync(join(out, "note", "meta.json"), "utf8"))).toEqual({ title: "天" });
      const shown = await call("inputs", "--json");
      expect(shown.exitCode).toBe(0);
      expect(JSON.parse(new TextDecoder().decode(shown.stdout))).toEqual({ job_id: job.job_id, attempt: job.attempt, inputs: job.inputs });
      expect((await call("submit", "note", "/workspace/drafts/missing.txt")).exitCode).toBe(1);
      expect((await call("submit", "note", source, "--meta", "novalue")).exitCode).toBe(2);
      expect((await call("receipt", "note")).exitCode).toBe(2);
      expect((await call("submit", "note", source, "--label", "k=v")).exitCode).toBe(2);
      const budget = await call("budget", "--json");
      expect(budget.exitCode).toBe(0);
      expect(JSON.parse(new TextDecoder().decode(budget.stdout)).spent_usd).toBeNull();
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });

  test("maps argv, cwd and control environment and rejects execution after close", { timeout: 30_000 }, async () => {
    const directory = resolve(root("local-contract"));
    const probe = "MAO_LOCAL_CONTRACT_PROBE";
    const previous = process.env[probe];
    process.env[probe] = "host only";
    try {
      const mounts = [["in", "/factory/in"], ["out", "/factory/out"], ["ctl", "/factory/ctl"], ["workspace", "/workspace"]]
        .map(([folder, target]) => {
          const source = join(directory, folder);
          mkdirSync(source);
          return { source, target, readonly: folder === "in" };
        });
      const code = "console.log(JSON.stringify([process.env.FACTORY_IN, Bun.argv.at(-1), process.env.MAO_LOCAL_CONTRACT_PROBE ?? '-', Bun.argv[0], process.cwd()]))";
      const sandbox = await new LocalProvider({ probe: [Bun.argv[0], "-e", code] }).open({ name: "local", image: "probe", mounts });
      const paths = { stdout: join(directory, "stdout"), stderr: join(directory, "stderr"), timeoutMs: 10_000 };
      try {
        const result = await sandbox.exec(["probe", "/factory/in/note/note.txt"], paths);
        expect(result).toMatchObject({ exitCode: 0, timedOut: false });
        expect(result.durationMs).toBeGreaterThanOrEqual(0);
        const [envPath, argvPath, leaked, executable, cwd] = JSON.parse(readFileSync(paths.stdout, "utf8"));
        const normalized = (path: string) => process.platform === "win32" ? normalize(path).toLowerCase() : normalize(path);
        expect(envPath).toBe(join(directory, "in"));
        expect(argvPath).toBe(join(directory, "in", "note", "note.txt"));
        expect(leaked).toBe("-");
        expect(normalized(executable)).toBe(normalized(Bun.argv[0]));
        expect(normalized(cwd)).toBe(normalized(join(directory, "workspace")));
      } finally { await sandbox.close(); }
      await expect(sandbox.exec(["probe"], paths)).rejects.toMatchObject({ code: "SANDBOX_CLOSED" });
    } finally {
      if (previous === undefined) delete process.env[probe]; else process.env[probe] = previous;
      rmSync(directory, { recursive: true, force: true });
    }
  });
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

  test("uses the log directory when no working directory is mounted", async () => {
    const directory = resolve(root("workdir"));
    const provider = new LocalProvider({ probe: [Bun.argv[0], "-e", "console.log(process.cwd())"] });
    const sandbox = await provider.open({ name: "local", image: "probe", mounts: [], workdir: join(directory, "unmounted") });
    try {
      const stdout = join(directory, "logs", "stdout");
      expect((await sandbox.exec(["probe"], { timeoutMs: 5000, stdout, stderr: join(directory, "logs", "stderr") })).exitCode).toBe(0);
      const cwd = normalize((await Bun.file(stdout).text()).trim());
      const expected = join(directory, "logs");
      expect(process.platform === "win32" ? cwd.toLowerCase() : cwd).toBe(process.platform === "win32" ? expected.toLowerCase() : expected);
    } finally {
      await sandbox.close();
      rmSync(directory, { recursive: true, force: true });
    }
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
