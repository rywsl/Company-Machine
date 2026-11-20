import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { randomUUID } from "node:crypto";
import { rmSync } from "node:fs";
import { join } from "node:path";
import { JobCache } from "../src/legacy/cache";
import { FactoryError } from "../src/legacy/errors";
import { CounterIds, FakeClock, digestBytes } from "../src/legacy/foundation";
import { Snapshot, loadSnapshot } from "../src/legacy/snapshot";
import { Warehouse } from "../src/legacy/warehouse";

function expectCode(run: () => unknown, code: string): void {
  try {
    run();
  } catch (error) {
    if (!(error instanceof FactoryError)) throw error;
    expect(error.code).toBe(code);
    return;
  }
  throw new Error("Expected " + code);
}

describe("Bun job cache", () => {
  test("uses result-affecting inputs without depending on epoch or descriptive metadata", async () => {
    const db = new Database(":memory:");
    const context = { db, ids: new CounterIds(), clock: new FakeClock() };
    try {
      const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
      const cache = new JobCache(context);
      const station = snapshot.docs["Station:writer@1"];
      const baseline = cache.material(snapshot, "writer@1", { task: "task-1" }, {});
      expect(baseline.toJSON()).toMatchObject({
        v: 1, model: "fake-model-1", inputs: { task: ["task-1"] }, params: { topic: "工厂" },
      });
      expect(cache.key(snapshot, "Station:writer@1", { task: ["task-1"] }, { topic: "工厂" })).toBe(baseline.key());
      const newEpoch = new Snapshot(snapshot.root, "e-elsewhere", snapshot.docs, snapshot.result);
      expect(cache.key(newEpoch, "writer@1", { task: "task-1" }, {})).toBe(baseline.key());
      station.metadata.description = "Updated description";
      station.metadata.labels = { team: "factory" };
      station.metadata.lifecycle = "retired";
      expect(cache.key(snapshot, "writer@1", { task: "task-1" }, {})).toBe(baseline.key());
      expect(cache.key(snapshot, "writer@1", { task: "task-1" }, {}, { variant: "draft" })).not.toBe(baseline.key());
      expect(cache.key(snapshot, "writer@1", { task: ["a", "b"] }, {}))
        .not.toBe(cache.key(snapshot, "writer@1", { task: ["b", "a"] }, {}));
      station.spec.operator.model = "another-model";
      expect(cache.key(snapshot, "writer@1", { task: "task-1" }, {})).not.toBe(baseline.key());
      station.spec.role = "sink";
      expectCode(() => cache.key(snapshot, "writer@1", { task: "task-1" }, {}), "CACHE_NOT_CACHEABLE");
    } finally { db.close(); }
  });

  test("selects the oldest usable hit and applies invalidations only to existing bindings", async () => {
    const root = join("tmp", "legacy-cache-" + randomUUID());
    const db = new Database(":memory:");
    const context = { db, ids: new CounterIds(), clock: new FakeClock() };
    const warehouse = new Warehouse(root, db, context.clock, context.ids);
    try {
      const snapshot = await loadSnapshot("tests/fixtures/registry-m2");
      const cache = new JobCache(context);
      const input = await warehouse.add({ type: "note.text@1.0", files: {}, meta: { source: "fixture" } });
      const good = await warehouse.add({ type: "note.text@1.0", files: {}, meta: { output: "good" } });
      const alternate = await warehouse.add({ type: "note.text@1.0", files: {}, meta: { output: "alternate" } });
      const unsealed = await warehouse.add({ type: "note.text@1.0", files: {}, meta: { output: "unsealed" } });
      warehouse.sealMany(
        [input, good, alternate].map((bean) => [bean.id, {}]), null, digestBytes(new TextEncoder().encode("report")),
      );
      const material = cache.material(snapshot, "upper@1", { note: input.id }, {});
      const submit = (output: string) => {
        const job = cache.jobs.create({
          station: "Station:upper@1", epoch: snapshot.epoch, inputs: { note: [input.id] }, params: {},
        });
        cache.bind(job.id, material);
        return cache.jobs.transition(job.id, "SEALED", { outputs: { note: [output] } });
      };
      const missing = submit("missing");
      const notSealed = submit(unsealed.id);
      const first = submit(good.id);
      const second = submit(alternate.id);
      expect(cache.lookup(material.key())?.id).toBe(first.id);
      expectCode(() => cache.bind(first.id, material), "CACHE_BIND_CONFLICT");
      expect(cache.explain(first.id).hits).toEqual([missing.id, notSealed.id, first.id, second.id]);

      cache.invalidate("job", first.id, { reason: "rerun first", by: "operator" });
      expect(cache.lookup(material.key())?.id).toBe(second.id);
      cache.invalidate("bean", input.id, { reason: "rerun from material", by: "operator" });
      expect(cache.lookup(material.key())).toBeNull();
      const newer = submit(alternate.id);
      expect(cache.lookup(material.key())?.id).toBe(newer.id);
      context.db.exec("CREATE TABLE quarantines (bean_id TEXT PRIMARY KEY)");
      context.db.query("INSERT INTO quarantines (bean_id) VALUES (?)").run(alternate.id);
      expect(cache.lookup(material.key())).toBeNull();
      const expired = submit(good.id);
      expect(cache.lookup(material.key())?.id).toBe(expired.id);
      context.db.query("UPDATE beans SET expired_at = ? WHERE id = ?").run("2026-01-01T00:00:00.000Z", good.id);
      expect(cache.lookup(material.key())).toBeNull();
      expect(cache.list("upper@1")).toHaveLength(6);
      const audit = context.db.query<{ count: number }>("SELECT count(*) AS count FROM events WHERE kind = 'cache.invalidated'").get();
      expect(audit?.count).toBe(2);
      expectCode(() => cache.invalidate("station", "upper@1", { by: "operator", reason: "invalid scope value" }), "CACHE_INVALIDATION_INVALID");
    } finally {
      db.close();
      rmSync(root, { recursive: true, force: true, maxRetries: 4, retryDelay: 100 });
    }
  });
});
