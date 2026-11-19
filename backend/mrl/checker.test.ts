import { describe, expect, test } from "bun:test";
import { BOOL, MrlStaticError, checkExpression, compileTemplate, parseType, typeName } from "./index";

describe("TypeScript MRL static checker and templates", () => {
  test("checks types and reports optional field warnings", () => {
    const result = checkExpression("r.x > 0", { environment: { r: "{x?: int}" } });
    expect(result.type).toEqual(BOOL);
    expect(result.warnings.map((item) => item.code)).toEqual(["MRL-MAYBE-ABSENT"]);
    expect(checkExpression("has(r.x) && r.x > 0", { environment: { r: "{x?: int}" } }).warnings).toHaveLength(0);
  });

  test("rejects undefined names and incompatible operators", () => {
    expect(() => checkExpression("missing + 1")).toThrow(MrlStaticError);
    try { checkExpression("\"a\" - 1"); } catch (error) {
      expect((error as MrlStaticError).diagnostics.some((item) => item.code === "MRL-TYPE")).toBe(true);
    }
  });

  test("supports typed records, macros and expected types", () => {
    expect(checkExpression("items.all(item, item.ready)", { environment: { items: "list<{ready: bool}>" }, expect: "bool" }).type).toEqual(BOOL);
    expect(() => checkExpression("1", { expect: "bool" })).toThrow(MrlStaticError);
    expect(typeName(parseType("{name: string, tags?: list<string>, ...}"))).toBe("{name: string, tags?: list<string>, ...}");
  });

  test("compiles and renders human-readable templates", () => {
    const template = compileTemplate("artifact={{ name }} ready={{ ready }}", { environment: { name: "string", ready: "bool" } });
    expect(template.render({ name: "脚本", ready: true })).toBe("artifact=脚本 ready=true");
    expect(() => compileTemplate("{{ items }}", { environment: { items: "list<string>" } })).toThrow(MrlStaticError);
    expect(compileTemplate("{{ {\"name\": name}.name }}", { environment: { name: "string" } }).render({ name: "x" })).toBe("x");
  });
});
