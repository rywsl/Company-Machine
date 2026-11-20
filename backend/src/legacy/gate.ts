import { lstatSync, readFileSync, readdirSync } from "node:fs";
import { rm } from "node:fs/promises";
import { join, resolve } from "node:path";
import { compileExpr, type EvalEnvironment } from "../../mrl/evaluator";
import { validateInlineSchema } from "../../mrl/factory/schema";
import { BOOL, DYN, parseType as parseMrlType, type MrlType } from "../../mrl/types";
import type { ContainerProvider } from "../../runtime/podman";
import { FactoryError } from "./errors";
import { canonicalBytes, canonicalJson, digestBytes, randomUuid } from "./foundation";
import { nominalTypeId, type Snapshot } from "./snapshot";
import { acceptEnv, paramsValue, propertyEnv, recipeEnv } from "./values";
import { mediaTypeOf, type Bean, type Warehouse } from "./warehouse";

type JsonObject = Record<string, unknown>;
type Materials = Record<string, Bean | Bean[]>;
type GateStage = "structure" | "validator" | "property" | "accept" | "consistency";
type Port = { type: string; optional?: boolean; requires?: string[]; guarantees?: string[]; accept?: string | string[] };
type PayloadRule = { path: string; required?: boolean; media_type?: string };
type Validator = { name: string; image: string; run: string[]; emits: string[] };
type Candidate = { files: Record<string, string>; meta: JsonObject };
type TypeContract = { properties: JsonObject; always: string[]; facts: Record<string, string>; metaSchema?: JsonObject };

export interface GateCheck { port: string; index: number; stage: GateStage; name: string; ok: boolean; detail: string; }
export interface GateReportData {
  job_id: string;
  station: string;
  attempt: number;
  verdict: "pass" | "reject" | "error";
  outputs: Record<string, string[]>;
  facts: Record<string, JsonObject>;
  checks: GateCheck[];
  feedback: string;
}
export class GateReport {
  constructor(readonly data: GateReportData) {}
  toJSON(): GateReportData { return structuredClone(this.data); }
  digest(): string { return digestBytes(canonicalBytes(this.data)); }
}

const STAGES: GateStage[] = ["structure", "validator", "property", "accept", "consistency"];
const INFRA = "设施故障：";
const compile = (source: string, values: JsonObject) => compileExpr(source, {
  environment: Object.fromEntries(Object.keys(values).map((key) => [key, DYN])), expect: BOOL,
});

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function parseType(value: string, output: boolean): { list: boolean; type: string } {
  const match = /^list<([^<>]+)>$/.exec(value);
  const inner = match?.[1] ?? value;
  if ((inner === "any" && output) || (inner !== "any" && !/^[\w.-]+@\d+(?:\.\d+)?$/.test(inner))) {
    throw new FactoryError("GATE_UNSUPPORTED", "M1 不检验这种类型表达式", { type: value });
  }
  return { list: Boolean(match), type: nominalTypeId(inner) };
}

function directory(path: string) {
  try {
    const entry = lstatSync(path);
    if (entry.isDirectory() && !entry.isSymbolicLink()) return true;
    throw new FactoryError("PAYLOAD_INVALID", "候选路径必须是普通目录");
  } catch (error) {
    if (isObject(error) && error.code === "ENOENT") return false;
    throw error;
  }
}

function readCandidate(root: string): Candidate {
  const files: Array<[string, string]> = [];
  const directories = [""];
  let meta: JsonObject = {};
  for (const parent of directories) {
    for (const name of readdirSync(join(root, parent)).sort() as string[]) {
      const path = parent ? parent + "/" + name : name;
      const full = join(root, path);
      const entry = lstatSync(full);
      if (entry.isSymbolicLink() || (!entry.isFile() && !entry.isDirectory())) {
        throw new FactoryError("PAYLOAD_INVALID", "候选目录含链接或特殊文件", { path });
      }
      if (path === "meta.json") {
        if (!entry.isFile()) throw new FactoryError("PAYLOAD_INVALID", "meta.json 必须是普通文件");
        try {
          const loaded: unknown = JSON.parse(readFileSync(full, "utf8"));
          if (!isObject(loaded)) throw new Error("not an object");
          meta = loaded;
        } catch {
          throw new FactoryError("PAYLOAD_INVALID", "meta.json 必须是 JSON 对象");
        }
      } else if (entry.isDirectory()) directories.push(path);
      else files.push([path, full]);
    }
  }
  return { files: Object.fromEntries(files), meta };
}

