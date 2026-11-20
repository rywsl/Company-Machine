import type { Context } from "./context";
import { FactoryError } from "./errors";
import { parseDuration, utcIso } from "./foundation";
import type { Snapshot } from "./snapshot";

export const resources = [["cpu", "cpu"], ["memoryMb", "memory_mb"], ["gpu", "gpu"]] as const;
export type Resources = Record<typeof resources[number][0], number>;
export type Pools = Partial<Record<typeof resources[number][1], number | null>>;
interface Compartment { name: string; match: { line: string }; priority?: number; pools: Pools; }
interface Breaker {
  subject: string;
  min_samples: number;
  failure_rate_gt: number;
  reject_rate_gt?: number;
  open_for: string;
  probe_limit: number;
}
export interface AdmissionConfig {
  compartments?: Compartment[];
  breakers?: Breaker[];
  toxic_failures?: number | null;
}
type ObjectValue = Record<string, unknown>;
type Event = (kind: string, data: ObjectValue, jobId?: string) => void;
type Status = "closed" | "open" | "half_open";
type Circuit = { state: Status; changed_at: string; sample_after: number };
type Outcome = "DEAD" | "REJECTED" | "SEALED";
const outcomeFilter = "jobs.state IN ('SEALED', 'DEAD', 'REJECTED')";

export function nonnegative(value: unknown, integral = false): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && (!integral || Number.isSafeInteger(value));
}

function object(value: unknown): value is ObjectValue {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function invalid(reason: string): never {
  throw new FactoryError("SCHEDULER_CONFIG_INVALID", "调度器配置不合法", { reason });
}

export function parsePools(value: unknown, path: string): Pools {
  if (!object(value)) return invalid(path);
  const result: Pools = {};
  for (const [, key] of resources) {
    const capacity = Object.hasOwn(value, key) ? value[key] : key === "memory_mb" ? value.memory : undefined;
    if (capacity === undefined) continue;
    if (capacity !== null && !nonnegative(capacity, key !== "cpu")) return invalid(path + "." + key);
    result[key] = capacity;
  }
  return result;
}

function positiveInteger(value: unknown): value is number {
  return nonnegative(value, true) && value > 0;
}

function ratio(value: unknown): value is number {
  return nonnegative(value) && value <= 1;
}

function validDuration(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const seconds = parseDuration(value);
    return Number.isFinite(seconds) && seconds > 0;
  }
  catch { return false; }
}

export function parseAdmission(value: ObjectValue, globalPools?: Pools): AdmissionConfig {
  const result: AdmissionConfig = {};
  if (value.compartments != null) {
    if (!Array.isArray(value.compartments)) return invalid("compartments");
    const names = new Set<string>();
    const lines = new Set<string>();
    result.compartments = value.compartments.map((item): Compartment => {
      if (!object(item) || typeof item.name !== "string" || !item.name || item.name === "*" || names.has(item.name)
        || !object(item.match) || Object.keys(item.match).length !== 1 || typeof item.match.line !== "string"
        || !/^Line:[a-z][a-z0-9-]*@[0-9]+$/.test(item.match.line) || lines.has(item.match.line)
        || (item.priority !== undefined && (typeof item.priority !== "number" || !Number.isSafeInteger(item.priority)))) {
        return invalid("compartments 条目需要唯一名称、产线和整数优先级");
      }
      names.add(item.name);
      lines.add(item.match.line);
      return { name: item.name, match: { line: item.match.line }, priority: item.priority as number | undefined,
        pools: parsePools(item.pools, "compartments." + item.name + ".pools") };
    });
    for (const [, key] of resources) {
      const capacity = globalPools?.[key];
      if (capacity == null) continue;
      const total = result.compartments.reduce((sum, item) => sum + (item.pools[key] === null ? Infinity : item.pools[key] ?? 0), 0);
      if (total > capacity) return invalid("隔舱容量总和超出 pools." + key);
    }
  }
  if (value.breakers != null) {
    if (!Array.isArray(value.breakers)) return invalid("breakers");
    const subjects = new Set<string>();
    result.breakers = value.breakers.map((item): Breaker => {
      if (!object(item) || typeof item.subject !== "string"
        || !/^(?:station:Station:[a-z][a-z0-9-]*@[0-9]+|machine:Machine:[a-z][a-z0-9-]*@[0-9]+|provider:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)$/.test(item.subject)
        || subjects.has(item.subject) || !positiveInteger(item.min_samples) || !positiveInteger(item.probe_limit)
        || !ratio(item.failure_rate_gt) || (item.reject_rate_gt !== undefined && !ratio(item.reject_rate_gt))
        || !validDuration(item.open_for)) return invalid("breakers 条目不完整或不合法");
      subjects.add(item.subject);
      return { subject: item.subject, min_samples: item.min_samples, probe_limit: item.probe_limit,
        failure_rate_gt: item.failure_rate_gt, reject_rate_gt: item.reject_rate_gt, open_for: item.open_for };
    });
  }
  if (value.toxic_failures !== undefined) {
    if (value.toxic_failures !== null && !positiveInteger(value.toxic_failures)) return invalid("toxic_failures");
    result.toxic_failures = value.toxic_failures;
  }
  return result;
}

