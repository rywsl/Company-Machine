import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import ts from "../frontend/node_modules/typescript/lib/typescript.js";
import { listFiles } from "../backend/mrl/factory/paths";
import { loadDocuments, YamlError } from "../backend/mrl/factory/yamlio";

const registryKinds = new Set(["Station", "Line", "Machine", "GoldenSet", "Baseline"]);
const markerPaths = ["tests/", "tests\\", "registry-m1", "registry-m2", "spec/examples", "fake_agent", "fake-agent", "fakelib"];
// Preserve the archived policy's unhyphenated names. Each still requires an actual declaration.
const unhyphenatedNames = new Set([
  "explainer", "upper", "quote", "toolbox", "studio", "vertical",
  "openmontage", "moneyprinter", "servicebox", "idlebox", "writer",
]);

function mapping(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

export function registryFixtureNames(root: string): Set<string> {
  const fixtures = join(root, "tests/fixtures");
  const registries = [join(root, "spec/examples/registry")];
  if (existsSync(fixtures)) {
    for (const entry of readdirSync(fixtures, { withFileTypes: true })) {
      if (entry.isDirectory() && entry.name.startsWith("registry-")) registries.push(join(fixtures, entry.name));
    }
  }
  const names = new Set<string>();
  for (const registry of registries.sort()) {
    for (const file of listFiles(registry)) {
      if (!/\.ya?ml$/.test(file)) continue;
      try {
        for (const { data } of loadDocuments(readFileSync(file, "utf8"))) {
          const document = mapping(data);
          if (typeof document?.kind !== "string" || !registryKinds.has(document.kind)) continue;
          const name = mapping(document.metadata)?.name;
          if (typeof name === "string" && (name.includes("-") || unhyphenatedNames.has(name))) names.add(name);
        }
      } catch (error) {
        if (!(error instanceof YamlError)) throw error;
      }
    }
  }
  return names;
}

export function registryLiteralProblems(file: string, text: string, names: ReadonlySet<string>): string[] {
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
  const problems: string[] = [];
  const inspect = (node: ts.Node): void => {
    if (ts.isStringLiteralLike(node) || ts.isTemplateLiteralToken(node)) {
      const literal = node.text;
      const { line, character } = source.getLineAndCharacterOfPosition(node.getStart(source));
      const location = file + ":" + (line + 1) + ":" + (character + 1);
      for (const marker of markerPaths) {
        if (literal.includes(marker)) problems.push(location + " 控制面禁止引用测试基础设施 " + JSON.stringify(marker));
      }
      for (const name of names) {
        if (literal === name || literal.startsWith(name + "@") || literal.includes(":" + name + "@")) {
          problems.push(location + " 控制面禁止写死 Registry 夹具名称 " + JSON.stringify(name));
        }
      }
    }
    ts.forEachChild(node, inspect);
  };
  inspect(source);
  return problems;
}
