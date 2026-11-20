import { Database } from "bun:sqlite";
import { canonicalJson, type Clock, type IdSource, utcIso } from "./foundation";
import { FactoryError } from "./errors";

export type JobState = "PENDING" | "PROVISIONING" | "RUNNING" | "AWAITING_APPROVAL" | "GATING" | "SEALED" | "REJECTED" | "FAILED" | "DEAD";
export type JobPhase = "propose" | "execute";
export const terminalStates = new Set<JobState>(["SEALED", "DEAD"]);
const edges: Record<JobState, JobState[]> = {
  PENDING: ["PROVISIONING", "SEALED", "DEAD"],
  PROVISIONING: ["RUNNING", "SEALED", "FAILED", "DEAD"],
  RUNNING: ["AWAITING_APPROVAL", "GATING", "FAILED", "DEAD"],
  AWAITING_APPROVAL: ["RUNNING", "FAILED"],
  GATING: ["SEALED", "REJECTED", "FAILED", "DEAD"],
  REJECTED: ["PENDING", "DEAD"],
  FAILED: ["PENDING", "DEAD"],
  SEALED: [],
  DEAD: [],
};

export interface Job {
  id: string;
  station: string;
  epoch: string;
  state: JobState;
  phase: JobPhase;
  attempt: number;
  inputs: Record<string, string[]>;
  params: Record<string, unknown>;
  runValue?: Record<string, unknown>;
  idempotencyKey?: string;
  outputs: Record<string, string[]>;
  costUsd: number;
  error?: Record<string, unknown>;
  feedback?: string;
  createdAt: string;
  updatedAt: string;
}

type JobRow = {
  id: string; station: string; epoch: string; state: JobState; attempt: number;
  inputs: string; params: string; outputs: string; cost_usd: number;
  error: string | null; feedback: string | null; created_at: string; updated_at: string;
  run_value: string | null; idempotency_key: string | null;
  phase: string;
};

type JobPatch = Partial<Pick<Job, "error" | "feedback" | "outputs" | "costUsd" | "phase">> & { external?: boolean };

export function ensureEdge(job: Job, next: JobState): void {
  if (!edges[job.state]?.includes(next)) {
    throw new FactoryError("JOB_STATE_INVALID", `不能从 ${job.state} 转到 ${next}`, { jobId: job.id, state: job.state });
  }
}

export function dumpJob(job: Job): Record<string, unknown> {
  return {
    id: job.id, station: job.station, epoch: job.epoch, state: job.state, phase: job.phase, attempt: job.attempt,
    inputs: job.inputs, params: job.params, outputs: job.outputs, cost_usd: job.costUsd,
    error: job.error ?? null, feedback: job.feedback ?? null, created_at: job.createdAt, updated_at: job.updatedAt,
  };
}

export class JobStore {
  constructor(readonly db: Database, readonly clock: Clock, readonly ids: IdSource) {
    db.exec(`
      CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, station TEXT NOT NULL, epoch TEXT NOT NULL, state TEXT NOT NULL,
        attempt INTEGER NOT NULL, inputs TEXT NOT NULL, params TEXT NOT NULL, outputs TEXT NOT NULL,
        cost_usd REAL NOT NULL DEFAULT 0, error TEXT, feedback TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        run_value TEXT, idempotency_key TEXT, phase TEXT NOT NULL DEFAULT 'execute'
      );
      CREATE TABLE IF NOT EXISTS job_events (
        id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS job_events_kind ON job_events(job_id, kind);
      CREATE TABLE IF NOT EXISTS job_execution_locks (job_id TEXT PRIMARY KEY, pid INTEGER NOT NULL);
    `);
    const definitions: Record<string, string> = { run_value: "TEXT", idempotency_key: "TEXT", phase: "TEXT NOT NULL DEFAULT 'execute'" };
    const missing = () => {
      const columns = new Set(db.query<{ name: string }>("PRAGMA table_info(jobs)").all().map(({ name }) => name));
      return Object.keys(definitions).filter((name) => !columns.has(name));
    };
    if (missing().length) {
      db.transaction(() => {
        for (const column of missing()) db.exec(`ALTER TABLE jobs ADD COLUMN ${column} ${definitions[column]}`);
      }).immediate();
    }
  }