export class AdmissionProtections {
  private readonly subjects = new WeakMap<Snapshot, Map<string, string[]>>();

  constructor(private readonly context: Context, private readonly event: Event) {
    const { db } = context;
    db.exec(`
      CREATE TABLE IF NOT EXISTS breaker_state (
        subject TEXT PRIMARY KEY, state TEXT NOT NULL, changed_at TEXT NOT NULL,
        probes_out INTEGER NOT NULL DEFAULT 0, sample_after INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS scheduler_probes (
        subject TEXT NOT NULL, job_id TEXT NOT NULL, PRIMARY KEY (subject, job_id)
      );
      CREATE TABLE IF NOT EXISTS scheduler_toxic_cursor (
        id INTEGER PRIMARY KEY CHECK (id = 1), seq INTEGER NOT NULL, threshold INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS scheduler_job_outcomes ON jobs(updated_at DESC, id DESC)
        WHERE state IN ('SEALED', 'DEAD', 'REJECTED');
      CREATE INDEX IF NOT EXISTS scheduler_station_outcomes ON jobs(station, updated_at DESC, id DESC)
        WHERE state IN ('SEALED', 'DEAD', 'REJECTED');
    `);
    db.transaction(() => {
      const columns = db.query<{ name: string }>("PRAGMA table_info(breaker_state)").all();
      if (!columns.some(({ name }) => name === "sample_after")) {
        db.exec("ALTER TABLE breaker_state ADD COLUMN sample_after INTEGER NOT NULL DEFAULT 0");
      }
    }).immediate();
  }

  private stationSubjects(snapshot: Snapshot, stationKey: string): string[] {
    let cache = this.subjects.get(snapshot);
    if (!cache) this.subjects.set(snapshot, cache = new Map());
    const known = cache.get(stationKey);
    if (known) return known;
    const station = snapshot.spec(stationKey);
    const policy = snapshot.spec(snapshot.key("Policy", station.policy));
    const result = ["station:" + stationKey, "machine:" + snapshot.key("Machine", station.machine)];
    for (const host of policy.egress?.providers ?? []) if (typeof host === "string") result.push("provider:" + host.toLowerCase());
    cache.set(stationKey, result);
    return result;
  }

