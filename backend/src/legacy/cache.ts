import type { Context } from "./context";
import { FactoryError } from "./errors";
import { canonicalBytes, canonicalJson, digestBytes, utcIso } from "./foundation";
import { JobStore, type Job } from "./jobs";
import type { Snapshot } from "./snapshot";
import { paramsValue } from "./values";

type JsonObject = Record<string, unknown>;
type JobInputs = Record<string, string | readonly string[]>;
export type InvalidationScope = "key" | "job" | "station" | "bean";
export const cacheableRoles: ReadonlySet<string> = new Set(["transform", "converter"]);

interface Material {
  v: 1;
  station: string;
  station_digest: string;
  machine: string;
  machine_digest: string;
  image: string;
  recipe: string;
  runtime: string;
  runtime_digest: string;
  model: string;
  policy: string;
  policy_digest: string;
  inputs: Record<string, string[]>;
  params: JsonObject;
  variant: string;
}

export function specDigest(document: JsonObject): string {
  const metadata = { ...document.metadata as JsonObject | undefined };
  delete metadata.lifecycle;
  delete metadata.description;
  delete metadata.labels;
  return digestBytes(canonicalBytes({ ...document, metadata }));
}

export class KeyMaterial {
  private readonly value: Material;
  private readonly hash: string;

  constructor(material: Material) {
    this.value = structuredClone(material);
    this.hash = "jk:" + digestBytes(canonicalBytes(this.value)).slice("sha256:".length);
  }

  get variant(): string { return this.value.variant; }
  key(): string { return this.hash; }
  toJSON(): Material { return structuredClone(this.value); }
}

type CacheRow = {
  seq: number; job_id: string; job_key: string; variant: string; material: string; created_at: string;
};

export interface CacheEntry {
  seq: number;
  jobId: string;
  key: string;
  variant: string;
  material: Material;
  createdAt: string;
}

const LOOKUP = `
  SELECT cache.job_id
  FROM job_keys AS cache
  JOIN jobs AS job ON job.id = cache.job_id
  WHERE cache.job_key = ? AND job.state = 'SEALED'
    AND NOT EXISTS (
      SELECT 1 FROM cache_invalidations AS invalidation
      WHERE scope = 'key' AND value = cache.job_key AND max_seq >= cache.seq
    )
    AND NOT EXISTS (
      SELECT 1 FROM cache_invalidations AS invalidation
      WHERE scope = 'job' AND value = cache.job_id AND max_seq >= cache.seq
    )
    AND NOT EXISTS (
      SELECT 1 FROM cache_invalidations AS invalidation
      WHERE scope = 'station' AND value = job.station AND max_seq >= cache.seq
    )
    AND NOT EXISTS (
      SELECT 1
      FROM json_each(job.inputs) AS port, json_each(port.value) AS bean
      JOIN cache_invalidations AS invalidation
        ON scope = 'bean' AND invalidation.value = bean.value AND max_seq >= cache.seq
    )
    AND NOT EXISTS (
      SELECT 1
      FROM json_each(job.outputs) AS port, json_each(port.value) AS output
      LEFT JOIN beans AS bean ON bean.id = output.value
      WHERE bean.id IS NULL OR bean.sealed = 0 OR bean.expired_at IS NOT NULL
        OR EXISTS (
          SELECT 1 FROM cache_invalidations AS invalidation
          WHERE scope = 'bean' AND value = output.value AND max_seq >= cache.seq
        )
        /* quarantine */
    )
  ORDER BY cache.seq
  LIMIT 1
`;

export class JobCache {
  readonly jobs: JobStore;

  constructor(readonly context: Pick<Context, "db" | "clock" | "ids">, jobs?: JobStore) {
    const { db } = context;
    this.jobs = jobs ?? new JobStore(db, context.clock, context.ids);
    db.exec(`
      CREATE TABLE IF NOT EXISTS job_keys (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL UNIQUE, job_key TEXT NOT NULL,
        variant TEXT NOT NULL DEFAULT '', material TEXT NOT NULL, created_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS job_keys_key ON job_keys(job_key);
      CREATE TABLE IF NOT EXISTS cache_invalidations (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL CHECK (scope IN ('key', 'job', 'station', 'bean')),
        value TEXT NOT NULL, max_seq INTEGER NOT NULL, reason TEXT NOT NULL, by TEXT NOT NULL, at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS cache_invalidation_scope ON cache_invalidations(scope, value, max_seq);
      CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, at TEXT NOT NULL,
        kind TEXT NOT NULL, job_id TEXT, data TEXT NOT NULL, run_id TEXT
      );
    `);
  }