  create(input: Pick<Job, "station" | "epoch" | "inputs" | "params" | "runValue" | "idempotencyKey"> & { phase?: JobPhase }): Job {
    return this.db.transaction(() => {
      const at = utcIso(this.clock.now());
      const job: Job = { ...input, id: this.ids.new("job"), state: "PENDING", phase: input.phase ?? "execute", attempt: 1, outputs: {}, costUsd: 0, createdAt: at, updatedAt: at };
      this.db.query("INSERT INTO jobs (id,station,epoch,state,attempt,inputs,params,outputs,created_at,updated_at,run_value,idempotency_key,phase) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
        .run(job.id, job.station, job.epoch, job.state, job.attempt, canonicalJson(job.inputs), canonicalJson(job.params), canonicalJson(job.outputs), at, at,
          job.runValue === undefined ? null : canonicalJson(job.runValue), job.idempotencyKey ?? null, job.phase);
      this.event(job, "job.created", Object.fromEntries(Object.entries(input).filter(([, value]) => value !== undefined)));
      return job;
    }).immediate();
  }

  get(id: string): Job {
    const row = this.db.query<JobRow>("SELECT * FROM jobs WHERE id = ?").get(id);
    if (!row) throw new FactoryError("JOB_NOT_FOUND", `找不到 job ${id}`, { jobId: id });
    if (row.phase !== "propose" && row.phase !== "execute") throw new FactoryError("PHASE_INVALID", "无效的 Job 阶段", { jobId: id, phase: row.phase });
    return {
      id: row.id, station: row.station, epoch: row.epoch, state: row.state, phase: row.phase, attempt: row.attempt,
      inputs: JSON.parse(row.inputs), params: JSON.parse(row.params), outputs: JSON.parse(row.outputs),
      costUsd: row.cost_usd, error: row.error ? JSON.parse(row.error) : undefined,
      feedback: row.feedback ?? undefined, createdAt: row.created_at, updatedAt: row.updated_at,
      ...(row.run_value ? { runValue: JSON.parse(row.run_value) } : {}),
      ...(row.idempotency_key === null ? {} : { idempotencyKey: row.idempotency_key }),
    };
  }

  transition(id: string, state: JobState, patch: JobPatch = {}): Job {
    return this.db.transaction(() => {
      const before = this.get(id);
      const phaseSwitch = before.phase === "propose" && patch.phase === "execute" && state === "PROVISIONING" &&
        (before.state === "RUNNING" || before.state === "AWAITING_APPROVAL");
      if (!phaseSwitch) ensureEdge(before, state);
      const retry = state === "PENDING";
      const { external, ...changes } = patch;
      const job: Job = {
        ...before, ...changes, state,
        attempt: before.attempt + Number(retry),
        error: retry || state === "SEALED" ? undefined : patch.error ?? before.error,
        updatedAt: utcIso(this.clock.now()),
      };
      this.db.query("UPDATE jobs SET state=?,attempt=?,phase=?,outputs=?,cost_usd=?,error=?,feedback=?,updated_at=? WHERE id=?")
        .run(state, job.attempt, job.phase, canonicalJson(job.outputs), job.costUsd, job.error ? canonicalJson(job.error) : null, job.feedback ?? null, job.updatedAt, id);
      this.event(job, "job.state", { from: before.state, to: state, attempt: job.attempt });
      if (job.phase !== before.phase) this.event(job, "job.phase", { from: before.phase, to: job.phase, attempt: job.attempt });
      if (state === "SEALED") {
        const provenance = external ? { external, station: job.station, params: job.params } : {};
        this.event(job, "job.sealed", { outputs: job.outputs, ...provenance });
        for (const [port, beans] of Object.entries(job.outputs)) {
          beans.forEach((bean, index) => this.event(job, "bean.sealed", { port, bean, index, ...provenance }));
        }
      }
      return job;
    }).immediate();
  }

  countTransitions(id: string, state: JobState): number {
    const row = this.db.query<{ count: number }>(
      "SELECT count(*) AS count FROM job_events WHERE job_id = ? AND kind = 'job.state' AND json_extract(data, '$.to') = ?",
    ).get(id, state);
    return row?.count ?? 0;
  }

  acquireExecution(id: string): void {
    this.db.transaction(() => {
      const lock = this.db.query<{ pid: number }>("SELECT pid FROM job_execution_locks WHERE job_id = ?").get(id);
      if (lock) {
        try {
          process.kill(lock.pid, 0);
          throw new FactoryError("JOB_STATE_INVALID", "工位已有执行进程", { jobId: id });
        } catch (error) {
          if ((error as { code?: unknown }).code !== "ESRCH") throw error;
        }
      }
      this.db.query("INSERT OR REPLACE INTO job_execution_locks (job_id,pid) VALUES (?,?)").run(id, process.pid);
    }).immediate();
  }

  releaseExecution(id: string): void {
    this.db.query("DELETE FROM job_execution_locks WHERE job_id = ? AND pid = ?").run(id, process.pid);
  }

  private event(job: Job, kind: string, data: unknown): void {
    this.db.query("INSERT INTO job_events (id,job_id,kind,data,at) VALUES (?,?,?,?,?)")
      .run(this.ids.new("event"), job.id, kind, canonicalJson(data), utcIso(this.clock.now()));
  }
}
