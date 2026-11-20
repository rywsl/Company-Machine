import { randomUUID } from "node:crypto";
import { lstatSync, mkdirSync, realpathSync } from "node:fs";
import { copyFile, rm } from "node:fs/promises";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { compileExpr, type EvalEnvironment } from "../../mrl/evaluator";
import { BOOL, DYN } from "../../mrl/types";
import { FactoryError } from "./errors";
import { canonicalJson, digestFile, parseDuration, utcIso } from "./foundation";
import { Gate } from "./gate";
import type { Job } from "./jobs";
import type { Runner } from "./runner";
import { nominalTypeId, type Snapshot } from "./snapshot";
import { beanValue } from "./values";

export type ContractKind = "validator" | "golden" | "equivalence" | "diff" | "sink_dry_run" | "fixture_set";
export interface ContractResult {
  id: string;
  kind: ContractKind;
  subject: string;
  verdict: "pass" | "fail";
  detail: Record<string, unknown>;
  at: string;
}

type ResultRow = Omit<ContractResult, "detail"> & { detail: string };
type Timeout = { timeoutS?: number };
type Inputs = Record<string, string | string[]>;
type Fixtures = { accept?: string[]; reject?: string[] };
type Validator = { name: string; image: string; run: string[]; emits: string[]; fixtures?: Fixtures };
type Station = {
  role: string;
  phases?: string[];
  limits?: { timeout?: string };
  inputs?: Record<string, { type: string }>;
  equivalence?: string[];
  fixtures?: Fixtures;
};
type GoldenCase = { name: string; inputs?: Inputs; params?: Record<string, unknown> };
type Golden = { station: string; budget_usd?: number; cases: GoldenCase[] };
const secretAssignment = /\b(api[_-]?key|secret|token|password|authorization|cookie)(\s*[:=]\s*)(?:bearer\s+)?(?:"[^"]*"|\S+)/gi;

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function redactResult(value: unknown): unknown {
  if (typeof value === "string") return value.replace(secretAssignment, "$1$2[REDACTED]");
  if (Array.isArray(value)) return value.map(redactResult);
  if (object(value)) return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, redactResult(item)]));
  return value;
}

function timeoutMilliseconds(timeout: Timeout, station: Station | undefined, subject: string): number {
  const seconds = timeout.timeoutS ?? (station?.limits?.timeout === undefined ? undefined : parseDuration(station.limits.timeout));
  if (seconds === undefined || !Number.isFinite(seconds * 1000) || seconds <= 0) {
    throw new FactoryError("CONTRACT_TIMEOUT_REQUIRED", "契约测试需要有效的超时", { subject });
  }
  return Math.max(1, seconds * 1000);
}

function notFound(subject: string): never {
  throw new FactoryError("CONTRACT_NOT_FOUND", `找不到契约测试对象 ${subject}`, { subject });
}

function underRoot(root: string, path: string): boolean {
  const suffix = relative(root, path);
  return Boolean(suffix) && suffix !== ".." && !suffix.startsWith(`..${sep}`) && !isAbsolute(suffix);
}

function factMatches(type: string | undefined, value: unknown): boolean {
  if (type === "int") return typeof value === "number" && Number.isInteger(value);
  if (type === "float" || type === "num") return typeof value === "number" && Number.isFinite(value);
  return (type === "bool" && typeof value === "boolean") || (type === "string" && typeof value === "string");
}

function firstOutput(job: Job | undefined): string | undefined {
  if (!job) return undefined;
  for (const port of Object.keys(job.outputs).sort()) {
    if (job.outputs[port].length) return job.outputs[port][0];
  }
  return undefined;
}

function decoded(row: ResultRow): ContractResult {
  return { ...row, detail: JSON.parse(row.detail) };
}

export class ContractRunner {
  constructor(readonly runner: Runner) {
    runner.context.db.exec(`
      CREATE TABLE IF NOT EXISTS contract_runs (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL,
        verdict TEXT NOT NULL CHECK (verdict IN ('pass','fail')), detail TEXT NOT NULL, at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS contract_runs_subject ON contract_runs(subject,kind);
    `);
  }