  material(snapshot: Snapshot, stationRef: string, inputs: JobInputs, params: JsonObject, options: { variant?: string } = {}): KeyMaterial {
    const stationKey = snapshot.key("Station", stationRef);
    const stationDoc = snapshot.doc(stationKey);
    const station = stationDoc.spec;
    if (!cacheableRoles.has(station.role)) {
      throw new FactoryError("CACHE_NOT_CACHEABLE", "工位角色不能缓存", { station: stationKey, role: station.role });
    }
    const machineKey = snapshot.key("Machine", station.machine);
    const machine = snapshot.doc(machineKey);
    const runtimeKey = snapshot.key("OperatorRuntime", station.operator.runtime);
    const policyKey = snapshot.key("Policy", station.policy);
    return new KeyMaterial({
      v: 1,
      station: stationKey, station_digest: specDigest(stationDoc),
      machine: machineKey, machine_digest: specDigest(machine),
      image: machine.spec.runtime?.image ?? "", recipe: station.recipe ?? "",
      runtime: runtimeKey, runtime_digest: specDigest(snapshot.doc(runtimeKey)),
      model: station.operator.model ?? "",
      policy: policyKey, policy_digest: specDigest(snapshot.doc(policyKey)),
      inputs: Object.fromEntries(Object.keys(inputs).sort().map((port) => {
        const ids = inputs[port];
        return [port, typeof ids === "string" ? [ids] : [...ids]];
      })),
      params: paramsValue(snapshot, stationKey, params),
      variant: options.variant ?? "",
    });
  }

  key(snapshot: Snapshot, stationRef: string, inputs: JobInputs, params: JsonObject, options: { variant?: string } = {}): string {
    return this.material(snapshot, stationRef, inputs, params, options).key();
  }

  bind(jobId: string, material: KeyMaterial): void {
    const { db } = this.context;
    db.transaction(() => {
      this.jobs.get(jobId);
      if (db.query("SELECT 1 FROM job_keys WHERE job_id = ?").get(jobId)) {
        throw new FactoryError("CACHE_BIND_CONFLICT", "Job 已绑定缓存键", { jobId });
      }
      db.query("INSERT INTO job_keys (job_id, job_key, variant, material, created_at) VALUES (?, ?, ?, ?, ?)")
        .run(jobId, material.key(), material.variant, canonicalJson(material.toJSON()), utcIso(this.context.clock.now()));
    }).immediate();
  }

  lookup(key: string): Job | null {
    const { db } = this.context;
    return db.transaction(() => {
      const quarantine = db.query("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'quarantines'").get();
      const sql = quarantine ? LOOKUP.replace(
        "/* quarantine */", "OR EXISTS (SELECT 1 FROM quarantines WHERE bean_id = output.value)",
      ) : LOOKUP;
      const row = db.query<{ job_id: string }>(sql).get(key);
      return row ? this.jobs.get(row.job_id) : null;
    })();
  }

  invalidate(scope: InvalidationScope, value: string, options: { reason: string; by: string }): string {
    if (!["key", "job", "station", "bean"].includes(scope) ||
        (scope === "station" && !value.startsWith("Station:")) ||
        (scope === "bean" && !value.startsWith("sha256:"))) {
      throw new FactoryError("CACHE_INVALIDATION_INVALID", "失效范围或值不合法", { scope, value });
    }
    const { db, ids, clock } = this.context;
    return db.transaction(() => {
      const id = ids.new("ci");
      const at = utcIso(clock.now());
      const record = { id, scope, value, reason: options.reason, by: options.by };
      db.query(`
        INSERT INTO cache_invalidations (id, scope, value, max_seq, reason, by, at)
        VALUES (?, ?, ?, (SELECT COALESCE(MAX(seq), 0) FROM job_keys), ?, ?, ?)
      `).run(id, scope, value, options.reason, options.by, at);
      db.query("INSERT INTO events (id, at, kind, data) VALUES (?, ?, 'cache.invalidated', ?)")
        .run(ids.new("ev"), at, canonicalJson(record));
      return id;
    }).immediate();
  }

  explain(jobId: string): { key: string; material: Material; hits: string[] } {
    const { db } = this.context;
    return db.transaction(() => {
      const row = db.query<CacheRow>("SELECT * FROM job_keys WHERE job_id = ?").get(jobId);
      if (!row) throw new FactoryError("CACHE_NOT_CACHEABLE", "Job 没有缓存键", { jobId });
      const hits = db.query<{ job_id: string }>("SELECT job_id FROM job_keys WHERE job_key = ? ORDER BY seq")
        .all(row.job_key).map((item) => item.job_id);
      return { key: row.job_key, material: JSON.parse(row.material), hits };
    })();
  }

  list(station?: string): CacheEntry[] {
    const { db } = this.context;
    const entries = station === undefined
      ? db.query<CacheRow>("SELECT * FROM job_keys ORDER BY seq").all()
      : db.query<CacheRow>(`
          SELECT job_keys.* FROM job_keys JOIN jobs ON jobs.id = job_keys.job_id
          WHERE jobs.station = ? ORDER BY job_keys.seq
        `).all(station.startsWith("Station:") ? station : "Station:" + station);
    return entries.map((row) => ({
      seq: row.seq, jobId: row.job_id, key: row.job_key, variant: row.variant,
      material: JSON.parse(row.material), createdAt: row.created_at,
    }));
  }
}
