import { existsSync } from "node:fs";
import { join } from "node:path";
import { ManagedDatabase } from "../sqlite";
import { FactoryError } from "./errors";

export type EventScope = { job: string } | { run: string };
export type EventQuery = { since?: string; kind?: string; limit?: number; scope?: EventScope };
type EventRow = { id: string; kind: string; at: string; job_id: string | null; data: string };
export type FactoryEvent = Omit<EventRow, "data"> & { data: unknown };

const secretKey = /api[_-]?key|secret|token|password|authorization|cookie/i;
const secretValue = /\b(api[_-]?key|secret|token|password|authorization|cookie)(\s*[:=]\s*)(?:bearer\s+)?(?:"[^"]*"|'[^']*'|\S+)/gi;

function redact(value: unknown): unknown {
  if (typeof value === "string") return value.replace(secretValue, "$1$2[REDACTED]");
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, secretKey.test(key) ? "[REDACTED]" : redact(item)]));
  }
  return value;
}

function isoLowerBound(since: string): string {
  const invalid = () => new FactoryError("EVENTS_QUERY_INVALID", "since 不是 ISO 时间", { field: "since" });
  const match = /^(\d{4}-\d{2}-\d{2})(?:[T ](?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d+)?)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?)?$/.exec(since);
  if (!match) throw invalid();
  const date = new Date(match[1]);
  if (!Number.isFinite(date.getTime()) || !date.toISOString().startsWith(match[1] + "T")) throw invalid();
  const instant = new Date(since.length === 10 || /Z$|[+-]\d{2}:\d{2}$/.test(since) ? since : since + "Z");
  if (!Number.isFinite(instant.getTime())) throw invalid();
  return instant.toISOString();
}

export function tailEvents(root: string, { since, kind, limit = 100, scope }: EventQuery = {}): FactoryEvent[] {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 10_000) {
    throw new FactoryError("EVENTS_QUERY_INVALID", "limit 超出范围", { field: "limit" });
  }
  const conditions: string[] = [];
  const parameters: (string | number)[] = [];
  if (since !== undefined) {
    conditions.push("julianday(at) >= julianday(?)");
    parameters.push(isoLowerBound(since));
  }
  if (kind !== undefined) {
    conditions.push("substr(kind, 1, length(?)) = ?");
    parameters.push(kind, kind);
  }
  const job = scope && "job" in scope ? scope.job : undefined;
  const run = scope && "run" in scope ? scope.run : undefined;
  if (job !== undefined) {
    conditions.push("job_id = ?");
    parameters.push(job);
  }
  if (run !== undefined) {
    conditions.push("run_id = ?");
    parameters.push(run);
  }

  const path = join(root, "factory.db");
  if (!existsSync(path)) return [];
  const table = job !== undefined ? "job_events" : "events";
  const order = job !== undefined ? "rowid" : "seq";
  const db = new ManagedDatabase(path, { readonly: true });
  try {
    return db.transaction(() => {
      if (!db.query("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?").get(table)) return [];
      if (run !== undefined && !db.query<{ name: string }>("PRAGMA table_info(events)").all().some(({ name }) => name === "run_id")) return [];
      const where = conditions.length ? " WHERE " + conditions.join(" AND ") : "";
      const rows = db.query<EventRow>("SELECT id, kind, at, job_id, data FROM " + table + where + " ORDER BY " + order + " DESC LIMIT ?")
        .all(...parameters, limit);
      return rows.reverse().map((row) => ({ ...row, data: redact(JSON.parse(row.data)) }));
    })();
  } finally { db.close(); }
}