  async runValidator(typeId: string, name: string, timeout: Timeout = {}): Promise<ContractResult> {
    const timeoutMs = timeoutMilliseconds(timeout, undefined, typeId);
    const validator = this.validator(this.runner.snapshot, typeId, name);
    const declared = this.runner.snapshot.factsDecl(nominalTypeId(typeId));
    const accept: Array<{ path: string; ok: boolean }> = [];
    const reject: Array<{ path: string; ok: boolean }> = [];
    let code: string | undefined;
    const fixtures = [
      ...(validator.fixtures?.accept ?? []).map((path) => ({ path, wanted: true })),
      ...(validator.fixtures?.reject ?? []).map((path) => ({ path, wanted: false })),
    ].map((item) => ({ ...item, fixture: this.fixture(item.path) }));
    for (const { path, wanted, fixture } of fixtures) {
      const accepted = fixture === undefined ? undefined : await this.validatorAccepts(typeId, validator, declared, fixture, timeoutMs);
      const ok = accepted === wanted;
      (wanted ? accept : reject).push({ path, ok });
      if (!ok) code ??= wanted || accepted === undefined ? "CONTRACT_FIXTURE_REJECTED" : "CONTRACT_FIXTURE_ACCEPTED";
    }
    return this.record("validator", name, { accept, reject }, code);
  }

  async runGolden(reference: string, baseline?: string, timeout: Timeout = {}): Promise<ContractResult> {
    const [key, spec] = this.spec<Golden>("GoldenSet", reference);
    const [station, binding] = this.spec<Station>("Station", spec.station);
    this.ensureNonPublishing(station, binding);
    if (typeof spec.budget_usd !== "number" || !Number.isFinite(spec.budget_usd) || spec.budget_usd < 0) {
      throw new FactoryError("CONTRACT_BUDGET_REQUIRED", "GoldenSet 需要费用上限", { golden: key });
    }
    const timeoutMs = timeoutMilliseconds(timeout, binding, key);
    const metrics = baseline === undefined ? undefined : this.spec<{ metrics?: Record<string, unknown> }>("Baseline", baseline)[1].metrics ?? {};
    const gates: Array<{ metric: string; key: string; limit: number; observed: number; ok: boolean }> = [];
    const cases: Array<{ name: string; job: string | null; sealed: boolean }> = [];
    const observed: Record<string, number> = { cost_usd: 0, gate_reject_rate: 0 };
    const unknown = Object.keys(metrics ?? {}).find((metric) => !Object.hasOwn(observed, metric));
    if (unknown !== undefined) return this.record("golden", key, { cases, observed, gates, metric: unknown }, "CONTRACT_METRIC_MISSING");
    const thresholds: Array<{ metric: string; key: string; limit: number; max: boolean }> = [];
    for (const [metric, declaration] of Object.entries(metrics ?? {})) {
      if (!object(declaration)) throw new FactoryError("CONTRACT_METRIC_UNKNOWN", "指标声明必须是对象", { metric });
      for (const [key, limit] of Object.entries(declaration)) {
        if (!/^(max|min)(?:_|$)/.test(key)) continue;
        if (typeof limit !== "number" || !Number.isFinite(limit)) {
          throw new FactoryError("CONTRACT_METRIC_UNKNOWN", "指标门槛必须是有限数", { metric, key });
        }
        thresholds.push({ metric, key, limit, max: key.startsWith("max") });
      }
    }
    for (const item of spec.cases) {
      const inputs = await this.goldenInputs(binding, item.inputs ?? {}, timeoutMs);
      const job = inputs === undefined ? undefined : await this.runStation(station, inputs, timeoutMs, item.params);
      cases.push({ name: item.name, job: job?.id ?? null, sealed: job?.state === "SEALED" });
      observed.cost_usd = Number((observed.cost_usd + (job?.costUsd ?? 0)).toFixed(6));
      if (observed.cost_usd > spec.budget_usd) break;
    }
    observed.gate_reject_rate = cases.length ? cases.filter(({ sealed }) => !sealed).length / cases.length : 0;
    for (const { metric, key, limit, max } of thresholds) {
      const value = observed[metric];
      gates.push({ metric, key, limit, observed: value, ok: max ? value <= limit : value >= limit });
    }
    const detail = { cases, observed, gates };
    if (observed.cost_usd > spec.budget_usd) {
      return this.record("golden", key, { ...detail, golden: key, spent: observed.cost_usd, cap: spec.budget_usd }, "CONTRACT_BUDGET_EXCEEDED");
    }
    const exceeded = gates.find(({ ok }) => !ok);
    if (exceeded) return this.record("golden", key, { ...detail, exceeded }, "CONTRACT_BASELINE_EXCEEDED");
    return this.record("golden", key, detail, metrics === undefined && cases.some(({ sealed }) => !sealed) ? "CONTRACT_FIXTURE_REJECTED" : undefined);
  }