  private states(rule: Breaker, snapshots: Snapshot[], after: number): Outcome[] {
    const { db } = this.context;
    const recent = `
      AND (jobs.state <> 'REJECTED' OR NOT EXISTS (SELECT 1 FROM job_execution_locks WHERE job_id = jobs.id))
      AND (? = 0 OR EXISTS (
        SELECT 1 FROM job_events WHERE job_id = jobs.id AND kind = 'job.state' AND rowid > ?
          AND json_extract(data, '$.to') = jobs.state
      )) ORDER BY jobs.updated_at DESC, jobs.id DESC LIMIT ?
    `;
    if (rule.subject.startsWith("station:")) {
      return db.query<{ state: Outcome }>(`
        SELECT jobs.state FROM jobs WHERE jobs.station = ? AND ${outcomeFilter} ${recent}
      `).all(rule.subject.slice("station:".length), after, after, rule.min_samples).map(({ state }) => state);
    }
    const members: [string, string][] = [];
    for (const snapshot of snapshots) {
      for (const station of snapshot.keys("Station")) {
        if (this.stationSubjects(snapshot, station).includes(rule.subject)) members.push([snapshot.epoch, station]);
      }
    }
    return db.query<{ state: Outcome }>(`
      SELECT jobs.state FROM jobs WHERE ${outcomeFilter} AND EXISTS (
        SELECT 1 FROM json_each(?) WHERE json_extract(value, '$[0]') = jobs.epoch AND json_extract(value, '$[1]') = jobs.station
      ) ${recent}
    `).all(JSON.stringify(members), after, after, rule.min_samples).map(({ state }) => state);
  }

  private circuit(subject: string): Circuit | null {
    return this.context.db.query<Circuit>("SELECT state, changed_at, sample_after FROM breaker_state WHERE subject = ?").get(subject);
  }

  private transition(subject: string, from: Status, to: Status, now: string, sampleAfter: number): void {
    const { db } = this.context;
    db.query(`
      INSERT INTO breaker_state (subject, state, changed_at, probes_out, sample_after) VALUES (?, ?, ?, 0, ?)
      ON CONFLICT(subject) DO UPDATE SET state = excluded.state, changed_at = excluded.changed_at,
        probes_out = 0, sample_after = excluded.sample_after
    `).run(subject, to, now, sampleAfter);
    db.query("DELETE FROM scheduler_probes WHERE subject = ?").run(subject);
    this.event("scheduler.circuit", { subject, from, to });
  }

  refresh(config: AdmissionConfig, snapshots: Snapshot[]): void {
    const { db, clock } = this.context;
    const now = utcIso(clock.now());
    for (const rule of config.breakers ?? []) {
      const state = this.circuit(rule.subject);
      if (state?.state === "open") {
        if (Date.parse(now) - Date.parse(state.changed_at) >= parseDuration(rule.open_for) * 1000) {
          this.transition(rule.subject, "open", "half_open", now, state.sample_after);
        }
        continue;
      }
      if (state?.state === "half_open") {
        const probes = db.query<{ state: string }>(`
          SELECT jobs.state FROM scheduler_probes JOIN jobs ON jobs.id = scheduler_probes.job_id
          WHERE subject = ? AND (jobs.state <> 'REJECTED' OR NOT EXISTS (
            SELECT 1 FROM job_execution_locks WHERE job_id = jobs.id
          ))
        `).all(rule.subject);
        if (probes.some(({ state: outcome }) => outcome === "DEAD" || outcome === "REJECTED")) {
          this.transition(rule.subject, "half_open", "open", now, state.sample_after);
        } else if (probes.filter(({ state: outcome }) => outcome === "SEALED").length >= rule.probe_limit) {
          const seq = db.query<{ seq: number }>("SELECT IFNULL(MAX(rowid), 0) AS seq FROM job_events").get()!.seq;
          this.transition(rule.subject, "half_open", "closed", now, seq);
        }
        continue;
      }
      const sample = this.states(rule, snapshots, state?.sample_after ?? 0);
      if (sample.length !== rule.min_samples) continue;
      const failed = sample.filter((outcome) => outcome === "DEAD").length / sample.length;
      const rejected = sample.filter((outcome) => outcome === "REJECTED").length / sample.length;
      if (failed > rule.failure_rate_gt || (rule.reject_rate_gt !== undefined && rejected > rule.reject_rate_gt)) {
        this.transition(rule.subject, "closed", "open", now, state?.sample_after ?? 0);
      }
    }
    if (config.toxic_failures != null) this.quarantineToxic(config.toxic_failures, now);
  }

