import type { Context } from "./context";
import { FactoryError } from "./errors";
import { canonicalJson, parseDuration, utcIso } from "./foundation";
import type { Run, RunStore } from "./runs";
import type { Snapshot } from "./snapshot";

type JsonObject = Record<string, unknown>;
type Trigger = { on: "manual" | "cron" | "bean"; type?: string; label?: string; schedule?: string };
type Line = { key: string; rules: Trigger[]; inputs: Record<string, { type: string }> };
type LineStarter = { start(line: string, options: Parameters<RunStore["create"]>[1]): Run };
type SealedBean = { id: string; type: string };
type BeanRule = { line: string; type: string; label?: string; port?: [string, string] };

const inputType = (value: string) => value.replace(/^list<(.+)>$/, "$1").replace(/(@\d+)\.\d+$/, "$1");
const CRON_SCHEDULE = /^every:(\d+(?:ms|s|m|h|d))$/;
const PENDING_BATCH_SIZE = 256;

export class TriggerEngine {
  constructor(
    readonly context: Pick<Context, "db" | "clock" | "ids">,
    readonly snapshot: Snapshot,
    readonly lines: LineStarter,
  ) {
    this.context.db.exec(`
      CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, at TEXT NOT NULL,
        kind TEXT NOT NULL, job_id TEXT, data TEXT NOT NULL, run_id TEXT
      );
      CREATE INDEX IF NOT EXISTS trigger_events_kind ON events(kind, seq);
      CREATE TABLE IF NOT EXISTS trigger_fires (
        line TEXT NOT NULL, bean_id TEXT NOT NULL, run_id TEXT NOT NULL, at TEXT NOT NULL,
        PRIMARY KEY (line, bean_id)
      );
      CREATE TABLE IF NOT EXISTS cron_fires (line TEXT PRIMARY KEY, last_at TEXT NOT NULL, run_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS cursors (name TEXT PRIMARY KEY, seq INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS trigger_epoch (id INTEGER PRIMARY KEY CHECK (id = 1), epoch TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS trigger_pending (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, bean_id TEXT NOT NULL UNIQUE
      );
      CREATE TRIGGER IF NOT EXISTS enqueue_trigger_on_seal
        AFTER UPDATE OF sealed ON beans WHEN NEW.sealed = 1 AND OLD.sealed <> 1 BEGIN
          INSERT INTO trigger_pending (bean_id) VALUES (NEW.id) ON CONFLICT (bean_id) DO NOTHING;
      END;
      CREATE TRIGGER IF NOT EXISTS enqueue_trigger_on_label_insert
        AFTER INSERT ON bean_labels BEGIN
          INSERT INTO trigger_pending (bean_id) VALUES (NEW.bean_id) ON CONFLICT (bean_id) DO NOTHING;
      END;
      CREATE TRIGGER IF NOT EXISTS enqueue_trigger_on_label_change
        AFTER UPDATE OF value ON bean_labels WHEN NEW.value <> OLD.value BEGIN
          INSERT INTO trigger_pending (bean_id) VALUES (NEW.bean_id) ON CONFLICT (bean_id) DO NOTHING;
      END;
    `);
    const eventsColumns = this.context.db.query<{ name: string }>("PRAGMA table_info(events)").all();
    if (!eventsColumns.some(({ name }) => name === "run_id")) this.context.db.exec("ALTER TABLE events ADD COLUMN run_id TEXT");
    if (this.hasJobEvents()) this.context.db.exec("CREATE INDEX IF NOT EXISTS trigger_job_events_kind ON job_events(kind)");
  }

