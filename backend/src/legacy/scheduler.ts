import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Context } from "./context";
import { FactoryError } from "./errors";
import { canonicalJson, utcIso } from "./foundation";
import { terminalStates, type Job, type JobState } from "./jobs";
import { LineRunner } from "./line-runner";
import type { Runner } from "./runner";
import { AdmissionProtections, nonnegative, parseAdmission, parsePools, resources, type AdmissionConfig, type Pools, type Resources } from "./scheduler-protections";
import type { Snapshot } from "./snapshot";
import { TriggerEngine } from "./triggers";

type JsonObject = Record<string, unknown>;
type QueueEntry = { id: string; epoch: string; station: string; line: string | null; inputs: string; pid: number | null };
type Claim = Resources & { jobId: string; station: string; pid: number; pool: string };
type Options = {
  lines?: Pick<LineRunner, "start">;
  config?: SchedulerConfig;
  resolveRunner?: (epoch: string) => Promise<Runner>;
};

export interface SchedulerConfig extends AdmissionConfig {
  pools?: Pools;
  wip?: Array<{ station: string; max: number }>;
  run_cap?: number | null;
  cron?: boolean;
}

export interface Demand extends Resources { pool: string; }

export interface DeadLetter {
  jobId: string;
  station: string;
  errorCode: string;
  error: JsonObject;
  at: string;
}

export interface SchedulerTick {
  expired: string[];
  cron: string[];
  triggered: string[];
  pumped: string[];
  dead: string[];
  blockedEpochs?: Record<string, string>;
}

const REDACT = /\b(api[_-]?key|secret|token|password|authorization|cookie)(\s*[:=]\s*)(?:bearer\s+)?(?:"[^"]*"|\S+)/gi;
const SECRET_KEY = /api[_-]?key|secret|token|password|authorization|cookie/i;

function redact(value: unknown): unknown {
  if (typeof value === "string") return value.replace(REDACT, "$1$2[REDACTED]");
  if (Array.isArray(value)) return value.map(redact);
  if (object(value)) return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, SECRET_KEY.test(key) ? "[REDACTED]" : redact(item)]));
  return value;
}

