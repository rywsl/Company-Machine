import { describe, expect, test } from "bun:test";
import { chooseTransport, officialCliInvocation, officialHarness, officialHarnesses } from "../src/harness";

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
});