function pathMatches(path: string, pattern: string): boolean {
  const parts = path.split("/");
  const rules = pattern.split("/");
  return parts.length === rules.length && parts.every((part, index) => {
    const rule = rules[index];
    if (!/[*?]/.test(rule)) return part === rule;
    const regex = Array.from(rule, (char) => {
      if (char === "*") return "[\\s\\S]*";
      if (char === "?") return "[\\s\\S]";
      return /[\\^$.*+?()[\]{}|]/.test(char) ? "\\" + char : char;
    }).join("");
    return new RegExp("^(?:" + regex + ")$", "u").test(part);
  });
}

function validatePayload(candidate: Candidate, rules: PayloadRule[]): void {
  const checked = new Set<PayloadRule>();
  for (const [path, source] of Object.entries(candidate.files).sort(([a], [b]) => a.localeCompare(b))) {
    if (path === "bean.json" || path === "meta.json") {
      throw new FactoryError("PAYLOAD_INVALID", "使用了保留的文件名", { path });
    }
    if (!lstatSync(source).isFile()) throw new FactoryError("PAYLOAD_INVALID", "物料必须是普通文件", { path });
    const matches = rules.filter((rule) => pathMatches(path, rule.path));
    if (matches.length !== 1) throw new FactoryError("PAYLOAD_INVALID", "文件必须恰好匹配一条规则", { path });
    const rule = matches[0];
    if (rule.media_type && !pathMatches(mediaTypeOf(path), rule.media_type)) {
      throw new FactoryError("PAYLOAD_INVALID", "媒体类型不匹配", { path });
    }
    checked.add(rule);
  }
  for (const rule of rules) {
    if (rule.required !== false && !checked.has(rule)) throw new FactoryError("PAYLOAD_INVALID", "缺少必填文件", { path: rule.path });
  }
}

function factMatches(value: unknown, type: MrlType): boolean {
  if (type.kind === "list") return Array.isArray(value) && value.every((item) => factMatches(item, type.element));
  if (type.kind === "map") return isObject(value) && Object.values(value).every((item) => factMatches(item, type.element));
  if (type.kind !== "primitive") return false;
  switch (type.name) {
    case "bool": return typeof value === "boolean";
    case "int": return typeof value === "number" && Number.isSafeInteger(value);
    case "float":
    case "num": return typeof value === "number" && Number.isFinite(value);
    case "string": return typeof value === "string";
    default: return false;
  }
}

function errorDetail(error: unknown): string {
  if (!(error instanceof Error)) return String(error);
  const path = error instanceof FactoryError ? error.detail.path : undefined;
  return typeof path === "string" ? error.message + " " + path : error.message;
}

function report(
  jobId: string, station: string, attempt: number, checks: GateCheck[],
  outputs: Record<string, string[]> = {}, facts: Record<string, JsonObject> = {},
): GateReport {
  const ordered = checks.sort((left, right) => left.port.localeCompare(right.port)
    || STAGES.indexOf(left.stage) - STAGES.indexOf(right.stage)
    || left.index - right.index || left.name.localeCompare(right.name));
  const failed = ordered.filter((check) => !check.ok);
  const verdict = failed.some((check) => check.detail.startsWith(INFRA)) ? "error" : failed.length ? "reject" : "pass";
  return new GateReport({
    job_id: jobId, station, attempt, verdict, outputs, facts, checks: ordered,
    feedback: failed.map((check) => [check.port, check.stage, check.name, check.detail].join(" ").trim()).join("\n"),
  });
}

