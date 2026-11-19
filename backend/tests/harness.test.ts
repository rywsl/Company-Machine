import { describe, expect, test } from "bun:test";
import { chooseTransport, decodeOfficialLine, officialCliInvocation, officialHarness, officialHarnesses } from "../src/harness";
import { decodeAcpLine } from "../src/acp";
import { AcpTransport } from "../src/acp";

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
    const events = [];
    for await (const event of session.events()) events.push(event);
    const messages = writes.join("").trim().split("\n").map((line) => JSON.parse(line));
    expect(messages.map((message) => message.method)).toEqual(["initialize", "session/new", "session/prompt"]);
    expect(messages[2].params.prompt[0].text).toContain("构建项目");
    expect(events[0]).toMatchObject({ type: "text.delta", sessionId: "s-1", text: "hello" });
    await session.close();
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
