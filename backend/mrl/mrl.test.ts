import { describe, expect, test } from "bun:test";
import { evaluate, MrlEvalError, parseExpression, queryRelation, tokenize } from "./index";

describe("TypeScript MRL compatibility layer", () => {
  test("lexes literals, comments and operators", () => {
    expect(tokenize("1 + // comment\n 2").map((token) => token.text)).toEqual(["1", "+", "2", ""]);
  });
  test("evaluates arithmetic, records, lists and access", () => {
    expect(evaluate("{\"items\": [1, 2, 3]}.items[1] * 2")).toBe(4);
    expect(evaluate("r.name + \"!\"", { r: { name: "MRL" } })).toBe("MRL!");
  });
  test("short circuits and runs collection macros", () => {
    expect(evaluate("false && (1 / 0 > 1)")).toBe(false);
    expect(evaluate("[1, 2, 3].filter(x, x > 1)")).toEqual([2, 3]);
    expect(evaluate("[1, 2, 3].map(x, x * 2)")).toEqual([2, 4, 6]);
  });
  test("reports stable MRL runtime codes", () => {
    expect(() => evaluate("1 / 0")).toThrow(MrlEvalError);
    try { evaluate("1 / 0"); } catch (error) { expect((error as MrlEvalError).code).toBe("MRL-DIV-ZERO"); }
  });
  test("filters named relation rows", () => {
    expect(queryRelation([{ id: "a", size: 2 }, { id: "b", size: 4 }], "size >= 3")).toEqual([{ id: "b", size: 4 }]);
  });
  test("parses an expression AST", () => { expect(parseExpression("a + 1").kind).toBe("binary"); });
});