  process(options: { limit?: number } = {}): string[] {
    const limit = options.limit ?? Infinity;
    if (limit <= 0) return [];
    const rules = this.beanRules();
    if (!rules.length) return [];
    const { db } = this.context;
    return db.transaction(() => {
      this.enqueueEvents("events", "trigger.bean", "seq");
      if (this.hasJobEvents()) this.enqueueEvents("job_events", "trigger.bean.job-events", "rowid");
      this.enqueueExistingBeans();
      const pending = db.query<SealedBean | { id: string; type: null }>(`
        SELECT pending.bean_id AS id, bean.type FROM trigger_pending AS pending
        LEFT JOIN beans AS bean ON bean.id = pending.bean_id AND bean.sealed = 1 AND bean.expired_at IS NULL
        ORDER BY pending.seq
        LIMIT ?
      `);
      const remove = db.query("DELETE FROM trigger_pending WHERE bean_id = ?");
      const readLabels = db.query<{ key: string; value: string }>("SELECT key, value FROM bean_labels WHERE bean_id = ?");
      const hasLabelRules = rules.some((rule) => rule.label !== undefined);
      const created: string[] = [];
      while (true) {
        const rows = pending.all(PENDING_BATCH_SIZE);
        for (const bean of rows) {
          if (bean.type !== null) {
            const labels = new Map(hasLabelRules ? readLabels.all(bean.id).map((row) => [row.key, row.value]) : []);
            for (const rule of rules) {
              if (created.length >= limit) return created.sort();
              const started = this.fireBean(rule, { id: bean.id, type: bean.type }, labels);
              if (started) created.push(started);
            }
          }
          remove.run(bean.id);
        }
        if (rows.length < PENDING_BATCH_SIZE) return created.sort();
      }
    }).immediate();
  }

  processCron(options: { enabled?: boolean; limit?: number } = {}): string[] {
    const limit = options.limit ?? Infinity;
    if (limit <= 0) return [];
    const { db, clock } = this.context;
    return db.transaction(() => {
      const now = clock.now();
      const at = utcIso(now);
      const created: string[] = [];
      for (const { key: line, inputs, rules } of this.activeLines()) {
        if (created.length >= limit) break;
        const previous = db.query<{ last_at: string }>("SELECT last_at FROM cron_fires WHERE line = ?").get(line);
        for (const rule of rules) {
          if (rule.on !== "cron") continue;
          const schedule = rule.schedule ?? "";
          const duration = CRON_SCHEDULE.exec(schedule)?.[1];
          const interval = duration ? parseDuration(duration) * 1000 : NaN;
          if (!Number.isFinite(interval)) {
            this.event("scheduler.cron", { line, schedule, code: "CRON_INVALID" });
            continue;
          }
          if (Object.keys(inputs).length) {
            this.event("scheduler.cron", { line, code: "CRON_INPUT_REQUIRED" });
            continue;
          }
          if (options.enabled === false || (previous && now.getTime() < Date.parse(previous.last_at) + interval)) continue;
          const run = this.start(line, { inputs: {}, trigger: { on: "cron", schedule, by: "scheduler" } }, "scheduler.cron");
          if (!run) continue;
          db.query(`
            INSERT INTO cron_fires (line, last_at, run_id) VALUES (?, ?, ?)
            ON CONFLICT (line) DO UPDATE SET last_at = excluded.last_at, run_id = excluded.run_id
          `).run(line, at, run.id);
          this.event("scheduler.cron", { line, run: run.id }, run.id);
          created.push(run.id);
          break;
        }
      }
      return created.sort();
    }).immediate();
  }

  private activeLines(): Line[] {
    return this.snapshot.keys("Line").flatMap((key) => {
      const { metadata, spec } = this.snapshot.doc(key);
      if ((metadata.lifecycle ?? "active") !== "active") return [];
      return [{ key, rules: spec.trigger ?? [], inputs: spec.interface?.inputs ?? {} }];
    });
  }

  private beanRules(): BeanRule[] {
    return this.activeLines().flatMap(({ key, inputs, rules }) => {
      const entries = Object.entries(inputs);
      const port: BeanRule["port"] = entries.length === 1 ? [entries[0][0], inputType(entries[0][1].type)] : undefined;
      return rules.filter((rule) => rule.on === "bean").map((rule) => ({
        line: key, type: inputType(rule.type ?? ""), label: rule.label, port,
      }));
    });
  }

