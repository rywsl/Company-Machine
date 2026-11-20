import { existsSync, mkdirSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { canonicalBytes, digestBytes } from "./foundation";
import { FactoryError } from "./errors";
import type { Snapshot } from "./snapshot";
import type { Bean, Warehouse } from "./warehouse";

export interface GateCheck { port: string; index: number; stage: string; name: string; ok: boolean; detail: string; }
export interface GateReportData { job_id: string; station: string; attempt: number; verdict: "pass" | "reject" | "error"; outputs: Record<string, string[]>; facts: Record<string, Record<string, unknown>>; checks: GateCheck[]; feedback: string; }
export class GateReport {
  constructor(readonly data: GateReportData) {}
  toJSON(): GateReportData { return structuredClone(this.data); }
  digest(): string { return digestBytes(canonicalBytes(this.data)); }
}
function typeId(type: string): string { const match = /^(.+@[0-9]+)(?:\.[0-9]+)?$/.exec(type); return match?.[1] ?? type; }
function parseType(value: string): { list: boolean; type: string } { const match = /^list<([^<>]+)>$/.exec(value); return match ? { list: true, type: match[1] } : { list: false, type: value }; }
function failReport(jobId: string, station: string, attempt: number, checks: GateCheck[], outputs: Record<string, string[]> = {}): GateReport { const failed = checks.filter((check) => !check.ok); return new GateReport({ job_id: jobId, station, attempt, verdict: failed.length ? "reject" : "pass", outputs, facts: {}, checks, feedback: failed.map((check) => `- ${check.port} ${check.stage} ${check.name}: ${check.detail}`).join("\n") }); }

export class Gate {
  constructor(readonly snapshot: Snapshot, readonly warehouse: Warehouse, readonly workdir: string) { mkdirSync(workdir, { recursive: true }); }
  checkInputs(stationRef: string, inputs: Record<string, string | string[]>): Record<string, Bean | Bean[]> {
    const station = this.snapshot.key("Station", stationRef); const spec = this.snapshot.spec(station); const declared = (spec.inputs ?? {}) as Record<string, any>; const result: Record<string, Bean | Bean[]> = {}; const problems: Array<Record<string, unknown>> = [];
    for (const [port, declaration] of Object.entries(declared)) {
      const parsed = parseType(String(declaration.type)); const optional = declaration.optional === true;
      if (!(port in inputs)) { if (!optional) problems.push({ port, index: 0, reason: "missing" }); continue; }
      const value = inputs[port]; const ids = parsed.list ? Array.isArray(value) ? value : [] : Array.isArray(value) ? [] : [value];
      if (!ids.length && (parsed.list ? !Array.isArray(value) : Array.isArray(value))) { problems.push({ port, index: 0, reason: "cardinality" }); continue; }
      const beans: Bean[] = [];
      ids.forEach((raw, index) => { try { const bean = this.warehouse.get(String(raw)); if (!bean.sealed) throw new FactoryError("INTAKE_REJECTED", "Bean 未封存", { beanId: bean.id }); if (!this.snapshot.isSubtype(typeId(bean.type), parsed.type)) throw new FactoryError("INTAKE_REJECTED", "Bean 类型不匹配", { beanId: bean.id, expected: parsed.type }); beans.push(bean); } catch (error) { problems.push({ port, index, reason: error instanceof FactoryError ? error.code : "missing", bean: String(raw) }); } });
      if (!problems.some((problem) => problem.port === port)) result[port] = parsed.list ? beans : beans[0];
    }
    for (const port of Object.keys(inputs)) if (!(port in declared)) problems.push({ port, index: 0, reason: "extra" });
    if (problems.length) throw new FactoryError("INTAKE_REJECTED", "来料检验未通过", { problems });
    return result;
  }
  async admitExternal(type: string, files: Record<string, string>, meta: Record<string, unknown>, jobId = "external", attempt = 0): Promise<{ report: GateReport; bean: Bean }> {
    const key = this.snapshot.typeKey(type); const document = this.snapshot.doc(key); const version = String(document.metadata.version); const name = String(document.metadata.name); const bean = await this.warehouse.add({ type: `${name}@${version}`, meta, files });
    const report = new GateReport({ job_id: jobId, station: "external", attempt, verdict: "pass", outputs: { "": [bean.id] }, facts: {}, checks: [{ port: "", index: 0, stage: "structure", name: "registered", ok: true, detail: "" }], feedback: "" });
    return { report, bean };
  }
  async inspect(stationRef: string, options: { jobId: string; attempt: number; outDir: string; inputs?: Record<string, Bean | Bean[]>; params?: Record<string, unknown> }): Promise<GateReport> {
    const station = this.snapshot.key("Station", stationRef); const spec = this.snapshot.spec(station); const checks: GateCheck[] = []; const outputs: Record<string, string[]> = {}; const facts: Record<string, Record<string, unknown>> = {};
    for (const [port, declaration] of Object.entries((spec.outputs ?? {}) as Record<string, any>)) {
      const parsed = parseType(String(declaration.type)); const candidates = join(options.outDir, port); const files = existsSync(candidates) ? collectFiles(candidates).filter((file) => !file.endsWith("meta.json") && !file.endsWith("bean.json")) : [];
      if (!files.length && declaration.optional !== true) { checks.push({ port, index: 0, stage: "structure", name: "required", ok: false, detail: "缺少必填产物" }); continue; }
      if (files.length) {
        const relativeFiles = Object.fromEntries(files.map((file) => [relative(candidates, file).replaceAll("\\", "/"), file]));
        try { const typeKey = this.snapshot.typeKey(parsed.type); const document = this.snapshot.doc(typeKey); const bean = await this.warehouse.add({ type: `${document.metadata.name}@${document.metadata.version}`, meta: {}, files: relativeFiles }); outputs[port] = [bean.id]; facts[bean.id] = {}; checks.push({ port, index: 0, stage: "structure", name: "registered", ok: true, detail: "" }); }
        catch (error) { checks.push({ port, index: 0, stage: "structure", name: "register", ok: false, detail: error instanceof Error ? error.message : String(error) }); }
      }
    }
    return failReport(options.jobId, station, options.attempt, checks, outputs);
  }
}
function collectFiles(root: string): string[] { const result: string[] = []; for (const name of readdirSync(root)) { const path = join(root, name); if (statSync(path).isDirectory()) result.push(...collectFiles(path)); else result.push(path); } return result.sort(); }
