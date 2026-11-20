import { isDeepStrictEqual } from "node:util";
import { dirname, join, relative, resolve } from "node:path";
import { MrlStaticError } from "../backend/mrl/checker";
import { compileExpr, type EvalEnvironment, type MrlValue } from "../backend/mrl/evaluator";
import { mrlJsonReplacer } from "../backend/mrl/numbers";
import { compileTemplate } from "../backend/mrl/template";
import { buildRuleProgram, type RuleLocation } from "../backend/mrl/rules";
import { DYN, parseType, typeName, type TypeEnvironment } from "../backend/mrl/types";
import { checkRegistry } from "../backend/mrl/factory/check";
import { listFiles } from "../backend/mrl/factory/paths";
import { readTree, registryFromTexts, type HostDiagnostic } from "../backend/mrl/factory/registry";
import { loadDocuments, loadFile } from "../backend/mrl/factory/yamlio";

type Row = MrlValue[];
type Kind = "expr" | "template" | "rules" | "factory";
interface ConformanceCase {
  id: string;
  expect: {
    static_error?: string;
    error?: string;
    type?: string;
    warnings?: string[];
    value?: MrlValue;
    relations?: Record<string, Row[]>;
    contains?: Record<string, Row[]>;
    excludes?: Record<string, Row[]>;
    clean?: boolean;
    ok?: boolean;
    codes?: string[];
    diags?: Partial<HostDiagnostic>[];
    absent?: Partial<HostDiagnostic>[];
    resolutions?: unknown[];
  };
  expr?: string;
  template?: string;
  program?: string;
  modules?: { name: string; text: string }[];
  env?: EvalEnvironment;
  types?: TypeEnvironment;
  budget?: number;
  facts?: Record<string, Row[]>;
  base?: string;
  patch?: Patch[];
}
type Patch = Patch[] | {
  file: string;
  content?: string;
  delete?: boolean;
  append?: unknown;
  doc?: number;
  remove_doc?: boolean;
  set?: Record<string, unknown>;
  unset?: string[];
};

const expectationKeys: Record<Kind, string[]> = {
  expr: ["static_error", "error", "type", "warnings", "value"],
  template: ["static_error", "error", "warnings", "value"],
  rules: ["static_error", "error", "warnings", "relations", "contains", "excludes"],
  factory: ["clean", "ok", "codes", "diags", "absent", "resolutions"],
};

function equal(label: string, actual: unknown, expected: unknown) {
  if (!isDeepStrictEqual(actual, expected)) {
    throw new Error(`${label}: 期望 ${JSON.stringify(expected, mrlJsonReplacer)}，实际 ${JSON.stringify(actual, mrlJsonReplacer)}`);
  }
}

function errorCode(error: unknown): unknown {
  return error && typeof error === "object" && "code" in error ? error.code : undefined;
}

function compile<T>(run: () => T, expected?: string): T | undefined {
  try {
    const value = run();
    if (expected) throw new Error(`期望静态错误 ${expected}，编译成功`);
    return value;
  } catch (error) {
    const codes = error instanceof MrlStaticError
      ? error.diagnostics.filter((item) => item.severity === "error").map((item) => item.code)
      : [errorCode(error)];
    if (expected && codes.includes(expected)) return undefined;
    throw error;
  }
}

function execute<T>(run: () => T, expected?: string): T | undefined {
  try {
    const result = run();
    if (expected) throw new Error(`期望运行时错误 ${expected}，求值成功`);
    return result;
  } catch (error) {
    if (expected && errorCode(error) === expected) return undefined;
    throw error;
  }
}

function expression(kind: "expr" | "template", test: ConformanceCase) {
  const { expect, env = {}, types = {}, budget } = test;
  const environment = { ...Object.fromEntries(Object.keys(env).map((key) => [key, DYN])), ...types };
  const source = kind === "expr" ? test.expr : test.template;
  if (typeof source !== "string") throw new Error(`缺少 ${kind} 源码`);
  const compiled = compile(
    () => kind === "expr" ? compileExpr(source, { environment, budget }) : compileTemplate(source, { environment }),
    expect.static_error,
  );
  if (!compiled) return;
  if (expect.warnings) equal("警告", compiled.warnings.map((item) => item.code).sort(), [...expect.warnings].sort());
  if ("type" in compiled && expect.type) equal("静态类型", typeName(compiled.type), typeName(parseType(expect.type)));
  if (!("value" in expect) && !expect.error) return;
  const value = execute(
    () => "evaluate" in compiled ? compiled.evaluate(env) : compiled.render(env, { budget }),
    expect.error,
  );
  if ("value" in expect) equal("求值结果", value, expect.value);
}

