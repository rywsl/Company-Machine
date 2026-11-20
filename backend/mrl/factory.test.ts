import { describe, expect, test } from "bun:test";
import { checkRegistry } from "./factory/check";
import { layoutKind, loadRegistry } from "./factory/registry";
import { validate } from "./factory/schema";
import { loadDocuments } from "./factory/yamlio";

describe("Bun MRL factory registry", () => {
  test("loads the M1 and M2 fixture registries through the active Bun path", () => {
    const m1 = checkRegistry("tests/fixtures/registry-m1");
    const m2 = checkRegistry("tests/fixtures/registry-m2");
    expect(m1.ok).toBe(true);
    expect(m2.ok).toBe(true);
    expect(m1.stats["kind:Type"]).toBe(2);
    expect(m2.stats["kind:Line"]).toBe(6);
  });

  test("classifies the normative registry layout and ignores helper files", () => {
    expect(layoutKind("types/note.yaml")).toBe("Type");
    expect(layoutKind("machines/studio/machine.yaml")).toBe("Machine");
    expect(layoutKind("fixtures/note/ok.json")).toBeUndefined();
    const registry = loadRegistry("tests/fixtures/registry-m1");
    expect(registry.docs.every((document) => document.file.endsWith(".yaml"))).toBe(true);
  });

  test("supports YAML documents and reports schema errors", () => {
    const documents = loadDocuments("---\n---\na: true\n---\nb: on\n");
    expect(documents.map((document) => document.data)).toEqual([{ a: true }, { b: "on" }]);
    expect(documents.map((document) => document.index)).toEqual([2, 3]);
    expect(validate({ apiVersion: "mao/v1", kind: "Type", metadata: { name: "bad", version: "nope" }, spec: {} }).length).toBeGreaterThan(0);
  });
});