  private fireBean(rule: BeanRule, bean: SealedBean, labels: ReadonlyMap<string, string>): string | undefined {
    const { db } = this.context;
    const { line, label } = rule;
    const type = inputType(bean.type);
    if (!rule.type || !this.matches(type, rule.type)) return undefined;
    if (label !== undefined) {
      const parts = label.split("=");
      if (parts.length !== 2 || !parts[0]) {
        this.event("run.trigger", { line, code: "TRIGGER_INVALID", reason: "label 不是 k=v" });
        return undefined;
      }
      if (labels.get(parts[0]) !== parts[1]) return undefined;
    }
    if (!rule.port || !this.matches(type, rule.port[1])) {
      this.event("run.trigger", { line, code: "TRIGGER_INVALID", reason: "产线需要一个兼容的接口输入" });
      return undefined;
    }
    if (db.query("SELECT 1 FROM trigger_fires WHERE line = ? AND bean_id = ?").get(line, bean.id)) return undefined;
    const details = { on: "bean", bean: bean.id, label: label ?? null };
    const run = this.start(line, { inputs: { [rule.port[0]]: bean.id }, trigger: details }, "run.trigger");
    if (!run) return undefined;
    db.query("INSERT INTO trigger_fires (line, bean_id, run_id, at) VALUES (?, ?, ?, ?)")
      .run(line, bean.id, run.id, utcIso(this.context.clock.now()));
    this.event("run.trigger", { line, bean: bean.id, label: label ?? null }, run.id);
    return run.id;
  }

  private matches(type: string, expected: string): boolean {
    return expected === "any" || type === expected || this.snapshot.isSubtype(type, expected);
  }

  private enqueueEvents(table: "events" | "job_events", name: string, column: "seq" | "rowid"): void {
    const { db } = this.context;
    const last = db.query<{ seq: number }>("SELECT seq FROM cursors WHERE name = ?").get(name)?.seq ?? 0;
    const latest = db.query<{ seq: number | null }>("SELECT MAX(" + column + ") AS seq FROM " + table + " WHERE kind = 'bean.sealed'").get()?.seq;
    if (latest === null || latest === undefined || latest <= last) return;
    db.query(`
      INSERT INTO trigger_pending (bean_id)
      SELECT data.bean FROM (
        SELECT CASE WHEN json_valid(data) THEN json_extract(data, '$.bean') END AS bean
        FROM ` + table + ` WHERE kind = 'bean.sealed' AND ` + column + ` > ? ORDER BY ` + column + `
      ) AS data WHERE typeof(data.bean) = 'text' ON CONFLICT (bean_id) DO NOTHING
    `).run(last);
    db.query("INSERT INTO cursors (name, seq) VALUES (?, ?) ON CONFLICT (name) DO UPDATE SET seq = excluded.seq").run(name, latest);
  }

  private enqueueExistingBeans(): void {
    const { db } = this.context;
    const previous = db.query<{ epoch: string }>("SELECT epoch FROM trigger_epoch WHERE id = 1").get();
    if (previous?.epoch === this.snapshot.epoch) return;
    db.exec(`
      INSERT INTO trigger_pending (bean_id)
      SELECT id FROM beans WHERE sealed = 1 AND expired_at IS NULL ORDER BY created_at, id
      ON CONFLICT (bean_id) DO NOTHING
    `);
    db.query("INSERT INTO trigger_epoch (id, epoch) VALUES (1, ?) ON CONFLICT (id) DO UPDATE SET epoch = excluded.epoch").run(this.snapshot.epoch);
  }

  private hasJobEvents(): boolean {
    return Boolean(this.context.db.query("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'job_events'").get());
  }

  private start(line: string, options: Parameters<LineStarter["start"]>[1], kind: "run.trigger" | "scheduler.cron"): Run | undefined {
    try {
      return this.context.db.transaction(() => this.lines.start(line, options))();
    } catch (error) {
      if (!(error instanceof FactoryError)) throw error;
      this.event(kind, { line, code: kind === "run.trigger" ? "TRIGGER_INVALID" : error.code, reason: error.code });
      return undefined;
    }
  }

  private event(kind: string, data: JsonObject, runId: string | null = null): void {
    const { db, clock, ids } = this.context;
    db.query("INSERT INTO events (id, at, kind, data, run_id) VALUES (?, ?, ?, ?, ?)")
      .run(ids.new("ev"), utcIso(clock.now()), kind, canonicalJson(data), runId);
  }
}