  async runEquivalence(reference: string, timeout: Timeout = {}): Promise<ContractResult> {
    const [station, spec] = this.spec<Station>("Station", reference);
    if (spec.role !== "converter") return notFound(station);
    const timeoutMs = timeoutMilliseconds(timeout, spec, station);
    const inputPort = this.inputPort(station, spec);
    const accept = [];
    const reject = [];
    let code: string | undefined;
    for (const [wanted, paths] of [[true, spec.fixtures?.accept ?? []], [false, spec.fixtures?.reject ?? []]] as const) {
      for (const path of paths) {
        const source = await this.admit(spec.inputs![inputPort].type, path, timeoutMs);
        const job = source === undefined ? undefined : await this.runStation(station, { [inputPort]: source }, timeoutMs);
        const output = job?.state === "SEALED" ? firstOutput(job) : undefined;
        if (!wanted) {
          reject.push({ path, sealed: output !== undefined });
          if (output !== undefined) code ??= "CONTRACT_FIXTURE_ACCEPTED";
          continue;
        }
        const failedExpr: number[] = [];
        if (source !== undefined && output !== undefined && job) {
          const store = this.runner.context.warehouse;
          const env = { src: beanValue(store.get(source)), dst: beanValue(store.get(output)), params: job.params } as EvalEnvironment;
          for (const [index, expr] of (spec.equivalence ?? []).entries()) {
            try {
              if (compileExpr(expr, { environment: { src: DYN, dst: DYN, params: DYN }, expect: BOOL }).evaluate(env) === true) continue;
            } catch { /* Failed evaluation is a failed equivalence. */ }
            failedExpr.push(index);
          }
        }
        accept.push({ path, sealed: output !== undefined, failed_expr: failedExpr });
        if (output === undefined) code ??= "CONTRACT_FIXTURE_REJECTED";
        else if (failedExpr.length) code ??= "CONTRACT_EQUIVALENCE_FAILED";
      }
    }
    return this.record("equivalence", station, { accept, reject }, code);
  }