export class Gate {
  private readonly predicates = new Map<string, ReturnType<typeof compile>>();
  private readonly contracts = new Map<string, TypeContract>();
  constructor(
    readonly snapshot: Snapshot, readonly warehouse: Warehouse, readonly workdir: string,
    readonly provider?: ContainerProvider,
    private readonly timeoutMs = 60_000,
  ) {}

  checkInputs(stationRef: string, inputs: Record<string, string | string[]>, params: JsonObject = {}): Materials {
    const station = this.snapshot.key("Station", stationRef);
    const spec = this.snapshot.spec(station);
    const declared: Record<string, Port> = spec.inputs ?? {};
    const result: Materials = Object.create(null);
    const problems: Array<{ port: string; index: number; reason: string; detail: string; bean_id: string }> = [];
    const problem = (port: string, index: number, reason: string, detail: string, bean_id = "") =>
      problems.push({ port, index, reason, detail, bean_id });
    for (const port of Object.keys(declared).sort()) {
      const declaration = declared[port];
      if (!Object.hasOwn(inputs, port)) {
        if (!declaration.optional) problem(port, 0, "missing", "缺少必填端口");
        continue;
      }
      const { list, type } = parseType(declaration.type, false);
      const input = inputs[port];
      if (list !== Array.isArray(input)) {
        problem(port, 0, "cardinality", list ? "列表端口需要 ID 列表" : "标量端口需要单个 ID");
        continue;
      }
      const ids = Array.isArray(input) ? input : [input];
      const beans = ids.flatMap((id, index) => {
        let bean: Bean;
        try { bean = this.warehouse.get(id); }
        catch (error) {
          if (!(error instanceof FactoryError) || error.code !== "BEAN_NOT_FOUND") throw error;
          problem(port, index, "not_found", "Bean 不存在", id);
          return [];
        }
        if (!bean.sealed) {
          problem(port, index, "unsealed", "Bean 未封存", id);
          return [];
        }
        const beanType = parseType(bean.type, false).type;
        try { this.snapshot.typeKey(beanType); }
        catch (error) {
          if (!(error instanceof FactoryError) || error.code !== "TYPE_NOT_FOUND") throw error;
          problem(port, index, "type", "类型不在快照里", id);
          return [];
        }
        if (type !== "any" && !this.snapshot.isSubtype(beanType, type)) {
          problem(port, index, "type", "类型不兼容", id);
          return [];
        }
        const failed = this.checkProperties(bean, beanType, declaration.requires ?? []).find((item) => !item.ok);
        if (failed) {
          problem(port, index, "requires", failed.name, id);
          return [];
        }
        return [bean];
      });
      if (beans.length === ids.length) result[port] = list ? beans : beans[0];
    }
    for (const port of Object.keys(inputs).sort()) {
      if (!Object.hasOwn(declared, port)) problem(port, 0, "extra", "工位没有这个输入端口");
    }
    if (!problems.length && spec.admit?.length) {
      const environment = recipeEnv(result, paramsValue(this.snapshot, station, params));
      for (const [index, expr] of (spec.admit as string[]).entries()) {
        const subject = `${station}/admit[${index}]`;
        if (!this.evaluate(expr, environment, subject)) problem("", index, "admit", "admit[" + index + "]");
      }
    }
    if (problems.length) throw new FactoryError("INTAKE_REJECTED", "来料检验未通过", { problems });
    return result;
  }

  async admitExternal(
    type: string, files: Record<string, string>, meta: JsonObject, jobId = "external", attempt = 0,
  ): Promise<{ report: GateReport; bean: Bean | null }> {
    type = nominalTypeId(type);
    const checks: GateCheck[] = [];
    const bean = await this.register("", 0, type, () => ({ files, meta }), checks);
    const observed = bean ? await this.validate("", 0, type, bean, [], checks) : null;
    if (bean && observed?.ok) this.checkConsistency("", 0, type, bean, observed.facts, checks);
    const result = report(
      jobId, "external", attempt, checks, bean ? { "": [bean.id] } : {}, bean && observed ? { [bean.id]: observed.facts } : {},
    );
    return { report: result, bean: result.data.verdict === "pass" ? bean : null };
  }

