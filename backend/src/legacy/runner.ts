import { mkdirSync } from "node:fs";
import { join } from "node:path";
import type { ContainerProvider } from "../../runtime/podman";
import type { Context } from "./context";
import { FactoryError } from "./errors";
import { Gate } from "./gate";
import { JobStore, terminalStates, type Job } from "./jobs";
import { buildInvocation, renderBrief } from "./operator";
import type { Snapshot } from "./snapshot";
import { inputsValue, paramsValue } from "./values";
import type { Bean } from "./warehouse";

export class Runner {
  readonly jobs: JobStore;

  constructor(readonly context: Context, readonly snapshot: Snapshot, readonly provider: ContainerProvider) {
    this.jobs = new JobStore(context.db, context.clock, context.ids);
  }

  submit(stationRef: string, inputs: Record<string, string | string[]>, params: Record<string, unknown>): Job {
    const station = this.snapshot.key("Station", stationRef);
    const gate = new Gate(this.snapshot, this.context.warehouse, join(this.context.root, "tmp", "gate"));
    const checkedInputs = gate.checkInputs(station, inputs);
    const ids = Object.fromEntries(Object.entries(checkedInputs).map(([port, value]) => [
      port, (Array.isArray(value) ? value : [value]).map((bean) => bean.id),
    ]));
    return this.jobs.create({
      station, epoch: this.snapshot.epoch, inputs: ids, params: paramsValue(this.snapshot, station, params),
    });
  }

  adopt(id: string): Job { return this.jobs.get(id); }

  async run(id: string): Promise<Job> {
    const job = this.jobs.get(id);
    if (terminalStates.has(job.state)) return job;
    if (job.epoch !== this.snapshot.epoch) throw new FactoryError("EPOCH_MISMATCH", "Job snapshot does not match", { jobId: id });
    this.jobs.transition(id, "PROVISIONING");

    try {
      return await this.runAttempt(job);
    } catch (error) {
      const current = this.jobs.get(id);
      if (terminalStates.has(current.state)) throw error;
      return this.jobs.transition(id, "DEAD", {
        error: {
          code: error instanceof FactoryError ? error.code : "OPERATOR_FAILED",
          message: error instanceof Error ? error.message : String(error),
        },
      });
    }
  }

  private async runAttempt(job: Job): Promise<Job> {
    const attemptRoot = join(this.context.root, "jobs", job.id, String(job.attempt));
    const inDir = join(attemptRoot, "in");
    const outDir = join(attemptRoot, "out");
    const ctlDir = join(attemptRoot, "ctl");
    const workspace = join(attemptRoot, "workspace");
    const logs = join(attemptRoot, "logs");
    for (const dir of [inDir, outDir, ctlDir, workspace, logs, join(attemptRoot, "tmp")]) {
      mkdirSync(dir, { recursive: true });
    }

    const station = this.snapshot.spec(job.station);
    const machine = this.snapshot.spec(this.snapshot.key("Machine", station.machine));
    const loaded: Record<string, Bean | Bean[]> = {};
    for (const [port, ids] of Object.entries(job.inputs)) {
      const beans = ids.map((id) => this.context.warehouse.get(id));
      const isList = String(station.inputs[port].type).startsWith("list<");
      loaded[port] = isList ? beans : beans[0];
      for (const [index, bean] of beans.entries()) {
        const target = isList ? join(inDir, port, String(index)) : join(inDir, port);
        await this.context.warehouse.materialize(bean.id, target, "port");
      }
    }
    const budgetUsd = Number(station.limits?.budget_usd ?? 0);
    const brief = renderBrief(this.snapshot, job.station, { inputs: loaded, params: job.params, attempt: job.attempt, budgetUsd });
    const invocation = buildInvocation(this.snapshot, job.station, brief, { inputs: loaded, params: job.params });
    await Bun.write(join(ctlDir, "job.json"), JSON.stringify({
      job_id: job.id, attempt: job.attempt, inputs: inputsValue(loaded), budget_usd: budgetUsd,
    }));
    await Bun.write(join(ctlDir, "brief.md"), brief.markdown);

    if (!machine.runtime?.image && this.provider.name !== "local") {
      throw new FactoryError("COMPOSE_UNSUPPORTED", "Station requires a pinned machine image", { station: job.station });
    }
    const sandbox = await this.provider.open({
      name: `mao-${job.id}-${job.attempt}`,
      image: machine.runtime?.image ?? "local",
      mounts: [
        { source: inDir, target: "/factory/in", readonly: true },
        { source: outDir, target: "/factory/out", readonly: false },
        { source: ctlDir, target: "/factory/ctl", readonly: false },
        { source: workspace, target: "/workspace", readonly: false },
      ],
      workdir: "/workspace",
      network: "none",
    });
    try {
      this.jobs.transition(job.id, "RUNNING");
      const result = await sandbox.exec(invocation.argv, {
        timeoutMs: invocation.timeoutS * 1000,
        stdout: join(logs, "stdout"),
        stderr: join(logs, "stderr"),
        env: { ...invocation.env, FACTORY_JOB_ID: job.id, FACTORY_ATTEMPT: String(job.attempt) },
      });
      if (result.timedOut || result.exitCode !== 0) {
        return this.jobs.transition(job.id, "DEAD", {
          error: { code: result.timedOut ? "OPERATOR_TIMEOUT" : "OPERATOR_FAILED", exit_code: result.exitCode },
        });
      }
    } finally {
      await sandbox.close();
    }

    this.jobs.transition(job.id, "GATING");
    const gate = new Gate(this.snapshot, this.context.warehouse, join(attemptRoot, "gate"));
    const report = await gate.inspect(job.station, { jobId: job.id, attempt: job.attempt, outDir, inputs: loaded, params: job.params });
    if (report.data.verdict !== "pass") {
      return this.jobs.transition(job.id, "DEAD", { feedback: report.data.feedback, error: { code: "GATE_REJECTED" } });
    }
    const seals = Object.values(report.data.outputs).flat()
      .map((id): [string, Record<string, unknown>] => [id, report.data.facts[id] ?? {}]);
    await this.context.warehouse.sealMany(seals, job.id, report.digest());
    this.context.warehouse.recordLineage(job.id, job.inputs, report.data.outputs);
    return this.jobs.transition(job.id, "SEALED", { outputs: report.data.outputs });
  }
}
