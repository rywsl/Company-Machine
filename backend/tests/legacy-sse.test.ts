import { expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { rm } from "node:fs/promises";
import { join } from "node:path";
import { createApp } from "../src/server";
import { Context } from "../src/legacy/context";

test("bridges newly appended legacy events into the business SSE stream once", async () => {
  const root = mkdtempSync(join(process.env.TEMP ?? process.env.TMP ?? ".", "mao-legacy-sse-"));
  const dbPath = join(root, "factory.db");
  const app = createApp({ dbPath, legacyPath: dbPath, storageRoot: root, dispatchWork: false });
  const legacy = Context.open(root);
  try {
    legacy.db.exec("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, at TEXT, kind TEXT, data TEXT, job_id TEXT, run_id TEXT)");
    legacy.db.query("INSERT INTO events (id,at,kind,data,job_id) VALUES (?,?,?,?,?)")
      .run("legacy-sse-1", new Date().toISOString(), "job.state", JSON.stringify({ to: "RUNNING" }), "job-1");
    const response = await app.fetch(new Request("http://localhost/api/v1/events?cursor=0"));
    const reader = response.body!.getReader();
    const chunk = new TextDecoder().decode((await reader.read()).value);
    expect(chunk).toContain("event: mao.event");
    expect(chunk).toContain('"type":"legacy.job.state"');
    expect(chunk).toContain('"source":"legacy"');
    expect(chunk).toContain('"legacy_id":"legacy-sse-1"');
    expect(chunk).toContain('"job_id":"job-1"');
    await reader.cancel();

    const bridged = app.store.eventsAfter().filter((event) => event.type === "legacy.job.state");
    expect(bridged).toHaveLength(1);
    app.store.syncLegacyEvents();
    expect(app.store.eventsAfter().filter((event) => event.type === "legacy.job.state")).toHaveLength(1);
  } finally {
    legacy.close();
    app.stop();
    await rm(root, { recursive: true, force: true, maxRetries: 3 });
  }
});

test("does not duplicate bridged legacy events in web logs", async () => {
  const root = mkdtempSync(join(process.env.TEMP ?? process.env.TMP ?? ".", "mao-legacy-log-"));
  const dbPath = join(root, "factory.db");
  const app = createApp({ dbPath, legacyPath: dbPath, storageRoot: root, dispatchWork: false });
  const legacy = Context.open(root);
  try {
    legacy.db.exec("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, at TEXT, kind TEXT, data TEXT, job_id TEXT, run_id TEXT)");
    legacy.db.query("INSERT INTO events (id,at,kind,data) VALUES (?,?,?,?)")
      .run("legacy-log-1", new Date().toISOString(), "bean.sealed", JSON.stringify({ bean: "bean-1" }));
    const response = await app.fetch(new Request("http://localhost/api/web/logs?limit=20"));
    const logs = await response.json() as Array<{ id: string; kind: string; data: { legacy_id?: string } }>;
    expect(logs.filter((event) => event.data?.legacy_id === "legacy-log-1")).toHaveLength(1);
    expect(logs.filter((event) => event.kind === "legacy.bean.sealed")).toHaveLength(1);
  } finally {
    legacy.close();
    app.stop();
    await rm(root, { recursive: true, force: true, maxRetries: 3 });
  }
});