  async inspect(
    stationRef: string,
    options: { jobId: string; attempt: number; outDir: string; inputs?: Materials; params?: JsonObject; port?: string },
  ): Promise<GateReport> {
    const station = this.snapshot.key("Station", stationRef);
    const declarations: Record<string, Port> = this.snapshot.spec(station).outputs ?? {};
    const params = paramsValue(this.snapshot, station, options.params ?? {});
    const inputs = options.inputs ?? {};
    const checks: GateCheck[] = [];
    const outputs: Record<string, string[]> = Object.create(null);
    const facts: Record<string, JsonObject> = Object.create(null);
    let hasRoot: boolean;
    try { hasRoot = directory(options.outDir); }
    catch (error) {
      checks.push({ port: "", index: 0, stage: "structure", name: "payload", ok: false, detail: errorDetail(error) });
      return report(options.jobId, station, options.attempt, checks);
    }

    const ports = options.port === undefined ? Object.keys(declarations).sort() : [options.port];
    for (const port of ports) {
      const declaration = declarations[port];
      const { list, type } = parseType(declaration.type, true);
      const portDir = join(options.outDir, port);
      let candidates: string[];
      try {
        if (!hasRoot || !directory(portDir)) {
          if (!declaration.optional) throw new FactoryError("PAYLOAD_INVALID", "缺少候选目录");
          continue;
        }
        candidates = list ? this.indexedCandidates(portDir) : [portDir];
        if (!declaration.optional && !candidates.length) throw new FactoryError("PAYLOAD_INVALID", "缺少必填产物");
      } catch (error) {
        checks.push({ port, index: 0, stage: "structure", name: "payload", ok: false, detail: errorDetail(error) });
        continue;
      }

      const ready: Array<{ index: number; bean: Bean }> = [];
      for (const [index, root] of candidates.entries()) {
        const bean = await this.register(port, index, type, () => readCandidate(root), checks);
        if (!bean) continue;
        (outputs[port] ??= []).push(bean.id);
        const observed = await this.validate(port, index, type, bean, declaration.guarantees ?? [], checks);
        facts[bean.id] = observed.facts;
        if (observed.ok) ready.push({ index, bean: { ...bean, facts: observed.facts } });
      }
      if (ready.length && ready.length === candidates.length) {
        const environment = acceptEnv(list ? ready.map(({ bean }) => bean) : ready[0].bean, inputs, params);
        const accepts = typeof declaration.accept === "string" ? [declaration.accept] : declaration.accept ?? [];
        for (const [index, expression] of accepts.entries()) {
          const name = "accept[" + index + "]";
          const ok = this.evaluate(expression, environment, `${station}/outputs.${port}.${name}`);
          checks.push({ port, index: -1, stage: "accept", name, ok, detail: ok ? "" : name });
        }
      }
      for (const { index, bean } of ready) this.checkConsistency(port, index, type, bean, bean.facts, checks);
    }
    return report(options.jobId, station, options.attempt, checks, outputs, facts);
  }

  private indexedCandidates(root: string): string[] {
    const names = readdirSync(root) as string[];
    const available = new Set(names);
    return names.map((_, index) => {
      const name = String(index);
      const path = join(root, name);
      if (!available.has(name) || !directory(path)) throw new FactoryError("PAYLOAD_INVALID", "列表候选下标必须从 0 连续排列");
      return path;
    });
  }