function rules(test: ConformanceCase) {
  const { expect } = test;
  const sources = test.modules
    ? test.modules.map(({ name, text }) => ({ name, source: text }))
    : [{ name: test.id, source: test.program }];
  if (sources.some((item) => typeof item.source !== "string")) throw new Error("缺少规则源码");
  const program = compile(() => buildRuleProgram(sources as { name: string; source: string }[]), expect.static_error);
  if (!program) return;
  if (expect.warnings) {
    equal("警告", program.warnings.map((item) => item.code).sort(), [...expect.warnings].sort());
  }
  const model = execute(() => program.evaluate(test.facts), expect.error);
  if (!model) return;
  for (const [name, rows] of Object.entries(expect.relations ?? {})) {
    const remaining = model.rowsOf(name);
    equal(`${name} 行数`, remaining.length, rows.length);
    for (const row of rows) {
      const index = remaining.findIndex((item) => isDeepStrictEqual(item, Array.isArray(row) ? row : [row]));
      if (index < 0) throw new Error(`关系 ${name} 缺少 ${JSON.stringify(row, mrlJsonReplacer)}`);
      remaining.splice(index, 1);
    }
  }
  for (const [name, rows] of Object.entries(expect.contains ?? {})) {
    const actual = model.rowsOf(name);
    for (const row of rows) {
      if (!actual.some((item) => isDeepStrictEqual(item, Array.isArray(row) ? row : [row]))) {
        throw new Error(`关系 ${name} 缺少 ${JSON.stringify(row, mrlJsonReplacer)}`);
      }
    }
  }
  for (const [name, rows] of Object.entries(expect.excludes ?? {})) {
    const actual = model.rowsOf(name);
    for (const row of rows) {
      if (actual.some((item) => isDeepStrictEqual(item, Array.isArray(row) ? row : [row]))) {
        throw new Error(`关系 ${name} 不应包含 ${JSON.stringify(row, mrlJsonReplacer)}`);
      }
    }
  }
}

