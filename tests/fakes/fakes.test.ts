import { describe, expect, test } from "bun:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { FakeLlm } from "./fake_llm";

async function run(argv: string[], options: { cwd?: string; env?: Record<string, string> } = {}) {
  const child = Bun.spawn(argv, { cwd: options.cwd, env: { ...process.env, ...options.env }, stdout: "pipe", stderr: "pipe" });
  const code = await child.exited;
  return { code, stdout: await new Response(child.stdout).text(), stderr: await new Response(child.stderr).text() };
}

describe("Bun fixture Harnesses", () => {
  test("runs the migrated text validator fixtures", { timeout: 30_000 }, async () => {
    const root = await mkdtemp(join(process.cwd(), "tmp", "fakes-test-"));
    try {
      const input = join(root, "input.txt");
      const output = join(root, "output.txt");
      await writeFile(input, "hello\n", "utf8");
      const upper = await run([Bun.argv[0], join(import.meta.dir, "tools", "upper_text.ts"), input, output]);
      expect(upper.code).toBe(0);
      expect(await Bun.file(output).text()).toBe("HELLO\n");
      const count = await run([Bun.argv[0], join(import.meta.dir, "tools", "count_text.ts"), output]);
      expect(count.code).toBe(0);
      expect(JSON.parse(count.stdout)).toMatchObject({ chars: 5, lines: 1, has_lower: false });
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("emits the official stream-json-shaped fake agent result", { timeout: 30_000 }, async () => {
    const root = await mkdtemp(join(process.cwd(), "tmp", "fake-agent-test-"));
    try {
      const script = join(root, "task", "task.json");
      await (await import("node:fs/promises")).mkdir(join(root, "task"), { recursive: true });
      await writeFile(script, JSON.stringify({ default: [{ do: "echo_task", path: "/factory/out/task.txt" }] }), "utf8");
      const result = await run([Bun.argv[0], join(import.meta.dir, "fake_agent.ts"), "-p", "build it", "--model", "fake-model"], {
        env: { FACTORY_IN: root, FACTORY_OUT: root, FACTORY_JOB_ID: "fixture-job" },
      });
      const events = result.stdout.trim().split(/\r?\n/).map((line) => JSON.parse(line));
      expect(result.code).toBe(0);
      expect(events[0]).toMatchObject({ type: "system", subtype: "init" });
      expect(events.at(-1)).toMatchObject({ type: "result", subtype: "success", is_error: false });
      expect(await Bun.file(join(root, "task.txt")).text()).toBe("build it");
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("serves the scripted Anthropic fixture without a model API", { timeout: 30_000 }, async () => {
    const root = await mkdtemp(join(process.cwd(), "tmp", "fake-llm-test-"));
    const script = join(root, "llm.json");
    const fake = new FakeLlm(script);
    try {
      await writeFile(script, JSON.stringify({ turns: [[{ text: "ready" }]] }), "utf8");
      const port = fake.start();
      const response = await fetch(`http://127.0.0.1:${port}/v1/messages`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ model: "fake", messages: [{ role: "user", content: "hello" }], stream: false }) });
      expect(response.status).toBe(200);
      expect(await response.json()).toMatchObject({ content: [{ type: "text", text: "ready" }] });
      expect(fake.requests).toHaveLength(1);
    } finally {
      fake.stop();
      await rm(root, { recursive: true, force: true });
    }
  });
});