  private async register(
    port: string, index: number, type: string, candidate: () => Candidate, checks: GateCheck[],
  ): Promise<Bean | null> {
    const key = this.snapshot.typeKey(type);
    const document = this.snapshot.doc(key);
    let data: Candidate;
    try {
      data = candidate();
      validatePayload(data, document.spec.payload ?? []);
    } catch (error) {
      checks.push({ port, index, stage: "structure", name: "payload", ok: false, detail: errorDetail(error) });
      return null;
    }
    try {
      const bean = await this.warehouse.add({
        type: document.metadata.name + "@" + document.metadata.version, ...data,
      });
      checks.push({ port, index, stage: "structure", name: "payload", ok: true, detail: "" });
      return bean;
    } catch (error) {
      const invalid = error instanceof FactoryError && ["PAYLOAD_PATH_INVALID", "JSON_INVALID"].includes(error.code);
      checks.push({ port, index, stage: "structure", name: "payload", ok: false, detail: (invalid ? "" : INFRA) + errorDetail(error) });
      return null;
    }
  }

  private checkProperties(bean: Bean, type: string, names: string[]) {
    if (!names.length) return [];
    const { properties } = this.typeContract(type);
    const environment = propertyEnv(bean);
    return names.map((name) => {
      const expr = properties[name];
      return { name, ok: typeof expr === "string" && this.evaluate(expr, environment, this.snapshot.propertySubject(type, name)) };
    });
  }

  private typeContract(type: string): TypeContract {
    const cached = this.contracts.get(type);
    if (cached) return cached;
    const properties: Array<[string, unknown]> = [];
    const facts: Array<[string, string]> = [];
    const always = new Set<string>();
    const visited = new Set<string>();
    let metaSchema: JsonObject | undefined;
    let parent: string | undefined = type;
    while (parent) {
      const key = this.snapshot.typeKey(parent);
      if (visited.has(key)) throw new FactoryError("TYPE_CYCLE", "Type 继承存在循环", { type });
      visited.add(key);
      const spec = this.snapshot.spec(key);
      properties.unshift(...Object.entries(spec.properties ?? {}));
      facts.unshift(...Object.entries(spec.facts ?? {}) as Array<[string, string]>);
      for (const name of spec.always ?? []) always.add(name);
      metaSchema ??= spec.meta_schema;
      parent = spec.extends;
    }
    const contract = { properties: Object.fromEntries(properties), facts: Object.fromEntries(facts), always: [...always], metaSchema };
    this.contracts.set(type, contract);
    return contract;
  }

  private evaluate(source: string, values: JsonObject, subject?: string): boolean {
    try {
      const checked = subject === undefined ? undefined : this.snapshot.result.compiledSites.get(subject);
      if (checked?.kind === "expression" && checked.source === source) {
        return checked.evaluate(values as EvalEnvironment) === true;
      }
      const key = Object.keys(values).sort().join(",") + ":" + source;
      let predicate = this.predicates.get(key);
      if (!predicate) {
        predicate = compile(source, values);
        this.predicates.set(key, predicate);
      }
      return predicate.evaluate(values as EvalEnvironment) === true;
    } catch {
      return false;
    }
  }

