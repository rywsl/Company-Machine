import { readFileSync, readdirSync } from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";
import { listFiles } from "../backend/mrl/factory/paths";
import { standardFunctions } from "../backend/mrl/evaluator";
import { modulePolicyProblems } from "./module-policy";
import { registryFixtureNames, registryLiteralProblems } from "./registry-policy";

export const acceptanceRoots = ["backend/tests", "backend/runtime", "backend/mrl", "tests/fakes"];
const testingSpec = "spec/system/testing.md";
const archiveRoot = "removed/python/tests/acceptance";
const chapters = [
  "目的与范围", "依赖", "接口", "数据格式", "行为", "错误",
  "验收测试", "错误示范与正确示范", "风险与注意事项",
];

export interface AcceptanceCase {
  file: string;
  name: string;
  passed: boolean;
}

function read(root: string, path: string): string {
  return readFileSync(join(root, path), "utf8");
}

function relativePath(root: string, path: string): string {
  return (isAbsolute(path) ? relative(root, path) : path).replaceAll("\\", "/");
}

function codeWords(source: string, pattern: RegExp): Set<string> {
  return new Set([...source.matchAll(pattern)].map((match) => match[1]));
}

export function inspectSpecifications(root: string) {
  const problems: string[] = [];
  const historical: string[] = [];
  const archiveClaims = new Map<string, Set<string>>();
  const files = readdirSync(join(root, "spec/system"))
    .filter((file) => file.endsWith(".md") && file !== "README.md").sort();
  for (const file of files) {
    const path = `spec/system/${file}`;
    const source = read(root, path);
    let previous = -1;
    for (const [index, title] of chapters.entries()) {
      const number = index + 1;
      const headings = [...source.matchAll(new RegExp(`^##[ \\t]+${number}[.、]?[ \\t]+([^\\r\\n]+)`, "gm"))];
      if (headings.length !== 1 || !headings[0][1].startsWith(title)) {
        problems.push(`${path}: 第 ${number} 章必须恰好有一个「${title}」标题`);
      } else {
        if (headings[0].index <= previous) problems.push(`${path}: 第 ${number} 章顺序错误`);
        previous = headings[0].index;
      }
    }

    const acceptance = source.match(/^##[ \t]+7[.、]?[ \t]+验收测试[^\r\n]*\r?\n([\s\S]*?)(?=^##[ \t]|(?![\s\S]))/m)?.[1] ?? "";
    let currentArchive: Set<string> | undefined;
    for (const line of acceptance.split(/\r?\n/)) {
      const archive = [...line.matchAll(/`((?:removed\/python\/)?tests\/acceptance\/(?:[\w.-]+\/)*test_[\w.-]+\.py)`/g)].at(-1)?.[1];
      if (archive) {
        const path = archive.startsWith("removed/") ? archive : `removed/python/${archive}`;
        currentArchive = archiveClaims.get(path) ?? new Set();
        archiveClaims.set(path, currentArchive);
      }
      if (line.trimStart().startsWith("|") && currentArchive) {
        for (const cell of line.trim().split("|").slice(1, 3)) {
          for (const name of codeWords(cell, /`(test_\w+)`/g)) currentArchive.add(name);
        }
      }
    }
  }
  if (files.length === 0) problems.push("spec/system 缺少组件规范");

  const normative = read(root, "spec/mrl/mrl-1.0.md");
  const profile = read(root, "spec/mrl/factory-profile.md");
  const mrlErrors = new Set<string>();
  for (const file of listFiles(join(root, "backend/mrl")).filter((file) => file.endsWith(".ts") && !file.endsWith(".test.ts"))) {
    for (const code of codeWords(readFileSync(file, "utf8"), /["'](MRL-[A-Z_-]+)["']/g)) mrlErrors.add(code);
  }
  for (const code of mrlErrors) {
    if (!normative.includes(code)) problems.push(`mrl-1.0.md 缺少诊断码 ${code}`);
  }
  for (const name of Object.keys(standardFunctions)) {
    if (!new RegExp(`\`${name}\\b|\\b${name}\\(`).test(normative)) {
      problems.push(`mrl-1.0.md 缺少标准库函数 ${name}`);
    }
  }

  const kernelCodes = new Set<string>();
  const diagnosticRules: Array<{ file: string; line: number }> = [];
  for (const file of listFiles(join(root, "spec/mrl/kernel")).filter((file) => file.endsWith(".mrl"))) {
    const source = readFileSync(file, "utf8");
    for (const code of codeWords(source, /^code\("([^"]+)"/gm)) kernelCodes.add(code);
    for (const relation of codeWords(source, /^@input\s+rel\s+(\w+)\(/gm)) {
      if (!profile.includes(`\`${relation}(`) && !profile.includes(`\`${relation}\``)) {
        problems.push(`factory-profile.md 缺少输入关系 ${relation}`);
      }
    }
    for (const [index, line] of source.split(/\r?\n/).entries()) {
      if (/^diag\s*\(/.test(line)) diagnosticRules.push({ file: relativePath(root, file), line: index + 1 });
    }
  }
  if (kernelCodes.size === 0 || diagnosticRules.length === 0) problems.push("factory profile 缺少内核诊断码或诊断规则");
  for (const code of kernelCodes) {
    if (!profile.includes(`\`${code}\``)) problems.push(`factory-profile.md 缺少诊断码 ${code}`);
  }

  const expectedFiles = new Set(acceptanceRoots.flatMap((path) => listFiles(join(root, path)))
    .filter((path) => path.endsWith(".test.ts")).map((path) => relativePath(root, path)));
  const activeSection = read(root, testingSpec).split(/^### 7\.2\b/m)[0].split(/^### 7\.1\b/m)[1] ?? "";
  const mappedFiles = codeWords(activeSection, /^\|\s*`([^\s`]+\.test\.ts)`\s*\|/gm);
  for (const file of mappedFiles) {
    if (!expectedFiles.has(file)) problems.push(`${testingSpec}: 活动验收文件不存在或不在验收目录 ${file}`);
  }
  for (const file of expectedFiles) {
    if (!mappedFiles.has(file)) problems.push(`${testingSpec}: 缺少活动验收映射 ${file}`);
    const source = read(root, file);
    if (/\b(?:test|it|describe)\s*\.\s*only\b/.test(source)) {
      problems.push(`${file}: 验收测试必须运行完整文件，不能使用 .only`);
    }
  }

  const obligations = new Set<string>();
  const archivedFiles = listFiles(join(root, archiveRoot)).filter((file) => /(^|[\\/])test_\w+\.py$/.test(file));
  for (const file of archivedFiles) {
    const path = relativePath(root, file);
    const names = codeWords(readFileSync(file, "utf8"), /^(?:async )?def (test_\w+)\(/gm);
    for (const name of names) obligations.add(`${path}::${name}`);
    for (const name of names) {
      if (!archiveClaims.get(path)?.has(name)) historical.push(`${path}::${name} 缺少组件规范第 7 章引用`);
    }
  }
  for (const [file, names] of archiveClaims) {
    for (const name of names) {
      if (!obligations.has(`${file}::${name}`)) historical.push(`${file}::${name} 缺少历史用例定义`);
    }
  }
  if (archivedFiles.length === 0) problems.push(`找不到历史验收目录 ${archiveRoot}`);

  const evidence = new Map<string, Array<{ file: string; name: string }>>();
  const archiveSection = read(root, testingSpec).split(/^### 7\.2\b/m)[1]?.split(/^## 8\b/m)[0] ?? "";
  for (const match of archiveSection.matchAll(/^\|\s*`(removed\/python\/tests\/acceptance\/[^\s`]+\.py::test_\w+)`\s*\|\s*`([^\s`]+\.test\.ts)`\s*\|\s*`([^`]+)`\s*\|/gm)) {
    const [, obligation, file, name] = match;
    if (!obligations.has(obligation)) problems.push(`${testingSpec}: 历史用例不存在 ${obligation}`);
    if (!mappedFiles.has(file)) problems.push(`${testingSpec}: 等价证据文件缺少活动映射 ${file}`);
    const cases = evidence.get(obligation) ?? [];
    cases.push({ file, name });
    evidence.set(obligation, cases);
  }
  return { problems, historical, obligations, evidence, mappedFiles, kernelCodes, diagnosticRules, archivedFiles: archivedFiles.length, componentSpecs: files.length };
}

export function activePolicyProblems(root: string): string[] {
  const problems: string[] = [];
  const sources = ["backend/src", "backend/runtime", "backend/mrl"];
  const transpiler = new Bun.Transpiler({ loader: "ts", trimUnusedImports: false });
  const fixtureRoots = ["tests/", "spec/examples/", "removed/"];
  const builtIn = /^(?:node:|bun:sqlite$)/;
  const fixtureNames = registryFixtureNames(root);
  const productionFiles: string[] = [];
  for (const path of sources) {
    for (const file of listFiles(join(root, path)).filter((file) => file.endsWith(".ts") && !file.endsWith(".test.ts"))) {
      productionFiles.push(file);
      const source = readFileSync(file, "utf8");
      problems.push(...registryLiteralProblems(relativePath(root, file), source, fixtureNames));
      for (const entry of transpiler.scanImports(source)) {
        if (entry.path.startsWith(".")) {
          const target = relativePath(root, resolve(file, "..", entry.path));
          if (fixtureRoots.some((prefix) => target.startsWith(prefix))) {
            problems.push(`${relativePath(root, file)}: 控制面禁止引入 ${entry.path}`);
          }
        } else if (!builtIn.test(entry.path) || entry.path === "node:child_process") {
          problems.push(`${relativePath(root, file)}: 控制面不允许依赖 ${entry.path}`);
        }
      }
      if (/\bshell\s*:\s*true\b|(?:^|[^\w.])(?:eval|Function)\s*\(/m.test(source)) {
        problems.push(`${relativePath(root, file)}: 控制面必须使用 argv 进程调用和 MRL 表达式求值`);
      }
    }
  }
  problems.push(...modulePolicyProblems(root, productionFiles));
  for (const path of ["backend", "tools", "tests"]) {
    for (const file of listFiles(join(root, path)).filter((file) => file.endsWith(".py"))) {
      const location = relativePath(root, file);
      if (location === "tools/harness.py") {
        const lines = readFileSync(file, "utf8").split(/\r?\n/).filter((line) => line.trim() && !line.startsWith('"""'));
        const launcher = [
          "import subprocess", "import sys",
          'raise SystemExit(subprocess.run(["bun", "run", "tools/harness.ts", *sys.argv[1:]], check=False).returncode)',
        ];
        if (JSON.stringify(lines) === JSON.stringify(launcher)) continue;
      }
      problems.push(`${location}: 活动源码及测试入口使用 Bun TypeScript`);
    }
  }
  return problems;
}

function xmlText(value: string): string {
  return value.replace(/&(amp|lt|gt|quot|apos|#\d+|#x[0-9a-f]+);/gi, (_entity, key: string) => {
    if (key.startsWith("#x")) return String.fromCodePoint(Number.parseInt(key.slice(2), 16));
    if (key.startsWith("#")) return String.fromCodePoint(Number(key.slice(1)));
    return ({ amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" })[key] ?? _entity;
  });
}

export function parseAcceptanceReport(root: string, source: string): AcceptanceCase[] {
  const cases: AcceptanceCase[] = [];
  for (const testcase of source.matchAll(/<testcase\b([^>]*?)(?:\/\s*>|>([\s\S]*?)<\/testcase>)/g)) {
    const fields = Object.fromEntries([...testcase[1].matchAll(/\b(name|file)="([^"]*)"/g)]
      .map((match) => [match[1], xmlText(match[2])]));
    if (!fields.file || !fields.name) throw new Error("Bun JUnit 验收结果缺少 file 或 name");
    cases.push({
      file: relativePath(root, fields.file),
      name: fields.name,
      passed: !/<(?:error|failure|skipped)\b/.test(testcase[2] ?? ""),
    });
  }
  if (cases.length === 0) throw new Error("Bun JUnit 未记录任何验收用例");
  return cases;
}

export function acceptanceProblems(spec: ReturnType<typeof inspectSpecifications>, cases: AcceptanceCase[]): string[] {
  const failures = cases.filter((item) => !item.passed).map(({ file, name }) => `${file}::${name} 失败或跳过`);
  const results = new Map<string, Map<string, number>>();
  for (const { file, name, passed } of cases) {
    let fileResults = results.get(file);
    if (!fileResults) {
      fileResults = new Map();
      results.set(file, fileResults);
    }
    if (passed) fileResults.set(name, (fileResults.get(name) ?? 0) + 1);
  }
  for (const file of spec.mappedFiles) {
    if (!results.has(file)) failures.push(`${file} 没有执行任何验收用例`);
  }
  for (const file of results.keys()) {
    if (!spec.mappedFiles.has(file)) failures.push(`${file} 缺少活动验收映射`);
  }
  for (const [obligation, evidence] of spec.evidence) {
    for (const { file, name } of evidence) {
      if (results.get(file)?.get(name) !== 1) failures.push(`${obligation} 的 Bun 证据必须恰好通过一次 ${file}::${name}`);
    }
  }
  return failures;
}
