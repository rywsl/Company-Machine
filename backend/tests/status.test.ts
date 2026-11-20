import { expect, test } from "bun:test";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { main } from "../src/cli";
import { ManagedDatabase } from "../src/sqlite";
import { FakeClock } from "../src/legacy/foundation";
import { factoryStatus } from "../src/legacy/status";

const today = "2026-11-10T08:00:00.000Z";
const yesterday = "2026-11-09T23:59:59.000Z";
const clock = new FakeClock(new Date(today));

function fixture() {
  const root = mkdtempSync(join(tmpdir(), "mao-status-"));
  const factory = join(root, "factory.db");
  const db = new ManagedDatabase(factory, { create: true });
  db.exec(`
    CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT NOT NULL, cost_usd REAL NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE runs (state TEXT NOT NULL);
    CREATE TABLE foreman_outbox (status TEXT NOT NULL);
  `);
  return { root, db, paths: { root, factory, business: factory }, close: () => { db.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); } };
}

async function cli(...args: string[]) {
  const log = console.log;
  const output: string[] = [];
  console.log = (...values: unknown[]) => { output.push(values.join(" ")); };
  try { return { code: await main(args), output: output.join("\n") }; }
  finally { console.log = log; }
}

test("summarizes sorted factory states and the active epoch through the CLI", async () => {
  const w = fixture();
  try {
    const add = w.db.query("INSERT INTO jobs VALUES (?, ?, 0, ?)");
    for (const [i, state] of ["PENDING", "RUNNING", "SEALED", "SEALED", "DEAD"].entries()) add.run(`job-${i}`, state, today);
    w.db.exec(`
      INSERT INTO runs VALUES ('RUNNING'), ('SUCCEEDED'), ('SUCCEEDED');
      INSERT INTO foreman_outbox VALUES ('queued'), ('done');
      CREATE TABLE epoch_pointers (name TEXT PRIMARY KEY, "commit" TEXT NOT NULL);
      INSERT INTO epoch_pointers VALUES ('active', 'active-epoch');
      CREATE TABLE business_systems (id TEXT NOT NULL);
      INSERT INTO business_systems VALUES ('one'), ('two');
    `);
    const result = await cli("--root", w.root, "--json", "status");
    const status = JSON.parse(result.output);
    expect(result.code).toBe(0);
    expect(status).toMatchObject({ jobs: { DEAD: 1, PENDING: 1, RUNNING: 1, SEALED: 2 }, runs: { RUNNING: 1, SUCCEEDED: 2 },
      wakes: { done: 1, open: 1 }, changesets: {}, epochs: { active: "active-epoch", previous: null, candidate: null },
      serve: { online: false }, supervisor: { online: false }, businessSystems: 2, foremanInbox: 1,
    });
    expect(Object.keys(status.jobs)).toEqual(["DEAD", "PENDING", "RUNNING", "SEALED"]);
  } finally { w.close(); }
});

test("orders safe mode, failed Foreman work and dead letter alerts", async () => {
  const w = fixture();
  try {
    w.db.query("INSERT INTO jobs VALUES ('job-1', 'DEAD', 0, ?)").run(today);
    w.db.exec(`
      INSERT INTO foreman_outbox VALUES ('failed');
      CREATE TABLE epoch_pointers (name TEXT PRIMARY KEY, "commit" TEXT NOT NULL);
      INSERT INTO epoch_pointers VALUES ('active', 'active-epoch');
      CREATE TABLE governance_mode (id INTEGER PRIMARY KEY, on_flag INTEGER, level INTEGER, since TEXT, until TEXT);
      INSERT INTO governance_mode VALUES (1, 1, 2, NULL, NULL);
    `);
    const result = await cli("status", "--root", w.root, "--json");
    expect(result.code).toBe(1);
    expect(JSON.parse(result.output).alerts).toEqual([
      { code: "SAFE_MODE", severity: "error", detail: { level: 2 } },
      { code: "WAKES_FAILED", severity: "error", detail: { count: 1 } },
      { code: "DLQ_NONEMPTY", severity: "warning", detail: { count: 1 } },
    ]);
    expect(w.db.query<{ on_flag: number }>("SELECT on_flag FROM governance_mode").get()?.on_flag).toBe(1);
  } finally { w.close(); }
});