  private async validate(
    port: string, index: number, type: string, bean: Bean, guarantees: string[], checks: GateCheck[],
  ): Promise<{ ok: boolean; facts: JsonObject }> {
    const facts: JsonObject = Object.create(null);
    const validators: Validator[] = this.snapshot.validators(type);
    const contract = this.typeContract(type);
    if (validators.length) {
      const workspace = resolve(this.workdir, "mao-gate-" + randomUuid());
      try {
        if (!this.provider) throw new FactoryError("GATE_RUNTIME_UNAVAILABLE", "缺少 validator 沙箱运行时");
        await this.warehouse.materialize(bean.id, join(workspace, "bean"), "bean");
        if (Object.keys(bean.facts).length) {
          await Bun.write(join(workspace, "bean", "bean.json"), canonicalBytes(propertyEnv({ ...bean, facts: {} }).bean));
        }
        const declared = Object.fromEntries(Object.entries(contract.facts).map(([key, type]) => [key, parseMrlType(type)]));
        for (const [ordinal, validator] of validators.entries()) {
          const result = await this.runValidator(this.provider, workspace, ordinal, validator, declared);
          if (!result.ok) {
            checks.push({ port, index, stage: "validator", name: validator.name, ok: false, detail: result.detail });
            return { ok: false, facts: {} };
          }
          const conflicts = Object.keys(result.facts).some((key) =>
            Object.hasOwn(facts, key) && canonicalJson(facts[key]) !== canonicalJson(result.facts[key]));
          checks.push({
            port, index, stage: "validator", name: validator.name, ok: !conflicts,
            detail: conflicts ? INFRA + "emits 冲突" : "",
          });
          if (conflicts) return { ok: false, facts: {} };
          Object.assign(facts, result.facts);
        }
      } catch (error) {
        checks.push({ port, index, stage: "validator", name: "runtime", ok: false, detail: INFRA + errorDetail(error) });
        return { ok: false, facts: {} };
      } finally {
        await rm(join(workspace, "bean"), { recursive: true, force: true });
      }
    }

    const names = [...new Set([...contract.always, ...guarantees])].sort();
    for (const { name, ok } of this.checkProperties({ ...bean, facts }, type, names)) {
      checks.push({ port, index, stage: "property", name, ok, detail: ok ? "" : name });
      if (!ok) return { ok: false, facts };
    }
    return { ok: true, facts };
  }

  private async runValidator(
    provider: ContainerProvider, workspace: string, ordinal: number, validator: Validator, declared: Record<string, MrlType>,
  ):
  Promise<{ ok: true; facts: JsonObject } | { ok: false; detail: string }> {
    const name = "mao-gate-" + randomUuid();
    const stdout = join(workspace, String(ordinal) + ".out");
    const sandbox = await provider.open({
      name, image: validator.image, network: "none", workdir: "/",
      mounts: [{ source: join(workspace, "bean"), target: "/bean", readonly: true }],
    });
    let result: Awaited<ReturnType<typeof sandbox.exec>>;
    try {
      result = await sandbox.exec(validator.run, { timeoutMs: Math.min(60_000, this.timeoutMs), stdout, stderr: join(workspace, String(ordinal) + ".err") });
    } finally {
      await sandbox.close();
    }
    if (result.timedOut || result.exitCode === null) return { ok: false, detail: INFRA + "超时" };
    if (result.exitCode !== 0 && result.exitCode !== 1) return { ok: false, detail: INFRA + "退出码 " + result.exitCode };

    const text = (await Bun.file(stdout).text()).trim();
    let value: unknown;
    try { value = JSON.parse(text); } catch { value = undefined; }
    if (result.exitCode === 1) {
      const reason = isObject(value) && typeof value.reason === "string" ? value.reason : text;
      return { ok: false, detail: "拒绝：" + reason.slice(0, 500) };
    }
    const expected = new Set(validator.emits);
    if (!isObject(value) || Object.keys(value).length !== expected.size || !Object.keys(value).every((key) => expected.has(key))) {
      return { ok: false, detail: INFRA + "facts 形状不对" };
    }
    if (!Object.entries(value).every(([key, fact]) => Object.hasOwn(declared, key) && factMatches(fact, declared[key]))) {
      return { ok: false, detail: INFRA + "facts 类型不对" };
    }
    return { ok: true, facts: value };
  }

  private checkConsistency(port: string, index: number, type: string, bean: Bean, facts: JsonObject, checks: GateCheck[]): void {
    let detail: string | undefined;
    try {
      const { metaSchema } = this.typeContract(type);
      const error = metaSchema ? validateInlineSchema(bean.meta, metaSchema)[0] : undefined;
      detail = error ? [error.path, error.message].filter(Boolean).join(" ") : Object.keys(bean.meta).sort().find((key) =>
        Object.hasOwn(facts, key) && canonicalJson(bean.meta[key]) !== canonicalJson(facts[key]));
    } catch (error) {
      detail = INFRA + errorDetail(error);
    }
    checks.push({ port, index, stage: "consistency", name: "meta_schema", ok: detail === undefined, detail: detail ?? "" });
  }
}
