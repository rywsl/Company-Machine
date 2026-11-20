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
type Sightings = Pick<ConversionRequest, "found_at" | "samples" | "blocked_runs">;

function fulfilled(resolve: ReturnType<typeof contractResolver>, request: ConversionRequest): boolean {
  const status = resolve({ from: request.from_type, to: request.to_type, guarantees: request.guarantees, requires: request.requires });
  return status === "direct" || status === "convert";
}

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
      const row = this.pending(...key);
      const at = utcIso(clock.now());
      const sightings = {
        found_at: [origin.foundAt], samples: origin.sample ? [origin.sample] : [], blocked_runs: origin.runId ? [origin.runId] : [],
      };
      if (!row) {
        const id = ids.new("cr");
        db.query(`
          INSERT INTO conversion_requests (id, from_type, guarantees, to_type, requires, found_at, samples, blocked_runs, status, at, updated_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        `).run(id, ...key, canonicalJson(sightings.found_at), canonicalJson(sightings.samples), canonicalJson(sightings.blocked_runs), at, at);
        return this.get(id);
      }
      return this.merge(row, sightings, at);
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
        if (!fulfilled(resolve, request)) return [];
        update.run(changesetId, at, request.id);
        return [request.id];
      });
    }).immediate();
  }

  restoreAfterRollback(changesetId: string, snapshot: Snapshot): void {
    const { db, clock } = this.context;
    db.transaction(() => {
      const resolved = db.query<ConversionRow>(`
        SELECT * FROM conversion_requests WHERE status = 'resolved' AND changeset_id = ? ORDER BY id
      `).all(changesetId);
      if (!resolved.length) return;
      const resolve = contractResolver(snapshot);
      const at = utcIso(clock.now());
      const reopen = db.query("UPDATE conversion_requests SET status = 'open', changeset_id = NULL, updated_at = ? WHERE id = ?");
      for (const row of resolved) {
        const request = decode(row);
        if (fulfilled(resolve, request)) continue;
        const pending = this.pending(row.from_type, row.guarantees, row.to_type, row.requires);
        if (pending) this.merge(pending, request, at);
        else reopen.run(at, row.id);
      }
    }).immediate();
  }

  private pending(from: string, guarantees: string, to: string, requires: string): ConversionRow | null {
    return this.context.db.query<ConversionRow>(`
      SELECT * FROM conversion_requests WHERE from_type = ? AND guarantees = ? AND to_type = ? AND requires = ?
        AND status IN ('open', 'in_progress')
    `).get(from, guarantees, to, requires);
  }

  private merge(row: ConversionRow, extra: Sightings, at: string): ConversionRequest {
    const current = decode(row);
    const locations = new Map(current.found_at.map((location) => [canonicalJson(location), location]));
    for (const location of extra.found_at) locations.set(canonicalJson(location), location);
    const found = canonicalJson([...locations.values()]);
    const samples = canonicalJson([...new Set([...current.samples, ...extra.samples])]);
    const blocked = canonicalJson([...new Set([...current.blocked_runs, ...extra.blocked_runs])]);
    if (found === row.found_at && samples === row.samples && blocked === row.blocked_runs) return current;
    this.context.db.query("UPDATE conversion_requests SET found_at = ?, samples = ?, blocked_runs = ?, updated_at = ? WHERE id = ?")
      .run(found, samples, blocked, at, row.id);
    return this.get(row.id);
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
