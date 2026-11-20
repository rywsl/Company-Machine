import { describe, expect, test } from "bun:test";
import { chooseTransport, decodeOfficialLine, officialCliInvocation, officialHarness, officialHarnesses } from "../src/harness";
import { AcpTransport, decodeAcpLine } from "../src/acp";
import type { HarnessEvent, HarnessSession } from "../src/harness";

const acpRequest = { task: "构建项目", contract: "你是厂长", model: "kimi", cwd: "/workspace" };
const acpDefinition = officialHarness("kimi-code")!;
const encoder = new TextEncoder();

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
    ]);
    expect(chooseTransport(officialHarness("kimi-code")!, true)).toBe("acp");
    expect(chooseTransport(officialHarness("kimi-code")!, false)).toBe("official-cli");
  });

  test("renders official CLI invocations without implementing a model loop", () => {
    const definition = officialHarness("codex")!;
    const argv = officialCliInvocation(definition, { task: "build", contract: "write tests", model: "codex", cwd: "/workspace" });
    expect(argv[0]).toBe("codex");
    expect(argv).toContain("--json");
    expect(argv.at(-1)).toContain("write tests");
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

  test("passes the requested managed network to ACP spawn", async () => {
    let observed: { network?: string; image?: string } | undefined;
    const body = [
      JSON.stringify({ jsonrpc: "2.0", id: 1, result: { protocolVersion: 1 } }),
      JSON.stringify({ jsonrpc: "2.0", id: 2, result: { sessionId: "s-egress" } }),
      JSON.stringify({ method: "session/complete", params: { sessionId: "s-egress" } }),
    ].join("\n") + "\n";
    const transport = new AcpTransport(officialHarness("kimi-code")!, (_argv, options) => {
      observed = { network: options.network, image: options.containerImage };
      return {
        stdout: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(body)); controller.close(); } }),
        stdin: { write() {}, end() {} },
        kill() {},
      };
    });
    const session = await transport.start({ task: "构建", contract: "你是厂长", model: "kimi", cwd: "/workspace", network: "egress", containerImage: "registry.example/harness@sha256:" + "a".repeat(64) });
    for await (const _event of session.events()) { /* drain */ }
    expect(observed).toEqual({ network: "egress", image: "registry.example/harness@sha256:" + "a".repeat(64) });
    await session.close();
  });
});
