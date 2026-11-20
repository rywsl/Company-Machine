import { describe, expect, test } from "bun:test";
import { existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { Context, CounterIds, FakeClock, RandomIds, SystemClock, canonicalBytes, canonicalJson, digestBytes, digestFile, installFactoryctl, loadSnapshot, mediaTypeOf, paramsValue, parseDuration, runtimeEnv, utcIso, type Bean } from "../src/legacy";
import { JobStore } from "../src/legacy/jobs";
import { RunStore } from "../src/legacy/runs";

function tempRoot(label: string): string {
  const root = join("tmp", `legacy-${label}-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  mkdirSync(root, { recursive: true });
  return root;
}

function expectErrorCode(run: () => unknown, code: string, detail?: Record<string, unknown>): void {
  try { run(); }
  catch (error) { expect(error).toMatchObject(detail ? { code, detail } : { code }); return; }
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

  test("rejects unsupported canonical JSON values without changing valid encodings", () => {
    const sparse = new Array(2);
    sparse[1] = "second";
    const cycle: { child?: unknown } = {};
    cycle.child = cycle;
    const invalid: Array<[unknown, string]> = [
      [{ a: [0, Number.NaN] }, "$.a[1]"], [{ a: Infinity }, "$.a"], [{ a: -Infinity }, "$.a"],
      [{ a: new Set(["value"]) }, "$.a"], [{ a: new Map([[1, "value"]]) }, "$.a"],
      [{ a: new Date("2026-01-01T00:00:00Z") }, "$.a"], [{ a: new (class { field = "value"; })() }, "$.a"],
      [{ a: { [Symbol("key")]: "value" } }, "$.a"], [sparse, "$[0]"], [cycle, "$.child"],
    ];
    for (const [value, path] of invalid) {
      expectErrorCode(() => canonicalJson(value), "JSON_INVALID", { path });
    }
    const shared = Object.assign(Object.create(null), { y: "中", z: -0 });
    const encoded = '{"2":2,"10":10,"both":[{"y":"中","z":0},{"y":"中","z":0}],"𐀀":1,"\uE000":2}';
    const value = { "10": 10, "2": 2, both: [shared, shared], "\uE000": 2, "𐀀": 1 };
    expect(canonicalBytes(value)).toEqual(new TextEncoder().encode(encoded));
  });

  test("checks duration parsing, UTC formatting, fake time and independent counters", async () => {
    for (const [text, seconds] of [["250ms", 0.25], ["90m", 5400], ["7d", 604800], ["007s", 7]] as const) {
      expect(parseDuration(text)).toBe(seconds);
    }
    for (const text of ["", "5", "5x", "-5s", "1.5h", " 5s", "5s\n", "5S", 5, ["5s"], { toString: () => "5s" }, "9".repeat(400) + "d"]) {
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

  test("requires a valid instant and finite clock movements", async () => {
    const invalid = new Date(Number.NaN);
    expect(() => utcIso(invalid)).toThrow(RangeError);
    expect(() => new FakeClock(invalid)).toThrow(RangeError);
    expect(() => utcIso({ toISOString: () => "2026-01-01T00:00:00.000Z" } as Date)).toThrow(TypeError);
    const start = new Date("2026-01-01T00:00:00Z");
    const clock = new FakeClock(start);
    start.setTime(0);
    for (const seconds of [-1, Infinity, -Infinity, Number.NaN, Number.MAX_VALUE]) {
      expect(() => clock.advance(seconds)).toThrow(RangeError);
    }
    await expect(clock.sleep(Number.NaN)).rejects.toThrow(RangeError);
    expect(clock.monotonic()).toBe(0);
    expect(clock.now().toISOString()).toBe("2026-01-01T00:00:00.000Z");
    const system = new SystemClock();
    const before = Date.now();
    const instant = system.now();
    const after = Date.now();
    expect(instant.getTime()).toBeGreaterThanOrEqual(before);
    expect(instant.getTime()).toBeLessThanOrEqual(after);
    expect(utcIso(instant)).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/);
  });

  test("generates unique IDs in the Bun format and validates prefixes", () => {
    const ids = new RandomIds();
    const values = new Set(Array.from({ length: 8 }, () => ids.new("j")));
    expect(values.size).toBe(8);
    for (const value of values) expect(value).toMatch(/^j-[a-f0-9]{16}$/);
    for (const prefix of ["", "Job", "a123", "prefixlong"]) expect(() => ids.new(prefix)).toThrow("ID prefix is invalid");
  });

  test("uses SQLite WAL, commits rows and rolls back outer and nested transactions", () => {
    const root = tempRoot("sqlite-wal");
    const context = Context.open(root);
    try {
      const db = context.db;
      expect(db.query("PRAGMA journal_mode").get()).toEqual({ journal_mode: "wal" });
      expect(db.query("PRAGMA busy_timeout").get()).toEqual({ timeout: 5000 });
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

  test("initializes a portable factory layout and installs the bundled factoryctl idempotently", async () => {
    const root = tempRoot("factory-layout");
    const provider = { open() { throw new Error("Context.open must not launch a sandbox"); } };
    const clock = new FakeClock();
    const ids = new CounterIds();
    const context = Context.open(root, { provider, clock, ids });
    try {
      expect(context.root).toBe(resolve(root));
      expect(context.provider).toBe(provider);
      expect(context.clock).toBe(clock);
      expect(context.ids).toBe(ids);
      expect(lstatSync(join(context.root, "factory.db")).isFile()).toBe(true);
      for (const directory of ["cas", "jobs", "tmp", "tools/factory/bin"]) {
        expect(lstatSync(join(context.root, directory)).isDirectory()).toBe(true);
      }
      expect(context.warehouse.list()).toEqual([]);
      const installed = await installFactoryctl(context.root);
      expect(installed).toBe(join(context.root, "tools", "factory", "bin", "factoryctl"));
      const bytes = readFileSync(installed, "utf8");
      expect(bytes.startsWith("#!/usr/bin/env bun\n")).toBe(true);
      expect(bytes).not.toContain("\r\n");
      const original = lstatSync(installed);
      if (process.platform !== "win32") expect(original.mode & 0o111).toBe(0o111);
      expect(await installFactoryctl(context.root)).toBe(installed);
      const after = lstatSync(installed);
      expect([after.ino, after.mtimeMs]).toEqual([original.ino, original.mtimeMs]);
      expect(readdirSync(dirname(installed))).toEqual(["factoryctl"]);
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
    const clock = new FakeClock();
    const context = Context.open(root, { clock });
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
      expectErrorCode(() => store.sealMany([[first.id, { chars: 1 }], [first.id, { chars: 9 }]], null, report), "FACTS_CONFLICT");
      expect(store.get(first.id)).toMatchObject({ sealed: false, facts: {} });
      expect(context.db.query("SELECT id FROM seals").all()).toEqual([]);
      store.sealMany([[first.id, { chars: 1, lines: 1 }], [second.id, { chars: 2, lines: 1 }]], "j-1", report);
      const sealed = { sealed: true, facts: { chars: 1, lines: 1 } };
      expect(store.get(first.id)).toMatchObject({ ...sealed, createdAt: "2026-01-01T00:00:00.000Z" });
      expectErrorCode(() => store.sealMany([[first.id, { chars: 9 }]], "j-2", latestReport), "FACTS_CONFLICT");
      expect(store.get(first.id).facts).toEqual(sealed.facts);
      store.sealMany([[first.id, { lines: 1, chars: 1 }]], "j-2", latestReport);
      expect(store.get(first.id)).toMatchObject(sealed);
      clock.advance(1);
      store.sealMany([[first.id, sealed.facts]], null, report);
      clock.advance(1);
      store.sealMany([[first.id, sealed.facts]], null, report);
      store.sealMany([[first.id, sealed.facts]], null, latestReport);
      const evidence = context.db.query("SELECT job_id, report_digest, facts_json, sealed_at FROM seals WHERE bean_id=? ORDER BY id").all(first.id);
      const facts_json = canonicalJson(sealed.facts);
      expect(evidence).toEqual([
        { job_id: "j-1", report_digest: report, facts_json, sealed_at: "2026-01-01T00:00:00.000Z" },
        { job_id: "j-2", report_digest: latestReport, facts_json, sealed_at: "2026-01-01T00:00:00.000Z" },
        { job_id: null, report_digest: report, facts_json, sealed_at: "2026-01-01T00:00:01.000Z" },
        { job_id: null, report_digest: latestReport, facts_json, sealed_at: "2026-01-01T00:00:02.000Z" },
      ]);
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
      await expect(store.materialize(bean.id, input, "bean")).rejects.toMatchObject({ code: "DEST_INVALID" });
      expect(readFileSync(input, "utf8")).toBe("hello");
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
      const cardBlob = store.blobPath(cardBean.payload[0].digest);
      rmSync(cardBlob);
      mkdirSync(cardBlob);
      context.db.query("UPDATE beans SET sealed = 0 WHERE id=?").run(noteBean.id);
      const problems = await store.verify();
      expect(problems.map(({ kind }) => kind)).toEqual(["digest_mismatch", "facts_unsealed", "id_mismatch", "missing_blob", "seal_flag"]);
      expect(problems.find(({ kind }) => kind === "id_mismatch")).toMatchObject({ bean_id: noteBean.id });
      expect(problems.find(({ kind }) => kind === "missing_blob")).toMatchObject({
        bean_id: cardBean.id, digest: cardBean.payload[0].digest, path: "card.json",
      });
      expect(store.get(noteBean.id).facts).toEqual({ chars: 1 });
      expect(lstatSync(cardBlob).isDirectory()).toBe(true);
      expect(existsSync(join(root, "cas", "sha256"))).toBe(true);
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("hashes Unicode-ordered Bean payloads and reuses earlier Bun identities", async () => {
    const root = tempRoot("warehouse-identity-paths");
    const context = Context.open(root, { clock: new FakeClock() });
    try {
      const input = join(root, "note.txt");
      await Bun.write(input, "hi");
      const digest = await context.warehouse.putFile(input);
      const paths = ["Z.txt", "a.txt", "中.txt", "豈.txt", "𐀀.txt"];
      const records = paths.map((path) => ({ path, digest, size: 2, media_type: "text/plain" }));
      const files = Object.fromEntries([...paths].reverse().map((path) => [path, input]));
      const type = "note.text@1.1";
      const bean = await context.warehouse.add({ type, files });
      expect(bean.id).toBe(digestBytes(canonicalBytes({ type, meta: {}, payload: records })));
      expect(bean.payload.map(({ path }) => path)).toEqual(paths);

      const meta = { title: "earlier build" };
      const previousOrder = [...records].sort((left, right) => left.path.localeCompare(right.path));
      expect(previousOrder).not.toEqual(records);
      const previousId = digestBytes(canonicalBytes({ type, meta, payload: previousOrder }));
      const timestamp = "2025-12-31T00:00:00.000Z";
      context.db.query("INSERT INTO beans (id,type,meta_json,facts_json,created_at) VALUES (?,?,?,?,?)")
        .run(previousId, type, canonicalJson(meta), "{}", timestamp);
      const insert = context.db.query("INSERT INTO payload_files (bean_id,path,digest,size,media_type) VALUES (?,?,?,?,?)");
      for (const { path, digest, size, media_type } of previousOrder) insert.run(previousId, path, digest, size, media_type);
      expect(await context.warehouse.add({ type, meta, files })).toMatchObject({ id: previousId, createdAt: timestamp });
      expect(context.warehouse.list({ sealedOnly: false })).toHaveLength(2);
      expect(await context.warehouse.verify()).toEqual([]);
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("replaces lineage atomically and traverses cycles without losing port ordinals", async () => {
    const root = tempRoot("warehouse-lineage");
    const context = Context.open(root);
    try {
      const store = context.warehouse;
      const input = join(root, "note.txt");
      await Bun.write(input, "hi");
      const draft = { type: "note.text@1.1", files: { "note.txt": input } };
      const [source, sibling, leaf] = await Promise.all(["source", "sibling", "leaf"]
        .map(async (name) => (await store.add({ ...draft, meta: { name } })).id));
      store.recordLineage("j-2", { lines: [sibling, source], constructor: [source] }, { output: [leaf] });
      store.recordLineage("j-1", { input: [leaf] }, { output: [source] });
      const expected: ReturnType<typeof store.lineage>["jobs"] = [
        { jobId: "j-1", inputs: { input: [leaf] }, outputs: { output: [source] } },
        { jobId: "j-2", inputs: { constructor: [source], lines: [sibling, source] }, outputs: { output: [leaf] } },
      ];
      expect(store.lineage(leaf, "up")).toEqual({ id: leaf, direction: "up", jobs: expected, beans: [sibling, source].sort() });
      expect(store.lineage(sibling, "down")).toEqual({ id: sibling, direction: "down", jobs: expected, beans: [leaf, source].sort() });
      const missing = "sha256:" + "22".repeat(32);
      expectErrorCode(() => store.recordLineage("j-2", { lines: [missing] }, { output: [leaf] }), "BEAN_NOT_FOUND");
      expect(store.lineage(leaf, "up").jobs).toEqual(expected);
      store.recordLineage("j-2", { lines: [source] }, { output: [leaf] });
      expect(store.lineage(sibling, "down")).toEqual({ id: sibling, direction: "down", jobs: [], beans: [] });
      expect(store.lineage(leaf, "up").jobs[1]).toEqual({ jobId: "j-2", inputs: { lines: [source] }, outputs: { output: [leaf] } });
      expect(() => store.lineage(leaf, "sideways" as "up")).toThrow("direction must be up or down");
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

  test("rejects unsafe integer station parameters and defaults", async () => {
    const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
    const station = "Station:upper@1";
    const declaration = { type: "int", default: Number.MAX_SAFE_INTEGER };
    snapshot.docs[station].spec.params = { count: declaration };
    expect(paramsValue(snapshot, station, {})).toEqual({ count: Number.MAX_SAFE_INTEGER });
    expect(paramsValue(snapshot, station, { count: Number.MIN_SAFE_INTEGER })).toEqual({ count: Number.MIN_SAFE_INTEGER });

    const { count } = JSON.parse('{"count":9007199254740993}');
    for (const value of [count, -count]) {
      const detail = { station, problems: [{ name: "count", reason: "type", expected: "int", value }] };
      expectErrorCode(() => paramsValue(snapshot, station, { count: value }), "PARAM_INVALID", detail);
      declaration.default = value;
      expectErrorCode(() => paramsValue(snapshot, station, {}), "PARAM_INVALID", detail);
    }
  });
});
