import { describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chooseTransport, decodeOfficialLine, harnessEndpointEnv, harnessProcessCommand, officialCliInvocation, officialHarness, officialHarnesses, OfficialCliTransport, spawnOfficialProcess, windowsClaudeExecutable } from "../src/harness";
import { harnessEgressPlan } from "../src/harness-sandbox";
import { AcpTransport, decodeAcpLine } from "../src/acp";
import type { HarnessEvent, HarnessSession } from "../src/harness";

const acpRequest = { task: "构建项目", contract: "你是厂长", model: "kimi", cwd: "/workspace" };
const acpDefinition = officialHarness("kimi-code")!;
const encoder = new TextEncoder();

test("Harness endpoints reject credential-bearing query strings", () => {
  expect(() => harnessEndpointEnv(officialHarness("claude-code")!, "https://provider.example/v1?api_key=secret")).toThrow();
  expect(() => harnessEndpointEnv(officialHarness("codex")!, "https://provider.example/v1?token=secret")).toThrow();
  expect(() => harnessEndpointEnv(officialHarness("kimi-code")!, "https://provider.example/acp?credential=secret")).toThrow();
  expect(harnessEndpointEnv(officialHarness("claude-code")!, "https://provider.example/v1?tenant=mao")).toEqual({ ANTHROPIC_BASE_URL: "https://provider.example/v1?tenant=mao" });
});

test("cancels a pending official CLI event read when the owned stream stays open", async () => {
  let cancellations = 0;
  let terminations = 0;
  const transport = new OfficialCliTransport(officialHarness("claude-code")!, () => ({
    stdout: new ReadableStream({ cancel() { cancellations++; } }),
    kill() { terminations++; },
  }));
  const session = await transport.start(acpRequest);
  const events = session.events()[Symbol.asyncIterator]();
  const pending = events.next();
  await Promise.all([session.close(), session.close()]);
  expect(await pending).toMatchObject({ done: true });
  expect(cancellations).toBe(1);
  expect(terminations).toBe(1);
});

test("retains bounded redacted stderr on an official CLI failure", async () => {
  const transport = new OfficialCliTransport(officialHarness("claude-code")!, () => ({
    stdout: new ReadableStream({ start(controller) { controller.close(); } }),
    stderr: new ReadableStream({ start(controller) {
      controller.enqueue(encoder.encode("api_key=private-value\nprovider failed\n"));
      controller.close();
    } }),
    stderrRedactions: ["private-value"],
    exited: Promise.resolve(1),
    kill() {},
  }));
  const session = await transport.start(acpRequest);
  const events = [];
  for await (const event of session.events()) events.push(event);
  expect(events).toEqual([{
    type: "session.failed",
    metadata: { exitCode: 1, stderr: "api_key=[REDACTED]\nprovider failed" },
  }]);
});