  async runDiff(reference: string, timeout: Timeout = {}): Promise<ContractResult> {
    const [station, spec] = this.spec<Station>("Station", reference);
    this.ensureNonPublishing(station, spec);
    const snapshot = this.runner.snapshot;
    const metadata = snapshot.doc(station).metadata;
    const previous = snapshot.keys("Station").filter((key) => {
      const other = snapshot.doc(key).metadata;
      return other.name === metadata.name && Number(other.version) < Number(metadata.version);
    }).sort((left, right) => Number(snapshot.doc(right).metadata.version) - Number(snapshot.doc(left).metadata.version))[0];
    if (!previous) return this.record("diff", station, { reason: "no_previous", cases: [] });
    const previousSpec: Station = snapshot.spec(previous);
    this.ensureNonPublishing(previous, previousSpec);
    const timeoutMs = timeoutMilliseconds(timeout, spec, station);
    const port = this.inputPort(station, spec);
    const oldPort = this.inputPort(previous, previousSpec);
    const stored = this.runner.context.db.query<{ inputs: string }>("SELECT inputs FROM jobs WHERE station=? AND state='SEALED'").all(previous);
    const inputs = new Set<string>(stored.flatMap(({ inputs }) => Object.values(JSON.parse(inputs) as Record<string, string[]>).flat()));
    for (const path of spec.fixtures?.accept ?? []) {
      const bean = await this.admit(spec.inputs![port].type, path, timeoutMs);
      if (bean !== undefined) inputs.add(bean);
    }
    const cases = [];
    let regression: string | undefined;
    let factDifference: { input: string; fact: string } | undefined;
    for (const input of [...inputs].sort()) {
      const oldJob = await this.runStation(previous, { [oldPort]: input }, timeoutMs);
      const newJob = await this.runStation(station, { [port]: input }, timeoutMs);
      const old_sealed = oldJob?.state === "SEALED";
      const new_sealed = newJob?.state === "SEALED";
      const fact = old_sealed && new_sealed ? this.firstDifferentFact(oldJob, newJob) : undefined;
      if (old_sealed && !new_sealed) regression ??= input;
      if (fact !== undefined) factDifference ??= { input, fact };
      cases.push({ input, old_sealed, new_sealed, facts_equal: old_sealed && new_sealed ? fact === undefined : null });
    }
    const detail = { reason: null, cases };
    if (regression !== undefined) return this.record("diff", station, { ...detail, input: regression }, "CONTRACT_DIFF_REGRESSION");
    if (factDifference) return this.record("diff", station, { ...detail, ...factDifference }, "CONTRACT_DIFF_FACTS");
    return this.record("diff", station, detail);
  }

  async runSinkDryRun(reference: string, options: Timeout & { inputs: Inputs; params: Record<string, unknown> }): Promise<ContractResult> {
    const snapshot = this.runner.snapshot;
    const station = snapshot.key("Station", reference);
    const spec = snapshot.keys("Station").includes(station) ? snapshot.spec(station) as Station : undefined;
    if (spec?.role !== "sink" || !spec.phases?.includes("propose")) {
      throw new FactoryError("CONTRACT_SINK_NO_PROPOSE", "试运行需要两阶段 Sink 工位", { station });
    }
    const timeoutMs = timeoutMilliseconds(options, spec, station);
    const queued = this.runner.submit(station, options.inputs, options.params, undefined, { proposalOnly: true, timeoutMs });
    const job = await this.runner.run(queued.id);
    const decision = this.runner.approvals.proposal(job.id, job.attempt);
    const approval = decision ? { id: decision.id, status: decision.status } : null;
    const receipt = this.runner.context.db.query<{ id: string; status: string }>("SELECT id,status FROM receipts WHERE job_id=? LIMIT 1").get(job.id);
    const detail = { approval, receipt: receipt ?? null, executed_run: job.phase === "execute", job: job.id };
    if (receipt || detail.executed_run) return this.record("sink_dry_run", station, detail, "CONTRACT_SINK_PUBLISHED");
    if (job.state !== "SEALED") return this.record("sink_dry_run", station, detail, typeof job.error?.code === "string" ? job.error.code : undefined, "fail");
    return this.record("sink_dry_run", station, detail);
  }

  checkFixtureSet(old: Snapshot, current: Snapshot, typeId: string, name: string): ContractResult {
    const prior = this.validator(old, typeId, name).fixtures?.reject ?? [];
    const next = new Set(this.validator(current, typeId, name).fixtures?.reject ?? []);
    const removed = [...new Set(prior.filter((path) => !next.has(path)))].sort();
    return this.record("fixture_set", name, { removed }, removed.length ? "CONTRACT_FIXTURE_REMOVED" : undefined);
  }

