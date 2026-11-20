import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { createHash } from "node:crypto";
import { cpSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Context, LineRunner, LocalProvider, RegistrySnapshotStore, Runner, compileRunPlan, loadSnapshot } from "../src/legacy";

const registryFixture = "tests/fixtures/registry-m2";

function referenceEpoch(root: string): string {
  const digests = new Map<string, string>();
  const visit = (prefix: string) => {
    for (const entry of readdirSync(join(root, prefix), { withFileTypes: true })) {
      const path = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) visit(path);
      if (entry.isFile()) digests.set(path, `sha256:${createHash("sha256").update(readFileSync(join(root, path))).digest("hex")}`);
    }
  };
  visit("");
  const sorted = Object.fromEntries([...digests.keys()].sort().map((path) => [path, digests.get(path)]));
  return `e-${createHash("sha256").update(JSON.stringify(sorted)).digest("hex").slice(0, 16)}`;
}

describe("Frozen Bun Registry snapshots", () => {
  test("matches an independent tree hash across directories and includes non-Spec files", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-epoch-tree-"));
    const first = join(root, "first");
    const second = join(root, "second");
    try {
      cpSync(registryFixture, first, { recursive: true });
      cpSync(registryFixture, second, { recursive: true });
      const a = await loadSnapshot(first);
      expect(a.epoch).toMatch(/^e-[0-9a-f]{16}$/);
      expect(a.epoch).toBe(referenceEpoch(first));
      expect((await loadSnapshot(second)).epoch).toBe(a.epoch);
      writeFileSync(join(second, "added.txt"), "contents outside the Registry schema\r\n");
      const changed = await loadSnapshot(second);
      expect(changed.epoch).not.toBe(a.epoch);
      expect(changed.epoch).toBe(referenceEpoch(second));
    } finally { rmSync(root, { recursive: true, force: true }); }
  });

  test("reopens the original documents, constitution and resolutions after the source Registry changes", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-epoch-reopen-"));
    const registry = join(root, "registry");
    const dbPath = join(root, "factory.db");
    cpSync(registryFixture, registry, { recursive: true });
    let db = new Database(dbPath);
    try {
      const initial = await new RegistrySnapshotStore(db).capture(registry);
      const originalConstitution = initial.result.registry.constitution;
      const originalResolutions = initial.result.resolutions;
      const upper = join(registry, "stations", "upper.yaml");
      const rules = join(registry, "constitution", "invariants.mrl");
      expect(initial.spec("Station:upper@1").limits.timeout).toBe("60s");
      expect(originalConstitution.length).toBeGreaterThan(0);
      expect(originalResolutions.length).toBeGreaterThan(0);
      const stored = db.query<{ payload: string }>("SELECT payload FROM registry_snapshots WHERE epoch = ?").get(initial.epoch)!;
      expect(stored.payload).not.toContain("mao-epoch-reopen-");

      db.close();
      db = new Database(dbPath);
      const snapshots = new RegistrySnapshotStore(db);
      writeFileSync(upper, readFileSync(upper, "utf8").replace("60s", "30s"));
      writeFileSync(rules, readFileSync(rules, "utf8") + "\n// Updated Registry\n");
      const current = await snapshots.capture(registry);
      expect(current.epoch).not.toBe(initial.epoch);
      expect(current.spec("Station:upper@1").limits.timeout).toBe("30s");
      const reopened = await snapshots.materialize(initial.epoch, registry);
      expect(reopened.spec("Station:upper@1").limits.timeout).toBe("60s");
      expect(reopened.result.registry.constitution).toEqual(originalConstitution);
      expect(reopened.result.resolutions).toEqual(originalResolutions);
      const corrupted = db.query("UPDATE registry_snapshots SET payload = payload || ' ' WHERE epoch = ?");
      corrupted.run(initial.epoch);
      await expect(snapshots.materialize(initial.epoch, registry)).rejects.toMatchObject({ code: "EPOCH_CORRUPT" });
    } finally { db.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("recovers an older unfrozen epoch only from matching bytes and rejects invalid capture", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-epoch-fallback-"));
    const registry = join(root, "registry");
    const db = new Database(":memory:");
    try {
      cpSync(registryFixture, registry, { recursive: true });
      const old = await loadSnapshot(registry);
      const snapshots = new RegistrySnapshotStore(db);
      await expect(snapshots.materialize(old.epoch)).rejects.toMatchObject({ code: "EPOCH_NOT_FOUND" });
      const extra = join(registry, "local.txt");
      writeFileSync(extra, "changed");
      await expect(snapshots.materialize(old.epoch, registry)).rejects.toMatchObject({ code: "EPOCH_MISMATCH" });
      expect(db.query("SELECT epoch FROM registry_snapshots").all()).toEqual([]);
      rmSync(extra);
      expect((await snapshots.materialize(old.epoch, registry)).docs).toEqual(old.docs);
      writeFileSync(join(registry, "stations", "upper.yaml"), "invalid: [");
      await expect(snapshots.capture(registry)).rejects.toMatchObject({ code: "REGISTRY_INVALID" });
      expect(db.query("SELECT epoch FROM registry_snapshots").all()).toEqual([{ epoch: old.epoch }]);
    } finally { db.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("rejects a Run tick from a LineRunner bound to a different epoch", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-epoch-run-"));
    const context = Context.open(root);
    try {
      const snapshots = new RegistrySnapshotStore(context.db);
      const bound = await snapshots.capture(registryFixture);
      const other = await loadSnapshot("spec/examples/registry");
      expect(other.epoch).not.toBe(bound.epoch);
      const provider = new LocalProvider();
      const lines = new LineRunner(new Runner(context, bound, provider));
      const plan = compileRunPlan(bound, "note-pipeline@1");
      const run = lines.runs.create(plan, { inputs: { task: "sealed-task" } });
      const wrong = new LineRunner(new Runner(context, other, provider));
      let failure: unknown;
      try { wrong.tick(run.id); } catch (error) { failure = error; }
      expect(failure).toMatchObject({ code: "EPOCH_MISMATCH" });
      expect(lines.runs.get(run.id).state).toBe("PENDING");
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });
});