test("finds the Windows native Claude executable in PATH or next to the official npm shim", () => {
  const root = mkdtempSync(join(tmpdir(), "mao-claude-executable-"));
  try {
    const launcher = join(root, "claude.cmd");
    const nativeRoot = join(root, "node_modules", "@anthropic-ai", "claude-code", "bin");
    const executable = join(nativeRoot, "claude.exe");
    expect(windowsClaudeExecutable(null)).toBeUndefined();
    expect(windowsClaudeExecutable(launcher)).toBeUndefined();
    mkdirSync(nativeRoot, { recursive: true });
    writeFileSync(executable, "");
    expect(windowsClaudeExecutable(launcher)).toBe(executable);
    expect(windowsClaudeExecutable(executable)).toBe(executable);
    expect(windowsClaudeExecutable(join(root, "custom.cmd"))).toBeUndefined();
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("terminates the owned Windows command wrapper and the native child holding its stdout", { timeout: 20_000 }, async () => {
  if (process.platform !== "win32") return;
  const root = mkdtempSync(join(tmpdir(), "mao-owned-harness-"));
  const script = join(root, "native.ts");
  const launcher = join(root, "runner.cmd");
  writeFileSync(script, 'console.log(process.pid); await Bun.sleep(30000);');
  writeFileSync(launcher, '@echo off\r\n"' + Bun.argv[0] + '" "' + script + '"\r\n');
  try {
    const child = await spawnOfficialProcess([launcher], { cwd: root, env: {}, stdout: "pipe" });
    const reader = child.stdout.getReader();
    try {
      const ready = await reader.read();
      const nativePid = Number(new TextDecoder().decode(ready.value).trim());
      expect(Number.isSafeInteger(nativePid) && nativePid > 0).toBe(true);
      expect(process.kill(nativePid, 0)).toBe(true);
      await child.kill();
      expect(await reader.read()).toMatchObject({ done: true });
      let nativeRunning = false;
      try { nativeRunning = process.kill(nativePid, 0); }
      catch (error) { if ((error as { code?: string }).code !== "ESRCH") throw error; }
      expect(nativeRunning).toBe(false);
    } finally {
      await child.kill();
      await reader.cancel();
      reader.releaseLock();
    }
  } finally { rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

test("uses exact Harness profile and deployment hosts while refusing a named network bypass", () => {
  expect(harnessEgressPlan("https://Provider.Example/api", " AUTH.EXAMPLE.,provider.example").allow).toEqual([
    { tier: "providers", host: "auth.example", methods: ["*"] },
    { tier: "providers", host: "provider.example", methods: ["*"] },
  ]);
  expect(() => harnessEgressPlan("", "")).toThrow("MAO_HARNESS_EGRESS_HOSTS");
  expect(() => harnessEgressPlan("", "*.example.com")).toThrow("exact");
  expect(() => harnessEgressPlan("https://provider.example:8443", "")).toThrow("standard");
  const image = "registry.example/harness@sha256:" + "a".repeat(64);
  expect(() => harnessProcessCommand(["codex"], {
    containerImage: image, network: "egress", env: { MAO_EGRESS_NETWORK: "bridge", HTTPS_PROXY: "http://legacy.example" },
  })).toThrow("verified isolated relay");
});

test("gives container Harness configuration a writable temporary home", () => {
  const image = "registry.example/harness@sha256:" + "a".repeat(64);
  const result = harnessProcessCommand(["claude"], {
    containerImage: image, network: "none", env: { HOME: "/configured", XDG_CONFIG_HOME: "/configured" },
  });
  expect(result.command).toContain("HOME=/tmp");
  expect(result.command).toContain("XDG_CONFIG_HOME=/tmp/.config");
  expect(result.command).toContain("--read-only");
  expect(result.env.HOME).not.toBe("/configured");
});

test("mounts only validated Operator material files on a local Linux container engine", () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), "mao-harness-material-"));
  const outputDirectory = join(root, "outputs");
  const source = join(root, "input.bin");
  const input = { source, index: 1 };
  const materialFiles = { outputDirectory, inputs: [input] };
  const image = "registry.example/harness@sha256:" + "a".repeat(64);
  const previous = { runtime: process.env.MAO_CONTAINER_RUNTIME, connection: process.env.CONTAINER_HOST, podmanConnection: process.env.CONTAINER_CONNECTION, kiriMode: process.env.MAO_KIRI_MODE };
  try {
    mkdirSync(outputDirectory);
    writeFileSync(source, "input", "utf8");
    process.env.MAO_CONTAINER_RUNTIME = "podman";
    delete process.env.CONTAINER_HOST;
    delete process.env.CONTAINER_CONNECTION;
    expect(() => harnessProcessCommand(["codex"], { env: {}, materialFiles })).toThrow("local Linux container runtime");
    const command = (files = materialFiles, foremanMailbox?: string) =>
      harnessProcessCommand(["codex"], { containerImage: image, env: {}, materialFiles: files, foremanMailbox }).command;
    if (process.platform !== "linux") {
      expect(() => command()).toThrow("local Linux container runtime");
      return;
    }
    process.env.CONTAINER_HOST = "ssh://remote.example";
    expect(() => command()).toThrow("local Linux container runtime");
    delete process.env.CONTAINER_HOST;
    for (const runtime of ["podman", "kiri"]) {
      process.env.MAO_CONTAINER_RUNTIME = runtime;
      process.env.MAO_KIRI_MODE = "linux-socket";
      const argv = command();
      expect(argv).toContain("--read-only");
      expect(argv.filter((_, index) => argv[index - 1] === "--mount")).toEqual([
        `type=bind,source=${outputDirectory},target=/mao/outputs`,
        `type=bind,source=${source},target=/mao/inputs/1,readonly`,
      ]);
    }
    const alias = join(root, "alias");
    symlinkSync(source, alias);
    expect(() => command({ ...materialFiles, outputDirectory: source })).toThrow("Invalid material output directory");
    expect(() => command({ ...materialFiles, inputs: [input, input] })).toThrow("Invalid material input mount");
    expect(() => command({ ...materialFiles, inputs: [{ index: 1, source: alias }] })).toThrow("Invalid material input mount");
    expect(() => command({ ...materialFiles, inputs: [{ index: 1, source: outputDirectory }] })).toThrow("Invalid material input mount");
    expect(() => command(materialFiles, root)).toThrow("cannot be combined with a Foreman mailbox");
    process.env.MAO_KIRI_MODE = "docker-context";
    expect(() => command()).toThrow("local Linux container runtime");
  } finally {
    for (const [key, value] of Object.entries({
      MAO_CONTAINER_RUNTIME: previous.runtime, CONTAINER_HOST: previous.connection,
      CONTAINER_CONNECTION: previous.podmanConnection, MAO_KIRI_MODE: previous.kiriMode,
    })) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    rmSync(root, { recursive: true, force: true });
  }
});

function frame(value: object): Uint8Array {
  return encoder.encode(JSON.stringify(value) + "\n");
}

function terminalFixture(frames: Uint8Array[], keepOpen = false, definition = acpDefinition) {
  const writes: Array<{ id: number; method?: string; params?: Record<string, unknown>; error?: { code: number } }> = [];
  let killed = false;
  const transport = new AcpTransport(definition, () => ({
    stdout: new ReadableStream({
      start(controller) {
        for (const frame of frames) controller.enqueue(frame);
        if (!keepOpen) controller.close();
      },
    }),
    stdin: { write(data) { writes.push(JSON.parse(new TextDecoder().decode(data))); } },
    kill() { killed = true; },
  }));
  return { transport, writes, killed: () => killed };
}

async function collectPrompt(session: HarnessSession): Promise<HarnessEvent[]> {
  const events: HarnessEvent[] = [];
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      (async () => { for await (const event of session.events()) events.push(event); })(),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error("ACP did not finish the turn while stdout stayed open")), 1_000);
      }),
    ]);
    return events;
  } finally {
    clearTimeout(timer);
    await session.close();
  }
}

