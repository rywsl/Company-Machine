import { describe, expect, test } from "bun:test";
import { chooseTransport, decodeOfficialLine, officialCliInvocation, officialHarness, officialHarnesses } from "../src/harness";
import { decodeAcpLine } from "../src/acp";

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
  });
});
