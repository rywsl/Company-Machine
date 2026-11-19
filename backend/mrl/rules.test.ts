import { describe, expect, test } from "bun:test";
import { MrlRuleError, buildRuleProgram, parseRuleModule } from "./index";

describe("TypeScript MRL rule programs", () => {
  test("parses declarations, facts and clauses", () => {
    const module = parseRuleModule(`module demo.\n@input rel parent(parent: string, child: string).\nrel grandparent(grand: string, child: string).\ngrandparent(G, C) :- parent(G, P), parent(P, C).`);
    expect(module.name).toBe("demo");
    expect(module.relations.map((item) => item.name)).toEqual(["parent", "grandparent"]);
    expect(module.rules[0].body).toHaveLength(2);
  });

  test("evaluates joins and expression guards", () => {
    const program = buildRuleProgram([{ name: "demo", source: `module demo.\n@input rel parent(parent: string, child: string).\n@input rel score(name: string, value: int).\nrel grandparent(grand: string, child: string).\nrel high(name: string).\ngrandparent(G, C) :- parent(G, P), parent(P, C).\nhigh(N) :- score(N, V), V > 10.` }]);
    const model = program.evaluate({ parent: [["a", "b"], ["b", "c"], ["b", "d"]], score: [["a", 12], ["b", 2]] });
    expect(model.rowsOf("grandparent")).toEqual([["a", "c"], ["a", "d"]]);
    expect(model.rowsOf("high")).toEqual([["a"]]);
  });

  test("supports facts and detects unknown relations", () => {
    const program = buildRuleProgram([{ name: "demo", source: `module demo.\nrel flag(name: string).\nflag("ready").` }]);
    expect(program.evaluate().rowsOf("flag")).toEqual([["ready"]]);
    expect(() => buildRuleProgram([{ name: "bad", source: `module bad.\nunknown("x").` }])).toThrow(MrlRuleError);
  });

  test("evaluates negated atoms against the current binding", () => {
    const program = buildRuleProgram([{ name: "demo", source: `module demo.\n@input rel item(name: string).\n@input rel blocked(name: string).\nrel available(name: string).\navailable(N) :- item(N), !blocked(N).` }]);
    const model = program.evaluate({ item: [["a"], ["b"]], blocked: [["b"]] });
    expect(model.rowsOf("available")).toEqual([["a"]]);
  });
});
