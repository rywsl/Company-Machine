import { describe, expect, test } from "bun:test";
import { existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { Context, CounterIds, FakeClock, canonicalBytes, canonicalJson, digestBytes, digestFile, loadSnapshot, mediaTypeOf, paramsValue, parseDuration, runtimeEnv, utcIso, type Bean } from "../src/legacy";
import { JobStore } from "../src/legacy/jobs";
import { RunStore } from "../src/legacy/runs";

function tempRoot(label: string): string {
  const root = join("tmp", `legacy-${label}-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  mkdirSync(root, { recursive: true });
  return root;
}

function expectErrorCode(run: () => unknown, code: string): void {
  try { run(); }
  catch (error) { expect(error).toMatchObject({ code }); return; }
  throw new Error(`Expected ${code}`);
}

describe("Bun M1 core boundaries", () => {
  test("upgrades an archived SQLite schema twice and preserves records and its migration version", () => {
    const root = tempRoot("sqlite-compatibility");
    let context = Context.open(root);
    const at = "2026-01-01T00:00:00.000Z";
    try {
      const migration = join(import.meta.dir, "../../removed/python/src/mao/migrations/002_jobs.sql");
      context.db.exec(readFileSync(migration, "utf8"));
      context.db.exec("PRAGMA user_version=2");
      context.db.query(`INSERT INTO jobs (id,station,epoch,state,attempt,inputs,params,created_at,updated_at)
        VALUES ('job-archived','station@1','epoch','PENDING',1,'{}','{}',?,?)`).run(at, at);
      context.db.query(`INSERT INTO events (id,at,kind,job_id,data)
        VALUES ('ev-archived',?,'job.created','job-archived','{}')`).run(at);
      for (let attempt = 0; attempt < 2; attempt++) {
        context.close();
        context = Context.open(root);
        const { db, clock, ids } = context;
        const jobs = new JobStore(db, clock, ids);
        new RunStore(db, clock, ids);
        expect(jobs.get("job-archived")).toMatchObject({ id: "job-archived", phase: "execute", createdAt: at });
        expect(db.query("SELECT id, job_id, run_id FROM events WHERE id='ev-archived'").get()).toEqual({ id: "ev-archived", job_id: "job-archived", run_id: null });
        expect(db.query("PRAGMA user_version").get()).toEqual({ user_version: 2 });
      }
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("serializes sorted compact UTF-8 JSON and hashes the known empty payload", () => {
    expect(canonicalJson({ b: 1, a: [1.5, "中"] })).toBe('{"a":[1.5,"中"],"b":1}');
    expect(canonicalJson([1, 2])).toBe("[1,2]");
    expect(canonicalJson({ a: 1 })).not.toContain("\n");
    expect(canonicalBytes({ a: "中" })).toEqual(new TextEncoder().encode('{"a":"中"}'));
    expect(digestBytes(new Uint8Array())).toBe("sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  });

  test("checks duration parsing, UTC formatting, fake time and independent counters", async () => {
    for (const [text, seconds] of [["250ms", 0.25], ["90m", 5400], ["7d", 604800], ["007s", 7]] as const) {
      expect(parseDuration(text)).toBe(seconds);
    }
    for (const text of ["", "5", "5x", "-5s", "1.5h", " 5s", "5S", 5]) {
      expectErrorCode(() => parseDuration(text as string), "DURATION_INVALID");
    }
    expect(utcIso(new Date("2026-01-02T03:04:05.123999Z"))).toBe("2026-01-02T03:04:05.123Z");
    expect(utcIso(new Date("2026-01-02T08:00:00+08:00"))).toBe("2026-01-02T00:00:00.000Z");
    const start = new Date("2026-01-01T00:00:00Z");
    const clock = new FakeClock(start);
    expect(clock.now()).toEqual(start);
    expect(clock.now()).toEqual(start);
    clock.advance(1.5);
    await clock.sleep(0.5);
    expect(clock.monotonic()).toBe(2);
    expect(clock.now()).toEqual(new Date(start.getTime() + 2000));
    expect(() => clock.advance(-1)).toThrow(RangeError);
    const ids = new CounterIds();
    expect([ids.new("j"), ids.new("j"), ids.new("a")]).toEqual(["j-0001", "j-0002", "a-0001"]);
    expect(() => ids.new("Job")).toThrow("ID prefix is invalid");
  });

  test("uses SQLite WAL, commits rows and rolls back outer and nested transactions", () => {
    const root = tempRoot("sqlite-wal");
    const context = Context.open(root);
    try {
      const db = context.db;
      expect(db.query("PRAGMA journal_mode").get()).toEqual({ journal_mode: "wal" });
      db.exec("CREATE TABLE t (n INTEGER)");
      const insert = db.query("INSERT INTO t VALUES (?)");
      expect(() => db.transaction(() => { insert.run(3); throw new Error("outer"); }).immediate()).toThrow("outer");
      expect(db.query("SELECT count(*) AS c FROM t").get()).toEqual({ c: 0 });
      db.transaction(() => {
        insert.run(1);
        expect(() => db.transaction(() => { insert.run(2); throw new Error("inner"); })()).toThrow("inner");
      }).immediate();
      expect(db.query("SELECT n FROM t").all()).toEqual([{ n: 1 }]);
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("streams file digests and keeps one empty blob at its exact CAS path", async () => {
    const root = tempRoot("warehouse-cas-layout");
    const context = Context.open(root);
    try {
      const bytes = new Uint8Array(1024 * 1024 + 3).fill(120);
      const input = join(root, "large.bin");
      await Bun.write(input, bytes);
      expect(await digestFile(input)).toBe(digestBytes(bytes));
      const empty = join(root, "empty.bin");
      await Bun.write(empty, new Uint8Array());
      const digest = await context.warehouse.putFile(empty);
      expect(digest).toBe("sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
      expect(digest).toBe(digestBytes(new Uint8Array()));
      const path = context.warehouse.blobPath(digest);
      const hex = digest.slice(7);
      const sha = join(context.root, "cas", "sha256");
      expect(path).toBe(join(sha, hex.slice(0, 2), hex.slice(2)));
      expect(lstatSync(path).isFile()).toBe(true);
      expect(await context.warehouse.putFile(empty)).toBe(digest);
      expect(readdirSync(sha)).toEqual([hex.slice(0, 2)]);
      expect(readdirSync(join(sha, hex.slice(0, 2)))).toEqual([hex.slice(2)]);
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("preserves Bean identity and rejects invalid paths, types, digests and IDs", async () => {
    const root = tempRoot("warehouse-identity");
    const clock = new FakeClock();
    const context = Context.open(root, { clock });
    try {
      const input = join(root, "note.txt");
      await Bun.write(input, "hi");
      const type = "note.text@1.1";
      const meta = { title: "t" };
      const files = { "note.txt": input };
      const added = await context.warehouse.add({ type, meta, files });
      expect(added).toMatchObject({ sealed: false, facts: {}, createdAt: "2026-01-01T00:00:00.000Z" });
      expect(added.createdAt).toBe(utcIso(clock.now()));
      const digest = digestBytes(new TextEncoder().encode("hi"));
      expect(added.payload).toEqual([{ path: "note.txt", digest, size: 2, mediaType: "text/plain" }]);
      expect(added.id).toBe(digestBytes(canonicalBytes({ type, meta, payload: [{ path: "note.txt", digest, size: 2, media_type: "text/plain" }] })));
      expect([mediaTypeOf("note.txt"), mediaTypeOf("card.json")]).toEqual(["text/plain", "application/json"]);
      added.meta.title = "changed";
      expect(context.warehouse.get(added.id).meta).toEqual(meta);
      clock.advance(5);
      expect(await context.warehouse.add({ type, meta, files })).toMatchObject({ id: added.id, createdAt: added.createdAt });
      for (const path of ["../x", "a\\b.txt"]) {
        await expect(context.warehouse.add({ type, files: { [path]: input } })).rejects.toMatchObject({ code: "PAYLOAD_PATH_INVALID" });
      }
      await expect(context.warehouse.add({ type: "note.text@1", files })).rejects.toMatchObject({ code: "BEAN_TYPE_INVALID" });
      expectErrorCode(() => context.warehouse.blobPath("sha256:ABCD"), "DIGEST_INVALID");
      expectErrorCode(() => context.warehouse.get("sha256:" + "00".repeat(32)), "BEAN_NOT_FOUND");
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("rolls back a failed Bean seal and keeps facts stable across resealing", async () => {
    const root = tempRoot("warehouse-seals");
    const context = Context.open(root, { clock: new FakeClock() });
    const report = "sha256:" + "ab".repeat(32);
    const latestReport = "sha256:" + "cd".repeat(32);
    try {
      const input = join(root, "note.txt");
      await Bun.write(input, "a");
      const first = await context.warehouse.add({ type: "note.text@1.1", files: { "note.txt": input } });
      await Bun.write(input, "b");
      const second = await context.warehouse.add({ type: "note.text@1.1", files: { "note.txt": input } });
      const missing = "sha256:" + "00".repeat(32);
      const store = context.warehouse;
      expectErrorCode(() => store.sealMany([[first.id, { chars: 1 }], [missing, {}]], "j-1", report), "BEAN_NOT_FOUND");
      expect(store.get(first.id).sealed).toBe(false);
      expect(store.get(second.id).sealed).toBe(false);
      store.sealMany([[first.id, { chars: 1, lines: 1 }], [second.id, { chars: 2, lines: 1 }]], "j-1", report);
      const sealed = { sealed: true, facts: { chars: 1, lines: 1 } };
      expect(store.get(first.id)).toMatchObject({ ...sealed, createdAt: "2026-01-01T00:00:00.000Z" });
      expectErrorCode(() => store.sealMany([[first.id, { chars: 9 }]], "j-2", latestReport), "FACTS_CONFLICT");
      expect(store.get(first.id).facts).toEqual(sealed.facts);
      store.sealMany([[first.id, { lines: 1, chars: 1 }]], "j-2", latestReport);
      expect(store.get(first.id)).toMatchObject(sealed);
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("materializes both warehouse layouts with identical canonical Bean metadata", async () => {
    const root = tempRoot("warehouse-layouts");
    const context = Context.open(root);
    try {
      const input = join(root, "note.txt");
      await Bun.write(input, "hello");
      const store = context.warehouse;
      const bean = await store.add({ type: "note.text@1.1", meta: { title: "t" }, files: { "note.txt": input } });
      const portDirectory = join(root, "port");
      const beanDirectory = join(root, "bean");
      await store.materialize(bean.id, portDirectory, "port");
      await store.materialize(bean.id, beanDirectory, "bean");
      const expected = canonicalJson({ id: bean.id, type: bean.type, meta: bean.meta, facts: {},
        payload: [{ path: "note.txt", digest: bean.payload[0].digest, size: 5, media_type: "text/plain" }],
      });
      for (const [layout, file] of [[portDirectory, join(portDirectory, "note.txt")], [beanDirectory, join(beanDirectory, "payload", "note.txt")]]) {
        expect(existsSync(file)).toBe(true);
        expect(readFileSync(file, "utf8")).toBe("hello");
        expect(readFileSync(join(layout, "bean.json"), "utf8")).toBe(expected);
      }
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("filters sealed and unsealed Beans and verifies modified blobs and identities", async () => {
    const root = tempRoot("warehouse-verify");
    const context = Context.open(root);
    try {
      const note = join(root, "note.txt");
      const card = join(root, "card.json");
      await Bun.write(note, "n");
      await Bun.write(card, "{}");
      const store = context.warehouse;
      const noteBean = await store.add({ type: "note.text@1.1", files: { "note.txt": note } });
      const cardBean = await store.add({ type: "card@1.0", meta: { title: "c" }, files: { "card.json": card } });
      expect(store.list().map(({ id }) => id)).not.toContain(noteBean.id);
      store.sealMany([[noteBean.id, { chars: 1 }]], "job-1", "sha256:" + "ab".repeat(32));
      const listed = store.list({ typePrefix: "note.text" }).map(({ id }) => id);
      expect(listed).toContain(noteBean.id);
      expect(listed).not.toContain(cardBean.id);
      expect(store.list({ typePrefix: "card", sealedOnly: false }).map(({ id }) => id)).toContain(cardBean.id);
      expect(await store.verify()).toEqual([]);
      const blob = store.blobPath(noteBean.payload[0].digest);
      writeFileSync(blob, "nx");
      expect((await store.verify()).some(({ kind }) => kind === "digest_mismatch")).toBe(true);
      context.db.query("UPDATE beans SET meta_json=? WHERE id=?").run(canonicalJson({ tampered: true }), noteBean.id);
      expect((await store.verify()).some(({ kind, bean_id }) => kind === "id_mismatch" && bean_id === noteBean.id)).toBe(true);
      expect(existsSync(join(root, "cas", "sha256"))).toBe(true);
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });
  test("canonical JSON, IDs and durations are deterministic", () => {
    expect(canonicalJson({ b: 2, a: 1 })).toBe('{"a":1,"b":2}');
    expect(new CounterIds().new("job")).toBe("job-0001");
    expect(new FakeClock().monotonic()).toBe(0);
  });

  test("reuses SQLite statements and releases all database handles when the context closes", () => {
    const root = tempRoot("sqlite-close");
    const context = Context.open(root);
    try {
      const repeated = context.db.query("SELECT 1 AS value");
      context.db.transaction(() => {
        for (let value = 0; value < 64; value += 1) {
          expect(context.db.query("SELECT " + value + " AS value").get()).toEqual({ value });
        }
      })();
      expect(context.db.query("SELECT 1 AS value")).toBe(repeated);
      context.db.close(true);
    } finally {
      context.close();
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("stores, seals, materializes and traces Bean lineage", async () => {
    const root = tempRoot("warehouse");
    const input = join(root, "input.txt");
    await Bun.write(input, "hello\n");
    const context = Context.open(root, { clock: new FakeClock(), ids: new CounterIds() });
    try {
      const first = await context.warehouse.add({ type: "note.text@1.0", meta: { title: "hello" }, files: { "note.txt": input } });
      const second = await context.warehouse.add({ type: "note.text@1.0", meta: { title: "out" }, files: { "out.txt": input } });
      const report = digestBytes(new TextEncoder().encode("report"));
      await context.warehouse.sealMany([[first.id, { chars: 6 }], [second.id, { chars: 6 }]], "job-0001", report);
      expect(context.warehouse.lineage(second.id, "up")).toEqual({ id: second.id, direction: "up", jobs: [], beans: [] });
      context.warehouse.recordLineage("job-0001", { input: [first.id] }, { output: [second.id] });
      expect(context.warehouse.get(first.id).sealed).toBe(true);
      const up = context.warehouse.lineage(second.id, "up");
      const down = context.warehouse.lineage(first.id, "down");
      expect(up.beans).toEqual([first.id]);
      expect(down.beans).toEqual([second.id]);
      expect(up.jobs).toEqual([{ jobId: "job-0001", inputs: { input: [first.id] }, outputs: { output: [second.id] } }]);
      expect(down.jobs[0].jobId).toBe("job-0001");
      const missing = "sha256:" + "22".repeat(32);
      expectErrorCode(() => context.warehouse.recordLineage("job-0009", { note: [first.id] }, { note: [missing] }), "BEAN_NOT_FOUND");
      expect(context.warehouse.lineage(first.id, "down").jobs).toEqual(down.jobs);
      expectErrorCode(() => context.warehouse.lineage(missing, "up"), "BEAN_NOT_FOUND");
      const materialized = join(root, "materialized");
      await context.warehouse.materialize(second.id, materialized, "bean");
      expect(await Bun.file(join(materialized, "payload", "out.txt")).text()).toBe("hello\n");
      expect((await context.warehouse.verify()).length).toBe(0);
    } finally {
      context.close();
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("concurrent blob writes publish one staged digest and retain the sealed Bean", async () => {
    const root = tempRoot("warehouse-concurrent");
    const context = Context.open(root);
    const other = Context.open(root);
    try {
      const input = join(root, "large-input.txt");
      const payload = new Uint8Array(1024 * 1024 + 257);
      payload.fill(65, 0, 1024 * 1024);
      payload.fill(90, 1024 * 1024);
      await Bun.write(input, payload);
      const draft = { type: "note.text@1.0", files: { "note.txt": input } };
      const copies = await Promise.all(Array.from({ length: 8 }, (_, index) => (index % 2 === 0 ? context : other).warehouse.add(draft)));
      const first = copies[0];
      const digest = digestBytes(payload);
      expect(new Set(copies.map(({ id }) => id))).toEqual(new Set([first.id]));
      expect(first.payload).toEqual([{ path: "note.txt", digest, size: payload.length, mediaType: "text/plain" }]);
      const blob = context.warehouse.blobPath(digest);
      expect(digestBytes(new Uint8Array(await Bun.file(blob).arrayBuffer()))).toBe(digest);
      context.warehouse.sealMany([[first.id, { chars: payload.length }]], "job-001", digest);
      expect(await other.warehouse.add(draft)).toMatchObject({ id: first.id, sealed: true, facts: { chars: payload.length } });

      await expect(context.warehouse.putFile(join(root, "missing.txt"))).rejects.toMatchObject({ code: "ENOENT" });
      expect(readdirSync(join(root, "tmp"))).toEqual([]);
    } finally {
      other.close();
      context.close();
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("loads a checked Registry snapshot and resolves type declarations", async () => {
    const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
    expect(snapshot.keys("Type")).toContain("Type:note.text@1.0");
    expect(snapshot.typeKey("note.text@1")).toBe("Type:note.text@1.0");
    expect(snapshot.factsDecl("note.text@1")).toMatchObject({ chars: "int", lines: "int" });
    expect(snapshot.always("note.text@1")).toEqual(new Set());
  });

  test("projects Bean values and validates station parameters", () => {
    const bean: Bean = { id: "sha256:" + "a".repeat(64), type: "note.text@1.0", facts: {}, meta: {}, payload: [], sealed: true, createdAt: "2026-01-01T00:00:00.000Z" };
    expect(runtimeEnv({ task: "task", factory_contract: "contract" }, { max_turns: 1, timeout_s: 2, budget_usd: 3 }, { model: "m", runtime: "r" })).toEqual({ brief: { task: "task", factory_contract: "contract" }, limits: { max_turns: 1, timeout_s: 2, budget_usd: 3 }, operator: { model: "m", runtime: "r" } });
    const snapshot = { key: () => "Station:test@1", spec: () => ({ params: { count: { type: "int", default: 1 }, label: { type: "string" } } }) } as any;
    expect(paramsValue(snapshot, "test@1", { label: "ok" })).toEqual({ count: 1, label: "ok" });
    expect(() => paramsValue(snapshot, "test@1", { count: "bad" })).toThrow("工位参数不合法");
    expect(bean.id).toContain("sha256:");
  });
});
