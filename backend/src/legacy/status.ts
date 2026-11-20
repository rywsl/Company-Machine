import { existsSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { ManagedDatabase } from "../sqlite";
import { type Clock, SystemClock, utcIso } from "./foundation";

type Counts = Record<string, number>;
type Alert = { code: string; severity: "error" | "warning"; detail: Record<string, unknown> };
type Lease = { owner: string; expires_at: string };

function readDocument(path: string): Record<string, unknown> {
  try {
    const data = JSON.parse(readFileSync(path, "utf8"));
    return data && typeof data === "object" && !Array.isArray(data) ? data : {};
  } catch (error) {
    if (error instanceof SyntaxError || (error as { code?: string }).code === "ENOENT") return {};
    throw error;
  }
}

function readDatabase<T>(path: string, read: (db?: ManagedDatabase) => T): T {
  if (!existsSync(path)) return read();
  const db = new ManagedDatabase(path, { readonly: true });
  try {
    db.exec("PRAGMA busy_timeout = 5000");
    return db.transaction(() => read(db))();
  } finally { db.close(); }
}

function hasTable(db: ManagedDatabase | undefined, table: string): boolean {
  return Boolean(db?.query("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?").get(table));
}

function counts(db: ManagedDatabase | undefined, table: string, column = "state"): Counts {
  if (!db || !hasTable(db, table)) return {};
  return Object.fromEntries(db.query<{ label: string; count: number }>(
    `SELECT ${column} AS label, count(*) AS count FROM ${table} GROUP BY ${column} ORDER BY ${column}`,
  ).all().map(({ label, count }) => [label, count]));
}

function lease(db: ManagedDatabase | undefined, resource: string, now: Date) {
  const active = db && hasTable(db, "leases")
    ? db.query<Lease>("SELECT owner, expires_at FROM leases WHERE resource = ? AND julianday(expires_at) > julianday(?)").get(resource, utcIso(now))
    : null;
  return { online: Boolean(active), owner: active?.owner ?? null, expires_at: active?.expires_at ?? null };
}

function round(value: number): number { return Number(value.toFixed(6)); }

function spend(db: ManagedDatabase | undefined, day: string) {
  const historicalSessions = Boolean(db && hasTable(db, "foreman_sessions") && db.query<{ name: string }>("PRAGMA table_info(foreman_sessions)").all()
    .some(({ name }) => name === "cost_usd"));
  const usage = hasTable(db, "model_usage");
  const jobs = hasTable(db, "jobs");
  const sum = (sql: string): { total: number; today: number } => db?.query<{ total: number; today: number }>(sql).get(day) ?? { total: 0, today: 0 };
  const model = usage ? sum(`SELECT COALESCE(sum(cost_usd), 0) AS total,
    COALESCE(sum(CASE WHEN date(at) = ? THEN cost_usd END), 0) AS today FROM model_usage AS use
    ${historicalSessions ? "WHERE NOT EXISTS (SELECT 1 FROM foreman_sessions WHERE id = use.job_id)" : ""}`) : { total: 0, today: 0 };
  const transcript = jobs ? sum(`SELECT COALESCE(sum(cost_usd), 0) AS total,
    COALESCE(sum(CASE WHEN date(updated_at) = ? THEN cost_usd END), 0) AS today FROM jobs AS job
    ${usage ? "WHERE NOT EXISTS (SELECT 1 FROM model_usage WHERE job_id = job.id)" : ""}`) : { total: 0, today: 0 };
  const foreman = historicalSessions ? sum(`SELECT COALESCE(sum(cost_usd), 0) AS total,
    COALESCE(sum(CASE WHEN date(started_at) = ? THEN cost_usd END), 0) AS today FROM foreman_sessions`) : { total: 0, today: 0 };
  return {
    today: { today_usd: round(model.today + transcript.today + foreman.today), foreman_today_usd: round(foreman.today) },
    total: { foreman: round(foreman.total), model: round(model.total), transcript: round(transcript.total) },
    sessions: historicalSessions ? counts(db, "foreman_sessions") : {},
  };
}

function readBusiness(db?: ManagedDatabase) {
  const outbox = counts(db, "foreman_outbox", "status");
  return {
    outbox,
    systems: hasTable(db, "business_systems") ? db!.query<{ count: number }>("SELECT count(*) AS count FROM business_systems").get()?.count ?? 0 : 0,
    sessions: hasTable(db, "business_events") ? db!.query<{ count: number }>("SELECT count(*) AS count FROM business_events WHERE type = 'foreman.session.started'").get()?.count ?? 0 : 0,
  };
}

export function factoryStatus(paths: { root: string; factory: string; business: string }, clock: Clock = new SystemClock()) {
  const now = clock.now();
  const at = utcIso(now);
  const config = readDocument(join(paths.root, "factory.json"));
  const heartbeat = readDocument(join(paths.root, "serve.json"));
  const separateBusiness = resolve(paths.factory) !== resolve(paths.business);
  return readDatabase(paths.factory, (db) => {
    const business = separateBusiness ? readDatabase(paths.business, readBusiness) : readBusiness(db);
    const serve = { ...lease(db, "serve", now), heartbeat_at: typeof heartbeat.last_tick_at === "string" ? heartbeat.last_tick_at : null,
      tick: typeof heartbeat.tick === "number" ? heartbeat.tick : null };
    const supervisor = lease(db, "supervisor", now);
    const pointers = hasTable(db, "epoch_pointers") ? db!.query<{ name: string; commit: string }>("SELECT name, \"commit\" FROM epoch_pointers").all() : [];
    const epochs = pointers.length || config.governance
      ? { active: null, candidate: null, previous: null, ...Object.fromEntries(pointers.map(({ name, commit }) => [name, commit])) } : null;
    const mode = epochs && hasTable(db, "governance_mode")
      ? db!.query<{ on_flag: number; level: number; since: string | null; until: string | null }>("SELECT on_flag, level, since, until FROM governance_mode WHERE id = 1").get()
      : null;
    const safe_mode = epochs ? { on: mode?.on_flag === 1, level: mode?.level ?? 0, since: mode?.since ?? null, until: mode?.until ?? null } : null;
    const jobs = counts(db, "jobs");
    const outboxNames: Record<string, string> = { queued: "open", claimed: "taken", done: "done", failed: "failed" };
    const wakes = counts(db, "wakes", "status");
    for (const [state, count] of Object.entries(business.outbox)) {
      const label = outboxNames[state];
      if (label) wakes[label] = (wakes[label] ?? 0) + count;
    }
    const alerts: Alert[] = [];
    if (config.serve && !serve.online) alerts.push({ code: "SERVE_DOWN", severity: "error", detail: {} });
    if (safe_mode?.on) alerts.push({ code: "SAFE_MODE", severity: "error", detail: { level: safe_mode.level } });
    if (wakes.failed) alerts.push({ code: "WAKES_FAILED", severity: "error", detail: { count: wakes.failed } });
    const serveConfig = config.serve as { supervise?: boolean; tick_s?: unknown } | undefined;
    if (config.foreman && !(serveConfig?.supervise ? serve.online : supervisor.online)) alerts.push({ code: "SUPERVISOR_DOWN", severity: "warning", detail: {} });
    if (jobs.DEAD) alerts.push({ code: "DLQ_NONEMPTY", severity: "warning", detail: { count: jobs.DEAD } });
    const tickSeconds = typeof serveConfig?.tick_s === "number" ? Math.max(serveConfig.tick_s, 1) : 1;
    if (serve.online && serve.heartbeat_at && Date.parse(serve.heartbeat_at) < now.getTime() - 3000 * tickSeconds) {
      alerts.push({ code: "HEARTBEAT_STALE", severity: "warning", detail: { heartbeat_at: serve.heartbeat_at } });
    }
    const spendSummary = spend(db, at.slice(0, 10));
    return {
      status: {
        ok: !alerts.some(({ severity }) => severity === "error"), at, serve, supervisor, epochs, safe_mode, jobs,
        runs: counts(db, "runs"), changesets: counts(db, "changesets"), wakes: Object.fromEntries(Object.entries(wakes).sort()),
        approvals_pending: counts(db, "approvals", "status").pending ?? 0, spend: spendSummary.today, alerts,
        businessSystems: business.systems, foremanInbox: (business.outbox.queued ?? 0) + (business.outbox.claimed ?? 0),
      },
      spendTotal: spendSummary.total,
      sessions: { ...spendSummary.sessions, ...(business.sessions ? { started: business.sessions } : {}) },
    };
  });
}

function label(value: string): string { return value.replaceAll("\\", "\\\\").replaceAll("\n", "\\n").replaceAll('"', '\\"'); }

export function renderMetrics({ status, spendTotal, sessions }: ReturnType<typeof factoryStatus>): string {
  const lines: string[] = [];
  const family = (name: string, help: string, type: "gauge" | "counter", value: number | Counts, key?: string) => {
    lines.push(`# HELP ${name} ${help}`, `# TYPE ${name} ${type}`);
    if (typeof value === "number") lines.push(`${name} ${value}`);
    else for (const sample of Object.keys(value).sort()) lines.push(`${name}{${key}="${label(sample)}"} ${value[sample]}`);
  };
  family("mao_up", "Whether a factory component holds its runtime lease.", "gauge", { serve: Number(status.serve.online), supervisor: Number(status.supervisor.online) }, "component");
  family("mao_jobs", "Jobs by current state.", "gauge", status.jobs, "state");
  family("mao_runs", "Line runs by current state.", "gauge", status.runs, "state");
  family("mao_changesets", "Registry changes by current state.", "gauge", status.changesets, "state");
  family("mao_wakes", "Foreman work by status.", "gauge", status.wakes, "status");
  family("mao_safe_mode", "Whether governance safe mode is active.", "gauge", Number(status.safe_mode?.on ?? false));
  family("mao_spend_usd_total", "Recorded historical spending in USD.", "counter", spendTotal, "source");
  family("mao_foreman_sessions_total", "Recorded Foreman sessions by state.", "counter", sessions, "state");
  family("mao_alerts", "Current factory alerts.", "gauge", Object.fromEntries(status.alerts.map(({ code }) => [code, 1])), "code");
  return lines.join("\n");
}
