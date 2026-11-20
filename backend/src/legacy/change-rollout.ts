import type { Context } from "./context";
import { JobStore } from "./jobs";
import { RunStore } from "./runs";
import { digestBytes } from "./foundation";

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
