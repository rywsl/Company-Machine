import { expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { registryFixtureNames, registryLiteralProblems } from "./registry-policy";
import { activePolicyProblems } from "./spec-consistency";

function withTree(run: (root: string, write: (name: string, source: string) => void) => void): void {
  const root = mkdtempSync(join(tmpdir(), "mao-registry-policy-"));
  const write = (name: string, source: string) => {
    const file = join(root, name);
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, source, "utf8");
  };
  try { run(root, write); }
  finally { rmSync(root, { recursive: true, force: true }); }
}

test("collects fixture identities only from declared Registry kinds and directories", () => {
  withTree((root, write) => {
    write("spec/examples/registry/stations/sample.yaml",
      "kind: Station\nmetadata: {name: sample-station}\n---\nkind: Policy\nmetadata: {name: unrelated-policy}\n");
    write("tests/fixtures/registry-new/machines/example.yml", "kind: Machine\nmetadata: {name: toolbox}\n");
    write("tests/fixtures/registry-new/stations/common.yaml", "kind: Station\nmetadata: {name: common}\n");
    write("tests/fixtures/registry-new/broken.yaml", "kind: [\n");
    write("tests/fixtures/generic/line.yaml", "kind: Line\nmetadata: {name: outside-line}\n");
    expect([...registryFixtureNames(root)].sort()).toEqual(["sample-station", "toolbox"]);
  });
});

test("reports fixture and infrastructure literals including template segments and escaped strings", () => {
  const source = [
    '// "Station:sample-station@1" in a comment',
    'const exact = "sample-station";',
    'const ref = `Machine:toolbox@${version}`;',
    'const alias = "test\\u0073/path";',
    'const root = `${base}/registry-m2`;',
    'const safe = "discard-sample-station";',
    'const project = "writer";',
  ].join("\n");
  expect(registryLiteralProblems("backend/src/example.ts", source, new Set(["sample-station", "toolbox"]))).toEqual([
    'backend/src/example.ts:2:15 控制面禁止写死 Registry 夹具名称 "sample-station"',
    'backend/src/example.ts:3:13 控制面禁止写死 Registry 夹具名称 "toolbox"',
    'backend/src/example.ts:4:15 控制面禁止引用测试基础设施 "tests/"',
    'backend/src/example.ts:5:21 控制面禁止引用测试基础设施 "registry-m2"',
  ]);
});

test("runs the literal policy for active production sources and leaves tests and docs available for fixtures", () => {
  withTree((root, write) => {
    write("tests/fixtures/registry-live/stations/sample.yaml", "kind: Station\nmetadata: {name: sample-station}\n");
    write("backend/src/service.ts", 'export const name = "Station:sample-station@1";');
    write("backend/src/service.test.ts", 'export const name = "Station:sample-station@1";');
    write("backend/src/service.md", '"Station:sample-station@1"');
    write("tools/service.ts", 'export const name = "Station:sample-station@1";');
    expect(activePolicyProblems(root)).toEqual([
      'backend/src/service.ts:1:21 控制面禁止写死 Registry 夹具名称 "sample-station"',
    ]);
  });
});