describe("official Harness boundary", () => {
  test("lists the supported official Harnesses and prefers ACP where declared", () => {
    const definitions = officialHarnesses();
    expect(definitions.map((item) => item.id)).toEqual([
      "claude-code",
      "codex",
      "deepseek-harness",
      "zcode",
      "kimi-code",
      "pi",
      "qwen-code",
    ]);
    expect(chooseTransport(officialHarness("kimi-code")!, true)).toBe("acp");
    expect(chooseTransport(officialHarness("kimi-code")!, false)).toBe("official-cli");
    expect(chooseTransport(officialHarness("pi")!, true)).toBe("official-cli");
  });

  test("renders official CLI invocations without implementing a model loop", () => {
    const definition = officialHarness("codex")!;
    const argv = officialCliInvocation(definition, { task: "build", contract: "write tests", model: "codex", cwd: "/workspace" });
    expect(argv[0]).toBe("codex");
    expect(argv).toContain("--json");
    expect(argv.at(-1)).toContain("write tests");
  });

  test("uses the documented headless invocations for Pi and Qwen Code", () => {
    const request = { task: "build", contract: "write tests", model: "target/model", cwd: "/workspace" };
    expect(officialCliInvocation(officialHarness("pi")!, request)).toEqual([
      "pi", "--print", "--mode", "json", "--model", "target/model", "--", "write tests\n\nbuild",
    ]);
    expect(officialCliInvocation(officialHarness("pi")!, { ...request, resumeId: "session-1" })).toEqual([
      "pi", "--print", "--mode", "json", "--model", "target/model", "--session", "session-1", "--", "write tests\n\nbuild",
    ]);
    expect(officialCliInvocation(officialHarness("qwen-code")!, request)).toEqual([
      "qwen", "-p", "write tests\n\nbuild", "--model", "target/model", "--output-format", "stream-json",
    ]);
  });

  test("selects the exact advertised ACP model before sending the prompt", async () => {
    const definition = officialHarness("deepseek-harness")!;
    const option = { id: "backend-model", type: "select", category: "model", currentValue: "base",
      options: [{ group: "Available", options: [{ value: "target/model" }, { value: "base" }] }] };
    const created = (configOptions?: unknown[]) => frame({ jsonrpc: "2.0", id: 2, result: { sessionId: "session", configOptions } });
    const initialized = frame({ jsonrpc: "2.0", id: 1, result: { protocolVersion: 1 } });
    const selected = frame({ jsonrpc: "2.0", id: 4, result: { configOptions: [{ ...option, currentValue: "target/model" }] } });
    const promptResult = frame({ jsonrpc: "2.0", id: 3, result: { stopReason: "end_turn" } });
    const request = { ...acpRequest, model: "target/model", requireModelSelection: true };
    const supported = terminalFixture([initialized, created([option]), selected, promptResult], false, definition);
    const session = await supported.transport.start(request);
    expect(supported.writes[2]).toMatchObject({ method: "session/set_config_option",
      params: { sessionId: "session", configId: "backend-model", value: "target/model" } });
    expect(supported.writes[3].method).toBe("session/prompt");
    await collectPrompt(session);
    const unsupported = terminalFixture([initialized, created()], false, definition);
    await expect(unsupported.transport.start(request)).rejects.toThrow("未提供 ACP 模型选择");
    expect(unsupported.writes.map(({ method }) => method)).toEqual(["initialize", "session/new"]);
    const missing = terminalFixture([initialized, created([option])], false, definition);
    await expect(missing.transport.start({ ...request, model: "target" })).rejects.toThrow("未提供配置的模型");
    expect(missing.writes.map(({ method }) => method)).toEqual(["initialize", "session/new"]);
  });

  test("maps ACP JSON lines into bounded Harness events", () => {
    expect(decodeAcpLine('{"method":"session/update","params":{"sessionId":"s-1","delta":"hello"}}')).toMatchObject({ type: "text.delta", sessionId: "s-1", text: "hello" });
    expect(decodeAcpLine("not-json")).toBeNull();
  });

  test("maps official JSONL assistant records into realtime text events", () => {
    expect(decodeOfficialLine('{"type":"assistant","message":{"content":[{"type":"text","text":"已完成"}]}}')).toMatchObject({ type: "text.delta", text: "已完成" });
    expect(decodeOfficialLine('{"type":"result","text":"done"}')).toMatchObject({ type: "session.finished", text: "done" });
    expect(decodeOfficialLine('{"type":"mao.secret_request","name":"VIDEO_API_KEY"}')).toMatchObject({ type: "secret.request" });
    expect(decodeOfficialLine('{"type":"assistant","session_id":"q-1","message":{"content":[{"type":"text","text":"Qwen 完成"}]},"is_error":false}')).toMatchObject({ type: "text.delta", sessionId: "q-1", text: "Qwen 完成" });
  });

  test("maps Pi JSON mode events into bounded Harness events", () => {
    expect(decodeOfficialLine('{"type":"session","session":{"id":"pi-1"}}')).toMatchObject({ type: "session.started", sessionId: "pi-1" });
    expect(decodeOfficialLine('{"type":"message_update","sessionId":"pi-1","assistantMessageEvent":{"type":"text_delta","delta":"Pi 回复"}}')).toMatchObject({ type: "text.delta", sessionId: "pi-1", text: "Pi 回复" });
    expect(decodeOfficialLine('{"type":"agent_end","sessionId":"pi-1"}')).toMatchObject({ type: "session.finished", sessionId: "pi-1" });
  });

  test("initializes ACP, creates a session and sends the prompt through stdin", async () => {
    const writes: string[] = [];
    const body = [
      JSON.stringify({ jsonrpc: "2.0", id: 1, result: { protocolVersion: 1 } }),
      JSON.stringify({ jsonrpc: "2.0", id: 2, result: { sessionId: "s-1" } }),
      JSON.stringify({ method: "session/update", params: { sessionId: "s-1", delta: "hello" } }),
      JSON.stringify({ method: "session/complete", params: { sessionId: "s-1" } }),
    ].join("\n") + "\n";
    const transport = new AcpTransport(officialHarness("kimi-code")!, () => ({
      stdout: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(body)); controller.close(); } }),
      stdin: { write(data) { writes.push(new TextDecoder().decode(data)); }, end() {} },
      kill() {},
    }));
    const session = await transport.start({ task: "构建项目", contract: "你是厂长", model: "kimi", cwd: "/workspace" });
    expect(session.id).toBe("s-1");
    const events = [];
    for await (const event of session.events()) events.push(event);
    const messages = writes.join("").trim().split("\n").map((line) => JSON.parse(line));
    expect(messages.map((message) => message.method)).toEqual(["initialize", "session/new", "session/prompt"]);
    expect(messages[2].params.prompt[0].text).toContain("构建项目");
    expect(events[0]).toMatchObject({ type: "text.delta", sessionId: "s-1", text: "hello" });
    expect(events.at(-1)?.type).toBe("session.finished");
    await session.close();
  });

  test("finishes an official ACP turn on the prompt response while stdout stays open", async () => {
    const update = frame({
      jsonrpc: "2.0", method: "session/update",
      params: { sessionId: "real", update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "回复 😀" } } },
    });
    const split = update.indexOf(0xf0) + 1;
    const { transport, writes, killed } = terminalFixture([
      frame({ jsonrpc: "2.0", id: 1, result: { protocolVersion: 1 } }),
      frame({ jsonrpc: "2.0", id: 2, result: { sessionId: "real" } }),
      frame({ jsonrpc: "2.0", method: "session/update", params: { sessionId: "real", update: { sessionUpdate: "agent_thought_chunk", content: { type: "text", text: "internal" } } } }),
      update.subarray(0, split), update.subarray(split),
      frame({ jsonrpc: "2.0", id: 3, method: "fs/read_text_file", params: { sessionId: "real", path: "/workspace/example" } }),
      frame({ jsonrpc: "2.0", id: 7, result: {} }),
      frame({ jsonrpc: "2.0", id: 3, result: { stopReason: "end_turn" } }),
    ], true);
    const session = await transport.start({ ...acpRequest, resumeId: "old" });
    expect(session.id).toBe("real");
    expect(await collectPrompt(session)).toMatchObject([
      { type: "text.delta", sessionId: "real", text: "回复 😀" },
      { type: "session.finished", sessionId: "real", metadata: { stopReason: "end_turn" } },
    ]);
    expect(writes.map((item) => item.method)).toEqual(["initialize", "session/new", "session/prompt", undefined]);
    expect(writes[2].params?.sessionId).toBe("real");
    expect(writes[3]).toMatchObject({ id: 3, error: { code: -32601 } });
    expect(killed()).toBe(true);
  });

  test("uses the previous ACP session ID without publishing history replay as the current reply", async () => {
    const { transport, writes } = terminalFixture([
      frame({ jsonrpc: "2.0", id: 1, result: { protocolVersion: 1, agentCapabilities: { loadSession: true } } }),
      frame({ jsonrpc: "2.0", method: "session/update", params: { sessionId: "prior", update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "old reply" } } } }),
      frame({ jsonrpc: "2.0", id: 2, result: {} }),
      frame({ jsonrpc: "2.0", id: 3, result: { stopReason: "end_turn" } }),
    ], true);
    const session = await transport.start({ ...acpRequest, resumeId: "prior" });
    expect(session.id).toBe("prior");
    expect(writes[1]).toMatchObject({ method: "session/load", params: { sessionId: "prior", mcpServers: [] } });
    expect(await collectPrompt(session)).toEqual([{ type: "session.finished", sessionId: "prior", metadata: { stopReason: "end_turn" } }]);
  });

  test("creates a new ACP session with a distinct request ID if the previous session cannot load", async () => {
    const { transport, writes } = terminalFixture([
      frame({ jsonrpc: "2.0", id: 1, result: { protocolVersion: 1, agentCapabilities: { loadSession: true } } }),
      frame({ jsonrpc: "2.0", id: 2, error: { code: -32602, message: "unknown session" } }),
      frame({ jsonrpc: "2.0", id: 4, result: { sessionId: "new-session" } }),
      frame({ jsonrpc: "2.0", id: 3, result: { stopReason: "end_turn" } }),
    ], true);
    const session = await transport.start({ ...acpRequest, resumeId: "prior" });
    expect(session.id).toBe("new-session");
    expect(writes.map(({ id, method }) => [id, method])).toEqual([
      [1, "initialize"], [2, "session/load"], [4, "session/new"], [3, "session/prompt"],
    ]);
    expect(await collectPrompt(session)).toMatchObject([{ type: "session.finished", sessionId: "new-session" }]);
  });

  test("reports prompt errors and premature EOF as terminal failures", async () => {
    for (const [frameAfterStart, expected] of [
      [frame({ jsonrpc: "2.0", id: 3, error: { code: -32603, message: "raw model error" } }), { reason: "ACP session/prompt failed (JSON-RPC -32603)" }],
      [frame({ jsonrpc: "2.0", id: 3, result: { stopReason: "max_tokens" } }), { stopReason: "max_tokens" }],
      [undefined, { reason: "ACP closed before session/prompt response" }],
    ] as const) {
      const { transport } = terminalFixture([
        frame({ jsonrpc: "2.0", id: 1, result: { protocolVersion: 1 } }),
        frame({ jsonrpc: "2.0", id: 2, result: { sessionId: "s" } }),
        ...(frameAfterStart ? [frameAfterStart] : []),
      ], Boolean(frameAfterStart));
      const session = await transport.start(acpRequest);
      expect(await collectPrompt(session)).toEqual([{ type: "session.failed", sessionId: "s", metadata: expected }]);
    }
  });

  test("bounds an unterminated ACP frame split across chunks", async () => {
    const { transport, killed } = terminalFixture([
      frame({ jsonrpc: "2.0", id: 1, result: { protocolVersion: 1 } }),
      frame({ jsonrpc: "2.0", id: 2, result: { sessionId: "s" } }),
      new Uint8Array(768 * 1024).fill(65),
      new Uint8Array(512 * 1024).fill(65),
    ], true);
    const session = await transport.start(acpRequest);
    expect(await collectPrompt(session)).toEqual([{ type: "session.failed", sessionId: "s", metadata: { reason: "ACP message exceeds 1 MiB" } }]);
    expect(killed()).toBe(true);
  });

  test("passes the requested managed network and material files to official transports", async () => {
    const materialFiles = { outputDirectory: "/stage/outputs", inputs: [{ index: 1, source: "/stage/inputs/1" }] };
    let observed: { network?: string; image?: string; materialFiles?: typeof materialFiles } | undefined;
    const body = [
      JSON.stringify({ jsonrpc: "2.0", id: 1, result: { protocolVersion: 1 } }),
      JSON.stringify({ jsonrpc: "2.0", id: 2, result: { sessionId: "s-egress" } }),
      JSON.stringify({ method: "session/complete", params: { sessionId: "s-egress" } }),
    ].join("\n") + "\n";
    const transport = new AcpTransport(officialHarness("kimi-code")!, (_argv, options) => {
      observed = { network: options.network, image: options.containerImage, materialFiles: options.materialFiles };
      return {
        stdout: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(body)); controller.close(); } }),
        stdin: { write() {}, end() {} },
        kill() {},
      };
    });
    const containerImage = "registry.example/harness@sha256:" + "a".repeat(64);
    const request = { ...acpRequest, network: "egress" as const, containerImage, materialFiles };
    const session = await transport.start(request);
    for await (const _event of session.events()) { /* drain */ }
    const expected = { network: "egress", image: containerImage, materialFiles };
    expect(observed).toEqual(expected);
    await session.close();
    observed = undefined;
    const cli = new OfficialCliTransport(officialHarness("codex")!, (_argv, options) => {
      observed = { network: options.network, image: options.containerImage, materialFiles: options.materialFiles };
      return { stdout: new ReadableStream({ start(controller) { controller.close(); } }), kill() {} };
    });
    await (await cli.start(request)).close();
    expect(observed).toEqual(expected);
  });
});