test("reads status without governance and leaves an uninitialized root untouched", async () => {
  const w = fixture();
  try {
    w.db.query("INSERT INTO jobs VALUES ('job-1', 'DEAD', 0, ?)").run(today);
    const local = await cli("status", "--root", w.root);
    expect(local.code).toBe(0);
    expect(JSON.parse(local.output)).toMatchObject({ jobs: { DEAD: 1 }, epochs: null, safe_mode: null });
    const uninitialized = join(w.root, "uninitialized");
    const empty = await cli("status", "--root", uninitialized);
    expect(JSON.parse(empty.output)).toMatchObject({ jobs: {}, runs: {}, businessSystems: 0 });
    expect(existsSync(uninitialized)).toBe(false);
  } finally { w.close(); }
});

test("reads the configured factory and business databases without creating fallback files", async () => {
  const w = fixture();
  const business = join(w.root, "business.db");
  const db = new ManagedDatabase(business, { create: true });
  try {
    w.db.query("INSERT INTO jobs VALUES ('job-factory', 'SEALED', 0, ?)").run(today);
    db.exec(`CREATE TABLE business_systems (id TEXT NOT NULL);
      INSERT INTO business_systems VALUES ('one');
      CREATE TABLE foreman_outbox (status TEXT NOT NULL);
      INSERT INTO foreman_outbox VALUES ('queued'), ('claimed'), ('done');`);
    const health = factoryStatus({ ...w.paths, business }, clock).status;
    expect(health).toMatchObject({ jobs: { SEALED: 1 }, businessSystems: 1, foremanInbox: 2, wakes: { open: 1, taken: 1, done: 1 } });
    const missing = join(w.root, "missing", "business.db");
    expect(factoryStatus({ ...w.paths, business: missing }, clock).status.businessSystems).toBe(0);
    expect(existsSync(missing)).toBe(false);
  } finally { db.close(); w.close(); }
});

test("counts UTC model, transcript and Foreman spend once", () => {
  const w = fixture();
  try {
    w.db.exec(`
      CREATE TABLE model_usage (job_id TEXT, at TEXT, cost_usd REAL);
      CREATE TABLE foreman_sessions (id TEXT, state TEXT, started_at TEXT, cost_usd REAL);
    `);
    const add = w.db.query("INSERT INTO jobs VALUES (?, 'SEALED', ?, ?)");
    add.run("model", 0.5, today); add.run("old-model", 1, yesterday);
    add.run("transcript", 0.25, today); add.run("old-transcript", 0.75, yesterday);
    const usage = w.db.query("INSERT INTO model_usage VALUES (?, ?, ?)");
    usage.run("model", today, 0.5); usage.run("old-model", yesterday, 1); usage.run("foreman", today, 0.4);
    const session = w.db.query("INSERT INTO foreman_sessions VALUES (?, 'ok', ?, ?)");
    session.run("foreman", today, 0.4); session.run("old-foreman", yesterday, 2);
    const result = factoryStatus(w.paths, clock);
    expect(result.status.spend).toEqual({ today_usd: 1.15, foreman_today_usd: 0.4 });
    expect(result.spendTotal).toEqual({ foreman: 2.4, model: 1.5, transcript: 1 });
  } finally { w.close(); }
});

test("emits the documented Prometheus families with sorted and escaped sample labels", async () => {
  const w = fixture();
  try {
    const job = w.db.query("INSERT INTO jobs VALUES (?, ?, 0, ?)");
    for (const [i, state] of ["SEALED", "DEAD", "SEALED", "PENDING"].entries()) job.run(`job-${i}`, state, today);
    w.db.query("INSERT INTO runs VALUES (?)").run('quote"\\line\nsecond');
    const result = await cli("metrics", "--root", w.root);
    expect(result.code).toBe(0);
    const lines = result.output.split("\n");
    const families = ["mao_up", "mao_jobs", "mao_runs", "mao_changesets", "mao_wakes", "mao_safe_mode", "mao_spend_usd_total", "mao_foreman_sessions_total", "mao_alerts"];
    for (const prefix of ["# HELP ", "# TYPE "]) expect(lines.filter((line) => line.startsWith(prefix)).map((line) => line.split(" ")[2])).toEqual(families);
    expect(lines.filter((line) => line.startsWith("mao_up"))).toEqual(['mao_up{component="serve"} 0', 'mao_up{component="supervisor"} 0']);
    expect(lines.filter((line) => line.startsWith("mao_jobs"))).toEqual(['mao_jobs{state="DEAD"} 1', 'mao_jobs{state="PENDING"} 1', 'mao_jobs{state="SEALED"} 2']);
    expect(lines).toContain('mao_runs{state="quote\\"\\\\line\\nsecond"} 1');
    expect(lines).toContain('mao_alerts{code="DLQ_NONEMPTY"} 1');
  } finally { w.close(); }
});