function object(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function liveProcess(pid: number): boolean {
  try { process.kill(pid, 0); return true; }
  catch (error) { return (error as { code?: string }).code !== "ESRCH"; }
}

function parseConfig(value: unknown): SchedulerConfig {
  const invalid = (reason: string): never => {
    throw new FactoryError("SCHEDULER_CONFIG_INVALID", "调度器配置不合法", { reason });
  };
  if (!object(value)) return invalid("配置必须为对象");
  const result: SchedulerConfig = {};
  if (value.pools !== undefined) result.pools = parsePools(value.pools, "pools");
  if (value.wip !== undefined) {
    if (!Array.isArray(value.wip)) return invalid("wip 必须为列表");
    result.wip = value.wip.map((item) => {
      if (!object(item) || typeof item.station !== "string" || !item.station || !nonnegative(item.max, true)) {
        return invalid("wip 条目需要 station 和非负整数 max");
      }
      return { station: item.station, max: item.max };
    });
  }
  if (value.run_cap !== undefined) {
    if (value.run_cap !== null && !nonnegative(value.run_cap, true)) return invalid("run_cap");
    result.run_cap = value.run_cap;
  }
  if (value.cron !== undefined) {
    if (typeof value.cron !== "boolean") return invalid("cron");
    result.cron = value.cron;
  }
  return { ...result, ...parseAdmission(value, result.pools) };
}

export class Scheduler {
  private config?: SchedulerConfig;
  private readonly triggers: TriggerEngine;
  private readonly runners = new Map<string, Runner>();
  private readonly lines = new Map<string, LineRunner>();
  private readonly demands = new Map<string, Resources & { pool?: string }>();
  private readonly admission: AdmissionProtections;

  constructor(readonly context: Context, readonly snapshot: Snapshot, readonly runner: Runner, private readonly options: Options = {}) {
    const { db } = this.context;
    const lines = new LineRunner(runner);
    this.runners.set(snapshot.epoch, runner);
    this.lines.set(snapshot.epoch, lines);
    this.triggers = new TriggerEngine(context, snapshot, options.lines ?? lines);
    db.exec(`
      CREATE TABLE IF NOT EXISTS schedule_queue (job_id TEXT PRIMARY KEY, enqueued_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS schedule_claims (
        job_id TEXT PRIMARY KEY, station TEXT NOT NULL, pid INTEGER NOT NULL,
        cpu REAL NOT NULL, memory_mb INTEGER NOT NULL, gpu INTEGER NOT NULL, pool TEXT NOT NULL DEFAULT '*'
      );
      CREATE TABLE IF NOT EXISTS dead_letters (
        job_id TEXT PRIMARY KEY, station TEXT NOT NULL, error_code TEXT NOT NULL, error TEXT NOT NULL, at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS quarantines (
        bean_id TEXT PRIMARY KEY, reason TEXT NOT NULL, by TEXT NOT NULL, at TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('manual', 'auto'))
      );
      CREATE INDEX IF NOT EXISTS scheduler_jobs_state ON jobs(state, epoch);
    `);
    db.transaction(() => {
      const columns = db.query<{ name: string }>("PRAGMA table_info(schedule_claims)").all();
      if (!columns.some(({ name }) => name === "pool")) db.exec("ALTER TABLE schedule_claims ADD COLUMN pool TEXT NOT NULL DEFAULT '*'");
    }).immediate();
    this.admission = new AdmissionProtections(context, (kind, data, jobId) => this.event(kind, data, jobId));
    if (options.config !== undefined) this.configure(options.config);
  }

  configure(config: SchedulerConfig): void {
    this.config = parseConfig(config);
    this.demands.clear();
  }

  loadConfig(path = join(this.context.root, "scheduler.json")): void {
    let config: unknown;
    try { config = JSON.parse(readFileSync(path, "utf8")); }
    catch (error) {
      if ((error as { code?: string }).code === "ENOENT") {
        throw new FactoryError("SCHEDULER_CONFIG_MISSING", "缺少 scheduler.json", { path });
      }
      if (error instanceof SyntaxError) throw new FactoryError("SCHEDULER_CONFIG_INVALID", "scheduler.json 不是有效 JSON", { path });
      throw error;
    }
    this.config = parseConfig(config);
    this.demands.clear();
  }

  demand(stationRef: string, snapshot = this.snapshot, line?: string): Demand {
    const config = this.requireConfig();
    const stationKey = snapshot.key("Station", stationRef);
    const key = snapshot.epoch + ":" + stationKey;
    let need = this.demands.get(key);
    if (!need) {
      need = this.stationDemand(stationKey, snapshot);
      this.demands.set(key, need);
    }
    const matched = config.compartments?.find((compartment) => need.pool
      ? compartment.name === need.pool : compartment.match.line === line);
    if (need.pool && need.pool !== "*" && !matched) {
      throw new FactoryError("SCHEDULER_CAPACITY_UNSET", "调度隔舱未配置", { station: stationKey, pool: need.pool });
    }
    const demand = { ...need, pool: matched?.name ?? "*" };
    for (const [field, resource] of resources) {
      if (demand[field] > 0 && (config.pools?.[resource] === undefined || (matched && matched.pools[resource] === undefined))) {
        throw new FactoryError("SCHEDULER_CAPACITY_UNSET", "调度资源容量未配置", { station: stationKey, resource, pool: demand.pool });
      }
    }
    return demand;
  }

  private stationDemand(stationKey: string, snapshot: Snapshot): Resources & { pool?: string } {
    const station = snapshot.spec(stationKey);
    const machine = snapshot.spec(snapshot.key("Machine", station.machine));
    const policy = snapshot.spec(snapshot.key("Policy", station.policy));
    const declaration = { ...machine.runtime?.resources, ...policy.resources };
    const cpu = declaration.cpus ?? 0;
    const invalid = (key: string) => new FactoryError(
      "SCHEDULER_RESOURCE_INVALID", "工位资源格式不合法", { station: stationKey, key },
    );
    if (!nonnegative(cpu)) throw invalid("cpus");
    const ram = declaration.memory;
    const match = typeof ram === "string" ? /^(\d+)(Mi|Gi)$/.exec(ram) : null;
    if (ram !== undefined && ram !== 0 && !match) throw invalid("memory");
    const memoryMb = match ? Number(match[1]) * (match[2] === "Gi" ? 1024 : 1) : 0;
    if (!nonnegative(memoryMb, true)) throw invalid("memory");
    const gpuValue = declaration.gpu ?? "none";
    const gpu = typeof gpuValue === "string" ? gpuValue === "none" || gpuValue === "" ? 0 : 1 : gpuValue;
    if (!nonnegative(gpu, true)) throw invalid("gpu");

    return { cpu, memoryMb, gpu, pool: typeof policy.resources?.pool === "string" ? policy.resources.pool : undefined };
  }

  enqueue(jobIds: readonly string[]): void {
    this.requireConfig();
    const { db } = this.context;
    db.transaction(() => {
      const at = this.now();
      const insert = db.query("INSERT INTO schedule_queue (job_id, enqueued_at) VALUES (?, ?) ON CONFLICT (job_id) DO NOTHING");
      for (const id of [...new Set(jobIds)].sort()) {
        const job = this.runner.jobs.get(id);
        if (job.state === "DEAD") this.recordDead(job);
        else if (!terminalStates.has(job.state)) insert.run(id, at);
      }
    }).immediate();
  }

  queued(): string[] {
    return this.context.db.query<{ job_id: string }>("SELECT job_id FROM schedule_queue ORDER BY job_id").all().map(({ job_id }) => job_id);
  }

  async pump(): Promise<string[]> {
    await this.prepareEpochs();
    return this.pumpPrepared();
  }

  private async pumpPrepared(): Promise<string[]> {
    const { db } = this.context;
    const chosen = this.claimReady();
    const remaining = new Set(chosen);
    const completed: string[] = [];
    try {
      for (const id of chosen) {
        const queued = this.runner.jobs.get(id);
        const job = await this.runners.get(queued.epoch)!.run(id);
        db.transaction(() => {
          if (job.state === "DEAD") this.recordDead(job);
          if (terminalStates.has(job.state)) db.query("DELETE FROM schedule_queue WHERE job_id = ?").run(id);
          db.query("DELETE FROM schedule_claims WHERE job_id = ? AND pid = ?").run(id, process.pid);
          this.admission.refresh(this.requireConfig(), this.loadedSnapshots());
        }).immediate();
        remaining.delete(id);
        completed.push(id);
      }
      return completed.sort();
    } finally {
      const release = db.query("DELETE FROM schedule_claims WHERE job_id = ? AND pid = ?");
      db.transaction(() => { for (const id of remaining) release.run(id, process.pid); }).immediate();
    }
  }

  async tick(options: { pump?: boolean } = {}): Promise<SchedulerTick> {
    const config = this.requireConfig();
    const expired = this.runner.approvals.expireDue();
    const { db } = this.context;
    const { cron, triggered } = db.transaction(() => {
      const active = db.query<{ count: number }>(
        "SELECT COUNT(*) AS count FROM runs WHERE state NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED')",
      ).get()?.count ?? 0;
      const limit = config.run_cap == null ? Infinity : Math.max(0, config.run_cap - active);
      if (!limit) this.event("scheduler.backpressure", { scope: "run", id: "", reason: "capacity" });
      const triggered = this.triggers.process({ limit });
      const cron = this.triggers.processCron({ enabled: config.cron ?? false, limit: limit - triggered.length });
      return { cron, triggered };
    }).immediate();
    const dead = this.scanDead();
    const blockedEpochs = await this.prepareEpochs();
    this.advanceRuns();
    if (options.pump === false) this.refreshAdmission();
    const pumped = options.pump === false ? [] : await this.pumpPrepared();
    for (const id of pumped) {
      if (this.runner.jobs.get(id).state === "DEAD") dead.push(id);
    }
    if (pumped.length) this.advanceRuns();
    return { expired, cron, triggered, pumped, dead: [...new Set(dead)].sort(),
      ...(Object.keys(blockedEpochs).length ? { blockedEpochs } : {}) };
  }

  private scheduledRuns(): Array<{ id: string; epoch: string }> {
    return this.context.db.query<{ id: string; epoch: string }>(`
      SELECT id, epoch FROM runs
      WHERE state IN ('PENDING', 'RUNNING', 'FAILING', 'CANCELLING')
        AND json_extract(trigger, '$.on') IN ('bean', 'cron')
      ORDER BY id
    `).all();
  }

  private async prepareEpochs(): Promise<Record<string, string>> {
    const { db } = this.context;
    const epochs = new Set(this.scheduledRuns().map(({ epoch }) => epoch));
    for (const { epoch } of db.query<{ epoch: string }>(`
      SELECT DISTINCT jobs.epoch FROM schedule_queue JOIN jobs ON jobs.id = schedule_queue.job_id
      WHERE jobs.state NOT IN ('SEALED', 'DEAD')
    `).all()) epochs.add(epoch);
    const blocked: Record<string, string> = Object.create(null);
    for (const epoch of epochs) {
      if (this.runners.has(epoch)) continue;
      if (!this.options.resolveRunner) {
        blocked[epoch] = "EPOCH_NOT_FOUND";
        continue;
      }
      try {
        const runner = await this.options.resolveRunner(epoch);
        if (runner.snapshot.epoch !== epoch) throw new FactoryError("EPOCH_MISMATCH", "调度器收到不匹配的 Registry 快照", { epoch });
        this.runners.set(epoch, runner);
        this.lines.set(epoch, new LineRunner(runner));
      } catch (error) {
        if (!(error instanceof FactoryError) || !["EPOCH_NOT_FOUND", "EPOCH_MISMATCH", "EPOCH_CORRUPT"].includes(error.code)) throw error;
        blocked[epoch] = error.code;
      }
    }
    return blocked;
  }

  private advanceRuns(): void {
    for (const { id, epoch } of this.scheduledRuns()) {
      this.lines.get(epoch)?.tick(id, (jobs) => this.enqueue(jobs));
    }
  }

  async waitChange(timeoutS: number): Promise<void> {
    const expired = this.runner.approvals.expireDue();
    const pumped = await this.pump();
    if (!expired.length && !pumped.length) await this.context.clock.sleep(timeoutS);
  }

  isQuarantined(beanId: string): boolean {
    return Boolean(this.context.db.query("SELECT 1 FROM quarantines WHERE bean_id = ?").get(beanId));
  }

  quarantine(beanId: string, options: { by: string; reason: string }): void {
    const { db, warehouse } = this.context;
    db.transaction(() => {
      warehouse.get(beanId);
      if (this.isQuarantined(beanId)) this.invalidQuarantine(beanId, "quarantine");
      db.query("INSERT INTO quarantines (bean_id, reason, by, at, source) VALUES (?, ?, ?, ?, 'manual')")
        .run(beanId, redact(options.reason), options.by, this.now());
      this.event("scheduler.quarantine", { bean: beanId, by: options.by, source: "manual" });
    }).immediate();
  }

  release(beanId: string, options: { by: string }): void {
    const { db } = this.context;
    db.transaction(() => {
      if (!this.isQuarantined(beanId)) this.invalidQuarantine(beanId, "release");
      db.query("DELETE FROM quarantines WHERE bean_id = ?").run(beanId);
      this.event("scheduler.release", { bean: beanId, by: options.by });
    }).immediate();
  }

  dead(id: string): DeadLetter {
    const row = this.deadRows(id)[0];
    if (!row) throw new FactoryError("DLQ_NOT_FOUND", "找不到死信", { jobId: id });
    return row;
  }

  listDead(): DeadLetter[] { return this.deadRows(); }

  private requireConfig(): SchedulerConfig {
    if (!this.config) this.loadConfig();
    return this.config!;
  }

  private now(): string { return utcIso(this.context.clock.now()); }

  private loadedSnapshots(): Snapshot[] { return [...this.runners.values()].map(({ snapshot }) => snapshot); }

  private refreshAdmission(): void {
    const config = this.requireConfig();
    this.context.db.transaction(() => this.admission.refresh(config, this.loadedSnapshots())).immediate();
  }

  private claimReady(): string[] {
    const config = this.requireConfig();
    const { db } = this.context;
    return db.transaction(() => {
      this.admission.refresh(config, this.loadedSnapshots());
      for (const row of db.query<{ id: string; state: JobState }>(`
        SELECT queue.job_id AS id, job.state FROM schedule_queue AS queue
        LEFT JOIN jobs AS job ON job.id = queue.job_id
        WHERE job.id IS NULL OR job.state IN ('DEAD', 'SEALED')
      `).all()) {
        if (row.state === "DEAD") this.recordDead(this.runner.jobs.get(row.id));
        db.query("DELETE FROM schedule_queue WHERE job_id = ?").run(row.id);
      }
      const active = db.query<Claim>(`
        SELECT job_id AS jobId, station, pid, cpu, memory_mb AS memoryMb, gpu, pool FROM schedule_claims
      `).all();
      const used = { cpu: 0, memoryMb: 0, gpu: 0 };
      const compartmentUse = new Map<string, Resources>();
      const counts = new Map<string, number>();
      const alive = new Map<number, boolean>();
      const isAlive = (pid: number) => {
        if (!alive.has(pid)) alive.set(pid, liveProcess(pid));
        return alive.get(pid)!;
      };
      const account = (station: string, need: Demand) => {
        let local = compartmentUse.get(need.pool);
        if (!local) compartmentUse.set(need.pool, local = { cpu: 0, memoryMb: 0, gpu: 0 });
        for (const [field] of resources) {
          used[field] += need[field];
          local[field] += need[field];
        }
        counts.set(station, (counts.get(station) ?? 0) + 1);
      };
      for (const claim of active) {
        if (isAlive(claim.pid)) account(claim.station, claim);
        else db.query("DELETE FROM schedule_claims WHERE job_id = ?").run(claim.jobId);
      }
      const rows = db.query<QueueEntry>(`
        SELECT jobs.id, jobs.epoch, jobs.station, jobs.inputs, locks.pid, json_extract(jobs.run_value, '$.line') AS line
        FROM schedule_queue AS queue JOIN jobs ON queue.job_id = jobs.id
        LEFT JOIN job_execution_locks AS locks ON locks.job_id = jobs.id
        LEFT JOIN schedule_claims AS claims ON claims.job_id = jobs.id
        LEFT JOIN dead_letters AS dead ON dead.job_id = jobs.id
        WHERE claims.job_id IS NULL AND dead.job_id IS NULL AND NOT (
          jobs.state = 'AWAITING_APPROVAL' AND jobs.phase = 'propose' AND EXISTS (
            SELECT 1 FROM approvals WHERE job_id = jobs.id AND attempt = jobs.attempt AND phase = 'propose' AND status = 'pending'
          )
        )
        ORDER BY jobs.id
      `).all();
      const chosen: string[] = [];
      const quarantine = db.query("SELECT 1 FROM quarantines WHERE bean_id = ?");
      const claim = db.query("INSERT INTO schedule_claims (job_id, station, pid, cpu, memory_mb, gpu, pool) VALUES (?, ?, ?, ?, ?, ?, ?)");
      const compartments = new Map((config.compartments ?? []).map((item) => [item.name, item]));
      const candidates: { row: QueueEntry; need: Demand; snapshot: Snapshot }[] = [];
      for (const row of rows) {
        if (row.pid !== null && isAlive(row.pid)) continue;
        const snapshot = this.runners.get(row.epoch)?.snapshot;
        if (!snapshot) continue;
        let need: Demand;
        try { need = this.demand(row.station, snapshot, row.line ?? undefined); }
        catch (error) {
          if (!(error instanceof FactoryError)) throw error;
          if (!db.query("SELECT 1 FROM events WHERE job_id = ? AND kind = 'scheduler.queued' AND json_extract(data, '$.code') = ?").get(row.id, error.code)) {
            this.event("scheduler.queued", { job: row.id, reason: "capacity", code: error.code }, row.id);
          }
          continue;
        }
        candidates.push({ row, need, snapshot });
      }
      const priority = (pool: string) => compartments.get(pool)?.priority ?? 0;
      candidates.sort((a, b) => priority(b.need.pool) - priority(a.need.pool));
      for (const { row, need, snapshot } of candidates) {
        const inputs = JSON.parse(row.inputs) as Record<string, string[]>;
        const rule = config.wip?.find(({ station }) => {
          const key = snapshot.key("Station", station);
          return key === row.station || key === row.station.split("@", 1)[0];
        });
        if (rule && (counts.get(row.station) ?? 0) >= rule.max) continue;
        if (Object.values(inputs).some((items) => items.some((bean) => quarantine.get(bean)))) continue;
        const compartment = compartments.get(need.pool);
        const local = compartmentUse.get(need.pool);
        if (resources.some(([field, key]) => {
          const capacity = config.pools?.[key];
          const localCapacity = compartment?.pools[key];
          return (capacity != null && used[field] + need[field] > capacity)
            || (localCapacity != null && (local?.[field] ?? 0) + need[field] > localCapacity);
        })) continue;
        if (!this.admission.admit(row.id, row.station, snapshot, config)) continue;
        claim.run(row.id, row.station, process.pid, need.cpu, need.memoryMb, need.gpu, need.pool);
        account(row.station, need);
        this.event("scheduler.admitted", { job: row.id, compartment: need.pool }, row.id);
        chosen.push(row.id);
      }
      return chosen;
    }).immediate();
  }

  private scanDead(): string[] {
    const { db } = this.context;
    return db.transaction(() => {
      const missing = db.query<{ id: string }>(`
        SELECT jobs.id FROM jobs LEFT JOIN dead_letters ON dead_letters.job_id = jobs.id
        WHERE jobs.state = 'DEAD' AND dead_letters.job_id IS NULL ORDER BY jobs.id
      `).all();
      for (const { id } of missing) this.recordDead(this.runner.jobs.get(id));
      return missing.map(({ id }) => id);
    }).immediate();
  }

  private recordDead(job: Job): void {
    const { db } = this.context;
    if (db.query("SELECT 1 FROM dead_letters WHERE job_id = ?").get(job.id)) return;
    const error = redact(job.error ?? {}) as JsonObject;
    const code = typeof error.code === "string" ? error.code : "DEAD";
    db.query("INSERT INTO dead_letters (job_id, station, error_code, error, at) VALUES (?, ?, ?, ?, ?)")
      .run(job.id, job.station, code, canonicalJson(error), job.updatedAt);
    this.event("dlq.recorded", { job: job.id, error_code: code }, job.id);
  }

  private deadRows(id?: string): DeadLetter[] {
    type Row = { job_id: string; station: string; error_code: string | null; error: string | null; at: string };
    const { db } = this.context;
    const query = db.query<Row>(`
      SELECT * FROM (
        SELECT job_id, station, error_code, error, at FROM dead_letters
        UNION ALL
        SELECT jobs.id, jobs.station, NULL, jobs.error, jobs.updated_at
        FROM jobs LEFT JOIN dead_letters ON dead_letters.job_id = jobs.id
        WHERE jobs.state = 'DEAD' AND dead_letters.job_id IS NULL
      ) ${id === undefined ? "" : "WHERE job_id = ?"} ORDER BY job_id
    `);
    const rows = id === undefined ? query.all() : query.all(id);
    return rows.map((row) => {
      const payload: unknown = row.error === null ? {} : JSON.parse(row.error);
      const error = object(payload) ? (row.error_code === null ? redact(payload) as JsonObject : payload) : {};
      const errorCode = row.error_code ?? (typeof error.code === "string" ? error.code : "DEAD");
      return { jobId: row.job_id, station: row.station, errorCode, error, at: row.at };
    });
  }

  private invalidQuarantine(beanId: string, op: string): never {
    throw new FactoryError("QUARANTINE_STATE_INVALID", "Bean 隔离状态不允许此操作", { beanId, op });
  }

  private event(kind: string, data: JsonObject, jobId: string | null = null): void {
    const { db, ids } = this.context;
    db.query("INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, ?, ?)")
      .run(ids.new("ev"), this.now(), kind, jobId, canonicalJson(data));
  }
}

export class ScheduledExecutor {
  constructor(readonly scheduler: Scheduler) {}
  dispatch(jobIds: readonly string[]): void { this.scheduler.enqueue(jobIds); }
  wait(timeoutS: number): Promise<void> { return this.scheduler.waitChange(timeoutS); }
}
