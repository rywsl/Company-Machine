import { expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { modulePolicyProblems } from "./module-policy";

function scan(files: Record<string, string>): string[] {
  const root = mkdtempSync(join(tmpdir(), "mao-module-policy-"));
  try {
    const paths = Object.entries(files).map(([name, source]) => {
      const file = join(root, name);
      mkdirSync(dirname(file), { recursive: true });
      writeFileSync(file, source, "utf8");
      return file;
    });
    return modulePolicyProblems(root, paths);
  } finally { rmSync(root, { recursive: true, force: true }); }
}

test("reports imported object mutations without confusing local variables for imports", () => {
  expect(scan({
    "backend/src/config.ts": "export default { value: 0 }; export const config = { value: 0, nested: { flag: true } };",
    "backend/src/service.ts": [
      'import setting, { config as shared } from "./config";',
      'setting.value = 1;',
      'shared["value"]++;',
      'delete shared.nested.flag;',
      'Reflect.set((setting), "value", 2);',
      'Object.assign(shared, { value: 3 });',
      'function edit(shared: { value: number }) { shared.value = 4; }',
      'const copy = { ...setting }; copy.value = 5;',
    ].join("\n"),
  })).toEqual([2, 3, 4, 5, 6].map((line) => `backend/src/service.ts:${line}:${line === 4 ? 8 : 1} 控制面禁止修改导入对象`));
});

test("reports bracket and reflection access to other classes' private members", () => {
  expect(scan({
    "backend/src/config.ts": [
      'export class Config {',
      '  private secret = "x";',
      '  show() { return this["secret"]; }',
      '}',
    ].join("\n"),
    "backend/src/service.ts": [
      'import { Config } from "./config";',
      'const item = new Config();',
      'item["secret"];',
      'Reflect.get(item as object, "secret");',
      'Object.getOwnPropertyDescriptor(item, "secret");',
      '(item as any).secret;',
      'item.show();',
      '({ secret: "ordinary key" })["secret"];',
    ].join("\n"),
  })).toEqual([3, 4, 5, 6].map((line) => `backend/src/service.ts:${line}:1 控制面禁止绕过私有成员 secret 的访问边界`));
});

test("reserves stdout for CLI and executable entrypoints while permitting stderr diagnostics", () => {
  expect(scan({
    "backend/src/cli.ts": 'console.log("command result"); process.stdout.write("result");',
    "backend/src/library.ts": [
      'import { log as print } from "node:console";',
      'import processAlias from "node:process";',
      'console.log("library result");',
      'globalThis.console["info"]("library info");',
      'processAlias.stdout.write("result");',
      'Bun.write(Bun.stdout, "result");',
      'console.error("diagnostic");',
      'processAlias.stderr.write("diagnostic");',
      'function call(console: { log(s: string): void }) { console.log("local callback"); }',
      'const source = `console.log("contained script")`;',
      'const { log: write } = console;',
    ].join("\n"),
  })).toEqual([[1, 10], [3, 1], [4, 1], [5, 1], [6, 11], [11, 9]].map(([line, col]) =>
    `backend/src/library.ts:${line}:${col} 控制面标准输出仅用于命令行和进程入口`));
});

test("reserves wall time, elapsed time, ID randomness and cwd for their boundaries", () => {
  const problems = scan({
    "backend/src/legacy/foundation.ts": 'import { randomUUID } from "node:crypto"; Date.now(); randomUUID();',
    "backend/src/legacy/factoryctl.ts": 'import { randomUUID } from "node:crypto"; performance.now(); randomUUID();',
    "backend/src/vault.ts": 'import { randomBytes } from "node:crypto"; randomBytes(32);',
    "backend/src/cli.ts": 'process.cwd();',
    "backend/src/library.ts": [
      'import { randomUUID as uuid } from "node:crypto";',
      'import { cwd as where } from "node:process";',
      'new Date();',
      'Date.now();',
      'globalThis.performance["now"]();',
      'uuid();',
      'Math.random();',
      'where();',
      'process.chdir("/a");',
      'new Date(123);',
      'function local(Date: { now(): number }) { return Date.now(); }',
    ].join("\n"),
  });
  expect(problems).toEqual([
    ...[3, 4, 5].map((line) => `backend/src/library.ts:${line}:1 控制面时间来自注入的 Clock`),
    ...[6, 7].map((line) => `backend/src/library.ts:${line}:1 控制面 ID 来自 IdSource 或 foundation 中的 UUID 生成器`),
    ...[8, 9].map((line) => `backend/src/library.ts:${line}:1 控制面工作目录来自配置或调用方参数`),
  ]);
});
