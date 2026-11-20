import type { Context } from "./context";
import { FactoryError } from "./errors";
import { canonicalJson, utcIso } from "./foundation";
import { contractResolver } from "./runplan";
import type { Snapshot } from "./snapshot";

export type ConversionState = "open" | "in_progress" | "resolved";
export interface ConversionContract { from: string; guarantees: readonly string[]; to: string; requires: readonly string[] }

interface ConversionRow {
  id: string;
  from_type: string;
  guarantees: string;
  to_type: string;
  requires: string;
  found_at: string;
  samples: string;
  blocked_runs: string;
  status: ConversionState;
  changeset_id: string | null;
  at: string;
  updated_at: string;
}

function decode(row: ConversionRow) {
  return {
    ...row,
    guarantees: JSON.parse(row.guarantees) as string[],
    requires: JSON.parse(row.requires) as string[],
    found_at: JSON.parse(row.found_at) as Record<string, unknown>[],
    samples: JSON.parse(row.samples) as string[],
    blocked_runs: JSON.parse(row.blocked_runs) as string[],
  };
}

type ConversionRequest = ReturnType<typeof decode>;

export class ConversionStore {
  constructor(private readonly context: Pick<Context, "db" | "clock" | "ids">) {
    context.db.exec(`
      CREATE TABLE IF NOT EXISTS conversion_requests (
        id TEXT PRIMARY KEY, from_type TEXT NOT NULL, guarantees TEXT NOT NULL, to_type TEXT NOT NULL,
        requires TEXT NOT NULL, found_at TEXT NOT NULL, samples TEXT NOT NULL, blocked_runs TEXT NOT NULL,
        status TEXT NOT NULL, changeset_id TEXT, at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
      CREATE UNIQUE INDEX IF NOT EXISTS conversion_key ON conversion_requests(from_type, guarantees, to_type, requires)
        WHERE status IN ('open', 'in_progress');
    `);
  }

  get(id: string): ConversionRequest {
    const row = this.context.db.query<ConversionRow>("SELECT * FROM conversion_requests WHERE id = ?").get(id);
    if (!row) throw new FactoryError("CONVERSION_NOT_FOUND", "找不到转换需求", { id });
    return decode(row);
  }

  list(status?: ConversionState): ConversionRequest[] {
    const { db } = this.context;
    const rows = status === undefined
      ? db.query<ConversionRow>("SELECT * FROM conversion_requests ORDER BY id").all()
      : db.query<ConversionRow>("SELECT * FROM conversion_requests WHERE status = ? ORDER BY id").all(status);
    return rows.map(decode);
  }

  openOrMerge(contract: ConversionContract, origin: { foundAt: Record<string, unknown>; sample?: string; runId?: string }): ConversionRequest {
    const { db, clock, ids } = this.context;
    const properties = (values: readonly string[]) => canonicalJson([...new Set(values)].sort());
    const key = [contract.from, properties(contract.guarantees), contract.to, properties(contract.requires)] as const;
    return db.transaction(() => {
      const row = db.query<ConversionRow>(`
        SELECT * FROM conversion_requests WHERE from_type = ? AND guarantees = ? AND to_type = ? AND requires = ?
          AND status IN ('open', 'in_progress')
      `).get(...key);
      const at = utcIso(clock.now());
      if (!row) {
        const id = ids.new("cr");
        db.query(`
          INSERT INTO conversion_requests (id, from_type, guarantees, to_type, requires, found_at, samples, blocked_runs, status, at, updated_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        `).run(id, ...key, canonicalJson([origin.foundAt]), canonicalJson(origin.sample ? [origin.sample] : []), canonicalJson(origin.runId ? [origin.runId] : []), at, at);
        return this.get(id);
      }
      const current = decode(row);
      const location = canonicalJson(origin.foundAt);
      const found = current.found_at.some((item) => canonicalJson(item) === location) ? row.found_at : canonicalJson([...current.found_at, origin.foundAt]);
      const samples = origin.sample && !current.samples.includes(origin.sample) ? canonicalJson([...current.samples, origin.sample]) : row.samples;
      const blocked = origin.runId && !current.blocked_runs.includes(origin.runId) ? canonicalJson([...current.blocked_runs, origin.runId]) : row.blocked_runs;
      if (found === row.found_at && samples === row.samples && blocked === row.blocked_runs) return current;
      db.query("UPDATE conversion_requests SET found_at = ?, samples = ?, blocked_runs = ?, updated_at = ? WHERE id = ?")
        .run(found, samples, blocked, at, row.id);
      return this.get(row.id);
    }).immediate();
  }

  mark(id: string, status: ConversionState, changesetId?: string): ConversionRequest {
    const { db, clock } = this.context;
    return db.transaction(() => {
      const current = this.get(id);
      if (current.status === "resolved" || (status !== "open" && (status !== "in_progress" || current.status !== "open"))) {
        throw new FactoryError("CONVERSION_STATE_INVALID", "转换需求状态不能直接变更", { id, current: current.status, requested: status });
      }
      db.query("UPDATE conversion_requests SET status = ?, changeset_id = ?, updated_at = ? WHERE id = ?")
        .run(status, status === "in_progress" ? changesetId ?? null : null, utcIso(clock.now()), id);
      return this.get(id);
    }).immediate();
  }

  resolveMatching(changesetId: string, snapshot: Snapshot): string[] {
    const { db, clock } = this.context;
    return db.transaction(() => {
      const open = this.list().filter(({ status }) => status !== "resolved");
      if (!open.length) return [];
      const resolve = contractResolver(snapshot);
      const at = utcIso(clock.now());
      const update = db.query("UPDATE conversion_requests SET status = 'resolved', changeset_id = ?, updated_at = ? WHERE id = ?");
      return open.flatMap((request) => {
        const status = resolve({ from: request.from_type, to: request.to_type, guarantees: request.guarantees, requires: request.requires });
        if (status !== "direct" && status !== "convert") return [];
        update.run(changesetId, at, request.id);
        return [request.id];
      });
    }).immediate();
  }
}

export function conversionDocument(request: ConversionRequest) {
  return {
    apiVersion: "mao/v1", kind: "ConversionRequest", metadata: { name: request.id },
    spec: {
      from: { type: request.from_type, guarantees: request.guarantees },
      to: { type: request.to_type, requires: request.requires },
      found_at: request.found_at, samples: request.samples, blocked_runs: request.blocked_runs, status: request.status,
    },
  };
}
