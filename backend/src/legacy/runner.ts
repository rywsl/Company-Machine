import { mkdirSync } from "node:fs";
import { join } from "node:path";
import type { SandboxSpec } from "../../runtime/podman";
import { buildInvocation, renderBrief } from "./operator";
import { Context } from "./context";
import { Gate } from "./gate";
import { JobStore, terminalStates, type Job } from "./jobs";
import type { Snapshot } from "./snapshot";
import { FactoryError } from "./errors";

export class Runner {
  readonly jobs: JobStore;
  constructor(readonly context: Context, readonly snapshot: Snapshot, readonly provider: any) { this.jobs = new JobStore(context.db, context.clock, context.ids); }
  submit(stationRef: string, inputs: Record<string, string | string[]>, params: Record<string, unknown>): Job {
    const station = this.snapshot.key("Station", stationRef); const checked = new Gate(this.snapshot, this.context.warehouse, join(this.context.root, "tmp", "gate")); const normalized = checked.checkInputs(station, inputs); const ids = Object.fromEntries(Object.entries(normalized).map(([port, value]) => [port, (Array.isArray(value) ? value : [value]).map((bean) => bean.id)])); return this.jobs.create({ station, epoch: this.snapshot.epoch, inputs: ids, params });
  }
  adopt(id: string): Job { return this.jobs.get(id); }
  async run(id: string): Promise<Job> {
    let job = this.jobs.get(id); if (terminalStates.has(job.state)) return job;
    job = this.jobs.transition(id, "PROVISIONING"); const attemptRoot = join(this.context.root, "jobs", job.id, String(job.attempt)); const inDir = join(attemptRoot, "in"); const outDir = join(attemptRoot, "out"); const ctlDir = join(attemptRoot, "ctl"); const workspace = join(attemptRoot, "workspace"); for (const dir of [inDir, outDir, ctlDir, workspace, join(attemptRoot, "logs"), join(attemptRoot, "tmp")]) mkdirSync(dir, { recursive: true });
    const gate = new Gate(this.snapshot, this.context.warehouse, join(attemptRoot, "gate")); const loaded = Object.fromEntries(Object.entries(job.inputs).map(([port, ids]) => [port, ids.length === 1 ? this.context.warehouse.get(ids[0]) : ids.map((item) => this.context.warehouse.get(item))])); for (const [port, value] of Object.entries(loaded)) for (const bean of Array.isArray(value) ? value : [value]) await this.context.warehouse.materialize(bean.id, join(inDir, port), "port");
    const brief = renderBrief(this.snapshot, job.station, { inputs: loaded, params: job.params, attempt: job.attempt, budgetUsd: 0 }); await Bun.write(join(ctlDir, "job.json"), JSON.stringify({ job_id: job.id, attempt: job.attempt, inputs: Object.fromEntries(Object.entries(loaded).map(([port, value]) => [port, Array.isArray(value) ? value.map((bean) => ({ id: bean.id, type: bean.type })) : { id: value.id, type: value.type }])) })); await Bun.write(join(ctlDir, "brief.md"), brief.markdown);
    const invocation = buildInvocation(this.snapshot, job.station, brief, { inputs: loaded, params: job.params }); const station = this.snapshot.spec(job.station); const machine = this.snapshot.spec(this.snapshot.key("Machine", station.machine)); const sandboxSpec: SandboxSpec = { name: `mao-${job.id}-${job.attempt}`, image: machine.runtime?.image ?? "local", mounts: [{ source: inDir, target: "/factory/in", readonly: true }, { source: outDir, target: "/factory/out", readonly: false }, { source: ctlDir, target: "/factory/ctl", readonly: false }, { source: workspace, target: "/workspace", readonly: false }], workdir: "/workspace", network: "none", command: invocation.argv };
    const sandbox = await this.provider.open(sandboxSpec); job = this.jobs.transition(job.id, "RUNNING"); try { const result = await sandbox.exec(invocation.argv, { timeoutMs: invocation.timeoutS * 1000, stdout: join(attemptRoot, "logs", "stdout"), stderr: join(attemptRoot, "logs", "stderr"), env: { FACTORY_IN: inDir, FACTORY_OUT: outDir, FACTORY_CTL: ctlDir, FACTORY_WORKSPACE: workspace, ...invocation.env } }); if (result.timedOut || result.exitCode !== 0) return this.jobs.transition(job.id, "DEAD", { error: { code: result.timedOut ? "OPERATOR_TIMEOUT" : "OPERATOR_FAILED", exit_code: result.exitCode } }); job = this.jobs.transition(job.id, "GATING"); const report = await gate.inspect(job.station, { jobId: job.id, attempt: job.attempt, outDir, inputs: loaded, params: job.params }); if (report.data.verdict !== "pass") return this.jobs.transition(job.id, "DEAD", { feedback: report.data.feedback, error: { code: "GATE_REJECTED" } }); await this.context.warehouse.sealMany(Object.values(report.data.outputs).flat().map((beanId) => [beanId, report.data.facts[beanId] ?? {}] as [string, Record<string, unknown>]), null, report.digest()); this.context.warehouse.recordLineage(job.id, job.inputs, report.data.outputs); return this.jobs.transition(job.id, "SEALED", { outputs: report.data.outputs }); } finally { await sandbox.close(); }
  }
}