  admit(jobId: string, station: string, snapshot: Snapshot, config: AdmissionConfig): boolean {
    const { db } = this.context;
    if (!config.breakers?.length) return true;
    const subjects = this.stationSubjects(snapshot, station);
    const pending: string[] = [];
    for (const rule of config.breakers) {
      if (!subjects.includes(rule.subject)) continue;
      const row = db.query<{ state: Status; probes_out: number }>("SELECT state, probes_out FROM breaker_state WHERE subject = ?").get(rule.subject);
      if (row?.state === "open") return false;
      if (row?.state !== "half_open") continue;
      if (db.query("SELECT 1 FROM scheduler_probes WHERE subject = ? AND job_id = ?").get(rule.subject, jobId)) continue;
      if (row.probes_out >= rule.probe_limit) return false;
      pending.push(rule.subject);
    }
    for (const subject of pending) {
      db.query("INSERT INTO scheduler_probes (subject, job_id) VALUES (?, ?)").run(subject, jobId);
      db.query("UPDATE breaker_state SET probes_out = probes_out + 1 WHERE subject = ?").run(subject);
    }
    return true;
  }

  private quarantineToxic(threshold: number, now: string): void {
    const { db } = this.context;
    const cursor = db.query<{ threshold: number; seq: number }>("SELECT threshold, seq FROM scheduler_toxic_cursor WHERE id = 1").get();
    const previous = cursor?.threshold === threshold ? cursor.seq : 0;
    const last = db.query<{ seq: number }>("SELECT IFNULL(MAX(rowid), 0) AS seq FROM job_events").get()!.seq;
    if (last === previous) return;
    // A REJECTED Job can retry during the same invocation. Revisit its outcome after release.
    const locked = db.query<{ seq: number | null }>(`
      SELECT MIN(event.rowid) AS seq FROM job_events AS event
      JOIN jobs ON jobs.id = event.job_id JOIN job_execution_locks AS locks ON locks.job_id = jobs.id
      WHERE event.rowid > ? AND event.kind = 'job.state'
        AND json_extract(event.data, '$.to') = 'REJECTED' AND jobs.state = 'REJECTED'
    `).get(previous)?.seq;
    const seq = locked == null ? last : locked - 1;
    if (seq === previous) return;
    const affected = db.query<{ bean: string }>(`
      SELECT DISTINCT item.value AS bean FROM job_events
      JOIN jobs ON jobs.id = job_events.job_id
      JOIN json_each(jobs.inputs) AS port
      JOIN json_each(port.value) AS item
      WHERE job_events.rowid > ? AND job_events.rowid <= ? AND job_events.kind = 'job.state'
        AND json_extract(job_events.data, '$.to') IN ('DEAD', 'REJECTED')
        AND jobs.state IN ('DEAD', 'REJECTED')
        AND NOT EXISTS (SELECT 1 FROM quarantines WHERE bean_id = item.value)
    `).all(previous, seq);
    for (const { bean } of affected) {
      const sample = db.query<{ state: Outcome }>(`
        SELECT jobs.state FROM jobs WHERE ${outcomeFilter}
        AND (jobs.state <> 'REJECTED' OR NOT EXISTS (SELECT 1 FROM job_execution_locks WHERE job_id = jobs.id))
        AND EXISTS (
          SELECT 1 FROM json_each(jobs.inputs) AS port JOIN json_each(port.value) AS item WHERE item.value = ?
        ) ORDER BY jobs.updated_at DESC, jobs.id DESC LIMIT ?
      `).all(bean, threshold);
      if (sample.length !== threshold || sample.some(({ state }) => state === "SEALED")) continue;
      db.query("INSERT INTO quarantines (bean_id, reason, by, at, source) VALUES (?, ?, 'scheduler', ?, 'auto')")
        .run(bean, "repeated terminal failures", now);
      this.event("scheduler.quarantine", { bean, by: "scheduler", source: "auto" });
    }
    db.query(`
      INSERT INTO scheduler_toxic_cursor (id, seq, threshold) VALUES (1, ?, ?)
      ON CONFLICT(id) DO UPDATE SET seq = excluded.seq, threshold = excluded.threshold
    `).run(seq, threshold);
  }
}