  async canary(reference: string, timeout: Timeout = {}): Promise<ContractResult> {
    const [station] = this.spec<Station>("Station", reference);
    const snapshot = this.runner.snapshot;
    const matching = (kind: string) => snapshot.keys(kind).find((key) => snapshot.key("Station", snapshot.spec(key).station) === station);
    const golden = matching("GoldenSet");
    if (golden === undefined) return notFound(station);
    return this.runGolden(golden, matching("Baseline"), timeout);
  }

  get(id: string): ContractResult {
    const row = this.runner.context.db.query<ResultRow>("SELECT * FROM contract_runs WHERE id=?").get(id);
    return row ? decoded(row) : notFound(id);
  }

  list({ kind, subject }: { kind?: ContractKind; subject?: string } = {}): ContractResult[] {
    return this.runner.context.db.query<ResultRow>(`SELECT * FROM contract_runs
      WHERE (? IS NULL OR kind=?) AND (? IS NULL OR subject=?) ORDER BY at,id`).all(kind ?? null, kind ?? null, subject ?? null, subject ?? null).map(decoded);
  }

  private record(kind: ContractKind, subject: string, detail: Record<string, unknown>, code?: string, verdict = code ? "fail" as const : "pass" as const): ContractResult {
    const { db, ids, clock } = this.runner.context;
    // Results contain case labels, references, derived metrics and approval IDs, never payload or process output.
    const cleaned = redactResult(code ? { ...detail, code } : detail) as Record<string, unknown>;
    const result = { id: ids.new("ct"), kind, subject, verdict, detail: cleaned, at: utcIso(clock.now()) };
    db.query("INSERT INTO contract_runs (id,kind,subject,verdict,detail,at) VALUES (?,?,?,?,?,?)")
      .run(result.id, kind, subject, verdict, canonicalJson(result.detail), result.at);
    return result;
  }

  private spec<T>(kind: string, reference: string): [string, T] {
    const { snapshot } = this.runner;
    const key = snapshot.key(kind, reference);
    return snapshot.keys(kind).includes(key) ? [key, snapshot.spec(key) as T] : notFound(reference);
  }

  private validator(snapshot: Snapshot, typeId: string, name: string): Validator {
    try { return snapshot.validators(nominalTypeId(typeId)).find((item: Validator) => item.name === name) ?? notFound(name); }
    catch (error) {
      if (error instanceof FactoryError && error.code === "TYPE_NOT_FOUND") return notFound(typeId);
      throw error;
    }
  }

  private fixture(path: string): string | undefined {
    const root = realpathSync(this.runner.snapshot.root);
    const candidate = resolve(root, path);
    const invalid = () => new FactoryError("CONTRACT_FIXTURE_PATH", "样例路径越出 Registry 根", { path });
    if (isAbsolute(path) || !underRoot(root, candidate)) throw invalid();
    try {
      const canonical = realpathSync(candidate);
      if (!underRoot(root, canonical)) throw invalid();
      return lstatSync(canonical).isFile() ? canonical : undefined;
    } catch (error) {
      if ((error as { code?: string }).code === "ENOENT" || (error as { code?: string }).code === "ENOTDIR") return undefined;
      throw error;
    }
  }

  private async validatorAccepts(typeId: string, validator: Validator, facts: Record<string, string>, fixture: string, timeoutMs: number): Promise<boolean> {
    const name = `ct-${randomUUID()}`;
    const root = join(this.runner.context.root, "tmp", name);
    const bean = join(root, "bean");
    const payload = join(bean, "payload");
    const file = join(payload, basename(fixture));
    mkdirSync(payload, { recursive: true });
    try {
      await copyFile(fixture, file);
      const size = lstatSync(file).size;
      const digest = await digestFile(file);
      for (const token of new Set(validator.run.filter((argument) => argument.startsWith("/bean/payload/")))) {
        const alias = resolve(payload, token.slice("/bean/payload/".length));
        if (!underRoot(payload, alias)) return false;
        if (alias === file) continue;
        mkdirSync(dirname(alias), { recursive: true });
        await copyFile(file, alias);
      }
      await Bun.write(join(bean, "bean.json"), canonicalJson({ type: typeId, facts: {}, meta: {}, payload: [{ path: basename(file), digest, size }] }));
      const sandbox = await this.runner.provider.open({ name, image: validator.image, network: "none", mounts: [{ source: bean, target: "/bean", readonly: true }] });
      const stdout = join(root, "stdout");
      let result;
      try { result = await sandbox.exec(validator.run, { timeoutMs, stdout, stderr: join(root, "stderr") }); }
      finally { await sandbox.close(); }
      if (result.timedOut || result.exitCode !== 0) return false;
      let output: unknown;
      try { output = JSON.parse(await Bun.file(stdout).text()); } catch { return false; }
      return object(output) && validator.emits.every((fact) => Object.hasOwn(output, fact) && factMatches(facts[fact], output[fact]));
    } finally { await rm(root, { recursive: true, force: true, maxRetries: 3 }); }
  }