function mapping(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function editDocument(data: unknown, path: string, operation: "set" | "unset", value?: unknown) {
  const parts = [...path.matchAll(/(?:^|\.)([^.\[\]]+)|\[(\d+|"(?:[^"\\]|\\.)+")\]/g)];
  if (!parts.length || parts.map((item) => item[0]).join("") !== path) throw new Error(`无效的补丁路径 ${path}`);
  const keys: (string | number)[] = parts.map((item) => {
    if (item[1]) return item[1];
    return item[2][0] === '"' ? JSON.parse(item[2]) as string : Number(item[2]);
  });
  let current = data;
  for (const [index, key] of keys.entries()) {
    if (key === "__proto__" || key === "constructor" || key === "prototype") throw new Error(`无效的补丁路径 ${path}`);
    if ((typeof key === "number" && !Array.isArray(current)) || (typeof key === "string" && !mapping(current))) {
      throw new Error(`补丁路径 ${path} 的字段类型错误`);
    }
    const container = current as Record<string | number, unknown>;
    if (index === keys.length - 1) {
      if (operation === "set") container[key] = value;
      else if (!Object.hasOwn(container, key)) throw new Error(`补丁路径 ${path} 不存在`);
      else if (Array.isArray(current)) current.splice(key as number, 1);
      else delete container[key];
      return;
    }
    if (operation === "set" && (!Object.hasOwn(container, key) || container[key] === null)) {
      if (typeof key === "number" && !Object.hasOwn(container, key)) throw new Error(`补丁路径 ${path} 不存在`);
      container[key] = typeof keys[index + 1] === "number" ? [] : {};
    }
    current = Object.hasOwn(container, key) ? container[key] : undefined;
  }
}

function applyPatch(files: Record<string, string>, patch: Patch) {
  if (Array.isArray(patch)) {
    for (const entry of patch) applyPatch(files, entry);
    return;
  }
  const { file } = patch;
  if (patch.content !== undefined) {
    files[file] = String(patch.content);
    return;
  }
  if (patch.delete) {
    if (!Object.hasOwn(files, file)) throw new Error(`补丁要删除的文件不存在 ${file}`);
    delete files[file];
    return;
  }
  const docs = Object.hasOwn(files, file) ? loadDocuments(files[file]).map((item) => item.data) : [];
  if ("append" in patch) {
    docs.push(patch.append);
  } else {
    const index = patch.doc ?? 0;
    if (!Object.hasOwn(docs, index)) throw new Error(`文件 ${file} 没有第 ${index} 个文档`);
    if (patch.remove_doc) docs.splice(index, 1);
    for (const [path, value] of Object.entries(patch.set ?? {})) editDocument(docs[index], path, "set", value);
    for (const path of patch.unset ?? []) editDocument(docs[index], path, "unset");
  }
  files[file] = docs.map((doc) => JSON.stringify(doc, mrlJsonReplacer)).join("\n---\n");
}

function factory(root: string, file: string, test: ConformanceCase, baseCache: Map<string, Record<string, string>>) {
  const fired: RuleLocation[] = [];
  const { expect, base } = test;
  const directory = base === "empty" ? undefined
    : !base || base === "example" ? join(root, "spec/examples/registry") : resolve(dirname(file), base);
  if (directory && !baseCache.has(directory)) baseCache.set(directory, readTree(directory));
  const files = Object.assign(Object.create(null) as Record<string, string>, directory ? baseCache.get(directory) : {});
  for (const patch of test.patch ?? []) applyPatch(files, patch);
  const { diagnostics, ok, resolutions } = checkRegistry(registryFromTexts(`<${test.id}>`, files), (location) => fired.push(location));
  const codes = diagnostics.filter((item) => item.severity === "error" || item.severity === "warning").map((item) => item.code).sort();
  if (expect.clean) equal("诊断码", codes, []);
  if (expect.ok) equal("检查结果", ok, true);
  if (expect.codes) equal("诊断码", codes, [...expect.codes].sort());
  const matches = (actual: HostDiagnostic, wanted: Partial<HostDiagnostic>) =>
    Object.entries(wanted).every(([key, value]) => key === "message"
      ? actual.message.includes(String(value)) : actual[key as keyof HostDiagnostic] === value);
  for (const wanted of expect.diags ?? []) {
    if (!diagnostics.some((item) => matches(item, wanted))) throw new Error(`缺少诊断 ${JSON.stringify(wanted)}`);
  }
  for (const unwanted of expect.absent ?? []) {
    if (diagnostics.some((item) => matches(item, unwanted))) throw new Error(`不应出现诊断 ${JSON.stringify(unwanted)}`);
  }
  for (const wanted of expect.resolutions ?? []) {
    if (!mapping(wanted) || !resolutions.some((actual) => Object.entries(wanted).every(([key, value]) =>
      isDeepStrictEqual(actual[key], value) || (key === "line" && actual.line.replace(/^Line:/, "") === value)))) {
      throw new Error("缺少解析结果 " + JSON.stringify(wanted));
    }
  }
  return fired;
}

export function runMrlConformance(root: string, kernelCodes: ReadonlySet<string>) {
  const counts = { expr: 0, template: 0, rules: 0, factory: 0 };
  const passed = { ...counts };
  const failures: Array<{ name: string; kind?: Kind; reason: string }> = [];
  const suiteIds = new Set<string>();
  const coveredCodes = new Set<string>();
  const coveredRules = new Map<string, Set<string>>();
  const baseCache = new Map<string, Record<string, string>>();
  const suites = listFiles(join(root, "spec/mrl/conformance")).filter((file) => /\.ya?ml$/.test(file));
  for (const file of suites) {
    const path = relative(root, file).replaceAll("\\", "/");
    let suite: { suite: string; kind: Kind; base?: string; cases: ConformanceCase[] };
    try {
      const document = loadFile(file, true);
      if (!mapping(document) || typeof document.suite !== "string" || !document.suite || !Object.hasOwn(expectationKeys, String(document.kind))
        || !Array.isArray(document.cases) || document.cases.length === 0) throw new Error("需要 suite、kind 和非空 cases");
      suite = document as unknown as typeof suite;
      if (suiteIds.has(suite.suite)) throw new Error(`套件 ID 重复 ${suite.suite}`);
      suiteIds.add(suite.suite);
    } catch (error) {
      failures.push({ name: path, reason: String(error) });
      continue;
    }
    const caseIds = new Set<string>();
    for (const test of suite.cases) {
      counts[suite.kind] += 1;
      const name = `${suite.suite}/${test?.id ?? "?"}`;
      try {
        if (!test || typeof test.id !== "string" || !test.id || !mapping(test.expect)) throw new Error("需要 id 和 expect");
        if (caseIds.has(test.id)) throw new Error(`用例 ID 重复 ${test.id}`);
        caseIds.add(test.id);
        const keys = Object.keys(test.expect);
        if (keys.length === 0 || keys.some((key) => !expectationKeys[suite.kind].includes(key))) throw new Error(`无效的 expect 字段 ${keys.join(", ")}`);
        if (suite.kind === "factory") {
          for (const rule of factory(root, file, { base: suite.base, ...test }, baseCache)) {
            const location = rule.file + ":" + rule.line;
            const witnesses = coveredRules.get(location) ?? new Set();
            witnesses.add(name);
            coveredRules.set(location, witnesses);
          }
          for (const code of test.expect.codes ?? []) coveredCodes.add(code);
          for (const diagnostic of test.expect.diags ?? []) if (diagnostic.code) coveredCodes.add(diagnostic.code);
        } else if (suite.kind === "rules") rules(test);
        else expression(suite.kind, test);
        passed[suite.kind] += 1;
      } catch (error) {
        failures.push({ name, kind: suite.kind, reason: String(error) });
      }
    }
  }
  if (suites.length === 0) failures.push({ name: "spec/mrl/conformance", reason: "找不到一致性套件" });
  return {
    suites: suites.length,
    counts,
    passed,
    failures,
    unverifiedCodes: [...kernelCodes].filter((code) => !coveredCodes.has(code)).sort(),
    coveredRules: Object.fromEntries([...coveredRules].sort(([a], [b]) => a.localeCompare(b))
      .map(([location, witnesses]) => [location, [...witnesses].sort()])),
  };
}
