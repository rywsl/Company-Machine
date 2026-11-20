import type { Context } from "./context";
import { governanceLimits } from "./change-facts";
import { JobStore } from "./jobs";
import { RunStore } from "./runs";
import { canonicalJson, digestBytes, utcIso } from "./foundation";

interface SafeModeStatus {
  on: boolean;
  level: number;
  since: string | null;
  until: string | null;
}

export class GovernanceSafeMode {
  constructor(private readonly context: Context) {
    context.db.exec(`
      CREATE TABLE IF NOT EXISTS governance_mode (
        id INTEGER PRIMARY KEY CHECK (id=1), level INTEGER NOT NULL, since TEXT, until TEXT, on_flag INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS governance_changeset_events_time ON events(at) WHERE kind='changeset.state';
    `);
  }

  current(): SafeModeStatus {
    const row = this.context.db.query<Omit<SafeModeStatus, "on"> & { on_flag: number }>(
      "SELECT level, since, until, on_flag FROM governance_mode WHERE id=1",
    ).get();
    if (!row) return { on: false, level: 0, since: null, until: null };
    const { on_flag, ...status } = row;
    return { ...status, on: on_flag === 1 };
  }

  evaluate(): SafeModeStatus {
    const { db, clock, ids } = this.context;
    return db.transaction(() => {
      const current = this.current();
      const now = clock.now();
      if (current.on && current.until && Date.parse(current.until) > now.getTime()) return current;
      const limits = governanceLimits();
      const at = utcIso(now);
      const since = utcIso(new Date(now.getTime() - limits.safe_mode_window_s * 1000));
      const rollbacks = db.query<{ count: number }>(`SELECT count(*) AS count FROM events
        WHERE kind='changeset.state' AND at>=? AND at<=?
          AND CASE WHEN json_valid(data) THEN json_extract(data, '$.code') END='SLO_ROLLBACK'`).get(since, at)!.count;
      const auditTable = db.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_findings'").get();
      const audit = auditTable && db.query("SELECT 1 FROM audit_findings WHERE state='wake' LIMIT 1").get();
      const reason = rollbacks >= limits.safe_mode_rollbacks ? "slo-rollbacks" : audit ? "audit-wake" : null;
      if (!current.on && !reason) return current;
      const next = { ...current, on: reason !== null };
      if (reason) {
        if (!current.on) { next.level++; next.since = at; }
        const cooldown = limits.safe_mode_cooldown_s * 1000 * 2 ** (next.level - 1);
        next.until = utcIso(new Date(Math.min(now.getTime() + cooldown, 8.64e15)));
      }
      db.query(`INSERT INTO governance_mode (id, level, since, until, on_flag) VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET level=excluded.level, since=excluded.since, until=excluded.until, on_flag=excluded.on_flag`)
        .run(next.level, next.since, next.until, Number(next.on));
      db.query("INSERT INTO events (id,at,kind,job_id,data) VALUES (?,?,'governance.safe_mode',NULL,?)")
        .run(ids.new("ev"), at, canonicalJson({ ...next, reason }));
      return next;
    }).immediate();
  }
}

interface RolloutMetrics {
  runs: number;
  jobs: number;
  success_rate: number | null;
  gate_reject_rate: number | null;
  cost_per_run_usd: number | null;
}

export function runMetrics(context: Context, epoch: string, since: string): RolloutMetrics {
  const { db, clock, ids } = context;
  new RunStore(db, clock, ids);
  new JobStore(db, clock, ids);
  return db.query<RolloutMetrics>(`
    WITH samples AS (
      SELECT id, state, spent_usd FROM runs
      WHERE epoch=? AND created_at>=? AND state IN ('SUCCEEDED','FAILED','CANCELLED')
        AND sink_mode='execute'
    ), linked_jobs AS (
      SELECT DISTINCT job_id FROM run_node_jobs WHERE run_id IN (SELECT id FROM samples)
    ), rejected_jobs AS (
      SELECT job_id FROM job_events
      WHERE job_id IN (SELECT job_id FROM linked_jobs) AND kind='job.state' AND json_extract(data,'$.to')='REJECTED'
      UNION
      SELECT job_id FROM events
      WHERE job_id IN (SELECT job_id FROM linked_jobs) AND kind='job.state' AND json_extract(data,'$.to')='REJECTED'
    )
    SELECT (SELECT count(*) FROM samples) AS runs,
      (SELECT count(*) FROM linked_jobs) AS jobs,
      (SELECT avg(state='SUCCEEDED') FROM samples) AS success_rate,
      1.0 * (SELECT count(*) FROM rejected_jobs) / (SELECT count(*) FROM linked_jobs) AS gate_reject_rate,
      (SELECT round(avg(spent_usd), 6) FROM samples) AS cost_per_run_usd
  `).get(epoch, since)!;
}

export function sloViolation(metrics: RolloutMetrics, slo: Record<string, unknown>) {
  const { runs: _runs, jobs: _jobs, ...rates } = metrics;
  for (const metric of Object.keys(rates) as Array<keyof typeof rates>) {
    const value = rates[metric];
    const bound = slo[metric];
    if (value === null || typeof bound !== "string") continue;
    const match = /^(>=|<=|>|<)\s*(\d+(?:\.\d+)?)$/.exec(bound);
    if (!match) continue;
    const limit = Number(match[2]);
    const ok = match[1] === ">=" ? value >= limit : match[1] === "<=" ? value <= limit : match[1] === ">" ? value > limit : value < limit;
    if (!ok) return { metric, value, bound };
  }
}

export function canaryBucket(key: string): number {
  return Number.parseInt(digestBytes(new TextEncoder().encode(key)).slice(7, 15), 16) % 100;
}