  private ensureNonPublishing(station: string, spec: Station): void {
    if (spec.role === "sink") {
      throw new FactoryError("CONTRACT_SINK_NO_PROPOSE", "发布工位的契约测试需要方案试运行", { station });
    }
  }

  private inputPort(station: string, spec: Station): string {
    const port = Object.keys(spec.inputs ?? {})[0];
    if (port === undefined) return notFound(station);
    return port;
  }

  private async admit(type: string, path: string, timeoutMs: number): Promise<string | undefined> {
    const fixture = this.fixture(path);
    if (!fixture) return undefined;
    const { snapshot, context, provider } = this.runner;
    const typeId = nominalTypeId(type.startsWith("list<") && type.endsWith(">") ? type.slice(5, -1) : type);
    const schema = snapshot.spec(snapshot.typeKey(typeId));
    const declaration = (schema.payload ?? [])[0]?.path;
    const file = typeof declaration === "string" ? declaration.replaceAll("*", basename(fixture)) : basename(fixture);
    const gate = new Gate(snapshot, context.warehouse, join(context.root, "tmp", "contract-gate"), provider, timeoutMs);
    const { bean, report } = await gate.admitExternal(typeId, { [file]: fixture }, {});
    if (!bean) return undefined;
    context.warehouse.sealMany([[bean.id, report.data.facts[bean.id] ?? {}]], null, report.digest());
    return bean.id;
  }

  private async goldenInputs(station: Station, inputs: Inputs, timeoutMs: number): Promise<Inputs | undefined> {
    const entries: Array<[string, string | string[]]> = [];
    for (const [port, value] of Object.entries(inputs)) {
      const type = station.inputs?.[port]?.type;
      if (!type) return undefined;
      const ids: string[] = [];
      for (const item of Array.isArray(value) ? value : [value]) {
        const resolved = item.startsWith("sha256:") ? item : await this.admit(type, item, timeoutMs);
        if (!resolved) return undefined;
        ids.push(resolved);
      }
      entries.push([port, Array.isArray(value) ? ids : ids[0]]);
    }
    return Object.fromEntries(entries);
  }

  private async runStation(station: string, inputs: Inputs, timeoutMs: number, params: Record<string, unknown> = {}): Promise<Job | undefined> {
    try { return await this.runner.run(this.runner.submit(station, inputs, params, undefined, { timeoutMs }).id); }
    catch (error) {
      if (error instanceof FactoryError && ["INTAKE_REJECTED", "PARAM_INVALID"].includes(error.code)) return undefined;
      throw error;
    }
  }

  private firstDifferentFact(oldJob: Job, newJob: Job): string | undefined {
    const store = this.runner.context.warehouse;
    const oldBean = firstOutput(oldJob);
    const newBean = firstOutput(newJob);
    const oldFacts = oldBean ? store.get(oldBean).facts : {};
    const newFacts = newBean ? store.get(newBean).facts : {};
    return Object.keys(oldFacts).sort().find((name) => Object.hasOwn(newFacts, name) && canonicalJson(oldFacts[name]) !== canonicalJson(newFacts[name]));
  }
}
