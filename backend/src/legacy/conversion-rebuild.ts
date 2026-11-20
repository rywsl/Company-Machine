import type { Context } from "./context";
import { ConversionStore } from "./conversion-requests";
import { FactoryError } from "./errors";
import { canonicalJson, utcIso } from "./foundation";
import { rebuildLineRun } from "./line-runner";
import { RunStore } from "./runs";
import type { Snapshot } from "./snapshot";

export function hasLateBlockedRuns(context: Context, epoch: string, changesetId: string): boolean {
  const { db } = context;
  const tables = db.query<{ count: number }>(`SELECT count(*) AS count FROM sqlite_schema
    WHERE type = 'table' AND name IN ('conversion_requests', 'runs')`).get();
  if (tables?.count !== 2) return false;
  ensureRecoveryTables(context);
  return Boolean(db.query(`
    SELECT 1 FROM conversion_requests AS request JOIN json_each(request.blocked_runs) AS blocked
    JOIN runs AS run ON run.id = blocked.value
    WHERE run.state = 'FAILED' AND (request.status = 'resolved' AND request.changeset_id = ? AND NOT EXISTS (
        SELECT 1 FROM conversion_requests AS outstanding JOIN json_each(outstanding.blocked_runs) AS linked
        WHERE outstanding.status <> 'resolved' AND linked.value = run.id
      ) OR
      request.status <> 'resolved' AND run.epoch <> ? AND NOT EXISTS (
        SELECT 1 FROM conversion_rebuild_checks WHERE request_id = request.id AND epoch = ?
      ))
      AND NOT EXISTS (SELECT 1 FROM conversion_rebuilds WHERE run_id = run.id AND epoch = ?)
    LIMIT 1
  `).get(changesetId, epoch, epoch, epoch));
}

export function rebuildResolvedRuns(context: Context, snapshot: Snapshot, changesetId: string): void {
  const { db, clock, ids } = context;
  db.transaction(() => {
    const runs = new RunStore(db, clock, ids);
    ensureRecoveryTables(context);
    const late = db.query<{ id: string }>(`
      SELECT DISTINCT request.id FROM conversion_requests AS request
      JOIN json_each(request.blocked_runs) AS blocked JOIN runs AS run ON run.id = blocked.value
      WHERE request.status <> 'resolved' AND run.state = 'FAILED' AND run.epoch <> ?
        AND NOT EXISTS (SELECT 1 FROM conversion_rebuild_checks WHERE request_id = request.id AND epoch = ?)
    `).all(snapshot.epoch, snapshot.epoch);
    if (late.length) {
      new ConversionStore(context).resolveMatching(changesetId, snapshot, new Set(late.map(({ id }) => id)));
      const check = db.query("INSERT INTO conversion_rebuild_checks (request_id, epoch) VALUES (?, ?)");
      for (const { id } of late) check.run(id, snapshot.epoch);
    }
    const blocked = db.query<{ id: string }>(`
      SELECT DISTINCT run.id FROM conversion_requests AS request
      JOIN json_each(request.blocked_runs) AS blocked
      JOIN runs AS run ON run.id = blocked.value
      WHERE request.status = 'resolved' AND request.changeset_id = ? AND run.state = 'FAILED'
        AND NOT EXISTS (SELECT 1 FROM conversion_rebuilds WHERE run_id = run.id AND epoch = ?)
        AND NOT EXISTS (
          SELECT 1 FROM conversion_requests AS outstanding JOIN json_each(outstanding.blocked_runs) AS linked
          WHERE outstanding.status <> 'resolved' AND linked.value = run.id
        )
      ORDER BY run.id
    `).all(changesetId, snapshot.epoch);
    if (!blocked.length) return;
    const record = db.query("INSERT INTO conversion_rebuilds (run_id, epoch, changeset_id, replacement, error) VALUES (?, ?, ?, ?, ?)");
    const event = db.query("INSERT INTO events (id, at, kind, data, run_id) VALUES (?, ?, ?, ?, ?)");
    for (const { id } of blocked) {
      try {
        const rebuilt = rebuildLineRun(context, snapshot, runs, id, { on: "conversion", changeset: changesetId });
        record.run(id, snapshot.epoch, changesetId, rebuilt.id, null);
        event.run(ids.new("ev"), utcIso(clock.now()), "conversion.rebuilt", canonicalJson({ replacement: rebuilt.id, epoch: snapshot.epoch, changeset: changesetId }), id);
      } catch (error) {
        if (!(error instanceof FactoryError)) throw error;
        const failure = { code: error.code, message: error.message, detail: error.detail };
        record.run(id, snapshot.epoch, changesetId, null, canonicalJson(failure));
        event.run(ids.new("ev"), utcIso(clock.now()), "conversion.rebuild_failed", canonicalJson({ epoch: snapshot.epoch, changeset: changesetId, error: failure }), id);
      }
    }
  }).immediate();
}

function ensureRecoveryTables({ db }: Context): void {
  db.exec(`
    CREATE TABLE IF NOT EXISTS conversion_rebuilds (
      run_id TEXT NOT NULL, epoch TEXT NOT NULL, changeset_id TEXT NOT NULL,
      replacement TEXT, error TEXT, PRIMARY KEY (run_id, epoch)
    );
    CREATE TABLE IF NOT EXISTS conversion_rebuild_checks (
      request_id TEXT NOT NULL, epoch TEXT NOT NULL, PRIMARY KEY (request_id, epoch)
    );
  `);
}
