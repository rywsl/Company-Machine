import { expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { chmodSync, existsSync, lstatSync, mkdirSync, readdirSync, rmSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { createBackup, restoreBackup, verifyBackup } from "../src/backup";
import { SqliteStore } from "../src/db";
import { Context } from "../src/legacy/context";
import { runCommand, type CommandResult } from "../runtime/podman";

async function inTemporaryDirectory(run: (root: string) => Promise<void>): Promise<void> {
  const root = join("tmp", `backup-${randomUUID()}`);
  mkdirSync(root, { recursive: true });
  try { await run(root); }
  finally {
    if (process.platform === "win32") makeFixtureRemovable(root);
    for (let attempt = 1; ; attempt += 1) {
      try {
        rmSync(root, { recursive: true, force: true });
        break;
      } catch (error) {
        const code = (error as { code?: string }).code;
        if (attempt === 6 || process.platform !== "win32" || !["EBUSY", "EPERM", "ENOTEMPTY"].includes(code ?? "")) throw error;
        await new Promise((resolve) => setTimeout(resolve, attempt * 100));
      }
    }
  }
}

function makeFixtureRemovable(path: string): void {
  const info = lstatSync(path);
  if (info.isDirectory()) {
    for (const name of readdirSync(path) as string[]) makeFixtureRemovable(join(path, name));
  } else if (info.isFile() && (info.mode & 0o200) === 0) chmodSync(path, info.mode | 0o200);
}

async function git(root: string, ...args: string[]): Promise<string> {
  const result = await runCommand(["git", "-C", root, ...args], {
    env: { GIT_CONFIG_GLOBAL: process.platform === "win32" ? "NUL" : "/dev/null", GIT_CONFIG_NOSYSTEM: "1" },
  });
  const decoder = new TextDecoder();
  if (result.exitCode !== 0) throw new Error(decoder.decode(result.stderr));
  return decoder.decode(result.stdout).trimEnd();
}

function assertCliExit(operation: string, result: CommandResult, expected: number): void {
  if (result.exitCode === expected) return;
  const decoder = new TextDecoder();
  throw new Error(`factory backup ${operation} exited ${result.exitCode} (expected ${expected})\n${decoder.decode(result.stderr)}${decoder.decode(result.stdout)}`);
}

async function registryFixture(root: string): Promise<string> {
  mkdirSync(root);
  await git(root, "init", "--template=", "--initial-branch", "main");
  await Bun.write(join(root, "station.yaml"), "name: example\n");
  await git(root, "add", "station.yaml");
  await git(root, "-c", "user.name=Backup Test", "-c", "user.email=backup@example.test", "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Registry fixture");
  return git(root, "rev-parse", "HEAD");
}

test("backs up a shared WAL database and warehouse and restores business IDs and bytes", async () => inTemporaryDirectory(async (root) => {
  const source = join(root, "source");
  const destination = join(root, "backup");
  const restoreRoot = join(root, "restored");
  const context = Context.open(source);
  const dbPath = join(source, "factory.db");
  const store = new SqliteStore(dbPath, dbPath);
  try {
    await Bun.write(join(source, "input.txt"), "portable material\n");
    const bean = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": join(source, "input.txt") } });
    context.warehouse.sealMany([[bean.id, { chars: 17 }]], null, `sha256:${"a".repeat(64)}`);
    const system = store.createBusinessSystem({ name: "Sales" });
    const project = store.createIntake({ name: "Invoices", business_system_id: system.id, repo_url: "https://example.test/invoices" });
    const run = store.createBusinessRun(system.id)!;
    const request = store.createSecretRequest({ projectId: project.id, name: "PAYMENT_KEY", vaultRef: "vault://secret/sample", status: "available" });
    context.db.exec("CREATE TABLE job_execution_locks(job_id TEXT, pid INTEGER); INSERT INTO job_execution_locks VALUES ('job-a', 42)");
    await Bun.write(join(source, "vault.json"), "vault-data-not-for-backup");
    await Bun.write(join(source, "vault.key"), "vault-key-not-for-backup");
    await Bun.write(join(source, "secrets", "manifest.json"), JSON.stringify({ LEGACY_KEY: { status: "declared" } }));
    await Bun.write(join(source, "secrets", "values", "LEGACY_KEY"), "secret-not-for-backup");
    await Bun.write(join(source, "jobs", "job-a", "2", "logs", "stdout"), "attempt output\n");
    await Bun.write(join(source, "foreman", "sessions", "session-a", "logs", "trace", "events.log"), "session log\n");
    const manifest = await createBackup({ root: source, withLogs: true }, destination);
    expect(manifest).toMatchObject({ format: "mao-backup/bun/1", databases: { factory: "factory.db", business: "factory.db" }, secrets: ["LEGACY_KEY", "PAYMENT_KEY"], with_logs: true });
    expect(manifest.files.map(({ path }) => path)).toEqual([
      `cas/sha256/${bean.payload[0].digest.slice(7, 9)}/${bean.payload[0].digest.slice(9)}`,
      "factory.db", "logs/foreman/sessions/session-a/trace/events.log", "logs/jobs/job-a/2/stdout",
    ]);
    expect(await verifyBackup(destination)).toEqual({ ok: true, problems: [] });

    const restored = await restoreBackup(destination, restoreRoot);
    expect(restored.secrets_missing).toEqual(manifest.secrets);
    expect(existsSync(join(restoreRoot, "vault.key"))).toBe(false);
    expect(await Bun.file(join(restoreRoot, "jobs", "job-a", "2", "logs", "stdout")).text()).toBe("attempt output\n");
    expect(await Bun.file(join(restoreRoot, "foreman", "sessions", "session-a", "logs", "trace", "events.log")).text()).toBe("session log\n");
    const restoredContext = Context.open(restoreRoot);
    const restoredStore = new SqliteStore(restored.factory_database, restored.factory_database);
    try {
      expect(restoredStore.listBusinessSystems().map(({ id }) => id)).toEqual([system.id]);
      expect(restoredStore.listBusinessUnits(system.id).map(({ id }) => id)).toEqual(store.listBusinessUnits(system.id).map(({ id }) => id));
      expect(restoredStore.listBusinessRuns(system.id).map(({ id }) => id)).toEqual([run.id]);
      expect(restoredStore.getSecretRequest(request.id)?.status).toBe("requested");
      expect(store.getSecretRequest(request.id)?.status).toBe("available");
      expect(restoredContext.db.query("SELECT * FROM job_execution_locks").all()).toEqual([]);
      await restoredContext.warehouse.materialize(bean.id, join(root, "materialized"), "port");
      expect(await Bun.file(join(root, "materialized", "note.txt")).text()).toBe("portable material\n");
      expect(await restoredContext.warehouse.verify()).toEqual([]);
    } finally {
      restoredStore.close();
      restoredContext.close();
    }
  } finally {
    store.close();
    context.close();
  }
}));

test("reports corrupt and missing blobs, extra files, and unsafe paths before restore", async () => inTemporaryDirectory(async (root) => {
  const source = join(root, "source");
  const destination = join(root, "backup");
  const restoreRoot = join(root, "restored");
  const context = Context.open(source);
  try {
    await Bun.write(join(root, "input.txt"), "test");
    const bean = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": join(root, "input.txt") } });
    const manifest = await createBackup({ root: source }, destination);
    const blob = manifest.files.find(({ path }) => path.startsWith("cas/"))!;
    await Bun.write(join(destination, blob.path), "bad!");
    await Bun.write(join(destination, "extra.txt"), "extra");
    expect(await verifyBackup(destination)).toMatchObject({ ok: false, problems: [
      { kind: "blob", path: bean.payload[0].digest }, { kind: "extra", path: "extra.txt" }, { kind: "mismatch", path: blob.path },
    ] });
    await expect(restoreBackup(destination, restoreRoot)).rejects.toMatchObject({ code: "BACKUP_CORRUPT" });
    expect(existsSync(restoreRoot)).toBe(false);
    unlinkSync(join(destination, blob.path));
    expect((await verifyBackup(destination)).problems).toContainEqual({ kind: "missing", path: blob.path });
    await Bun.write(join(destination, "manifest.json"), JSON.stringify({ ...manifest, files: [...manifest.files, { ...blob, path: "../input.txt" }] }));
    expect(await verifyBackup(destination)).toEqual({ ok: false, problems: [{ kind: "manifest", path: "manifest.json" }] });
    await expect(createBackup({ root: source }, destination)).rejects.toMatchObject({ code: "BACKUP_TARGET_NOT_EMPTY" });
    await expect(restoreBackup(destination, source)).rejects.toMatchObject({ code: "BACKUP_TARGET_NOT_EMPTY" });
    expect(await Bun.file(context.warehouse.blobPath(bean.payload[0].digest)).text()).toBe("test");
  } finally { context.close(); }
}));

test("backs up split business and warehouse databases and recovers their separate paths", async () => inTemporaryDirectory(async (root) => {
  const source = join(root, "source");
  const destination = join(root, "backup");
  const restoreRoot = join(root, "restored");
  const dedicated = join(root, "business.sqlite");
  const context = Context.open(source);
  const store = new SqliteStore(dedicated, join(source, "factory.db"));
  try {
    const system = store.createBusinessSystem({ name: "Operations" });
    mkdirSync(destination);
    const manifest = await createBackup({ root: source, businessDatabase: dedicated }, destination);
    expect(manifest.databases).toEqual({ factory: "factory.db", business: "business.db" });
    expect(await verifyBackup(destination)).toEqual({ ok: true, problems: [] });
    const restored = await restoreBackup(destination, restoreRoot);
    const restoredStore = new SqliteStore(restored.business_database!, restored.factory_database);
    try { expect(restoredStore.listBusinessSystems().map(({ id }) => id)).toEqual([system.id]); }
    finally { restoredStore.close(); }
  } finally {
    store.close();
    context.close();
  }
}));

test("keeps expired Bean history after blob collection and rejects a missing live blob", async () => inTemporaryDirectory(async (root) => {
  const source = join(root, "source");
  const destination = join(root, "backup");
  const restoreRoot = join(root, "restored");
  const context = Context.open(source);
  try {
    const input = join(root, "input.txt");
    await Bun.write(input, "expired bytes");
    const bean = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": input } });
    unlinkSync(context.warehouse.blobPath(bean.payload[0].digest));
    await expect(createBackup({ root: source }, destination)).rejects.toMatchObject({ code: "BACKUP_SOURCE_CORRUPT" });
    expect(existsSync(destination)).toBe(false);
    const expiredAt = "2026-01-02T03:04:00Z";
    context.db.query("UPDATE beans SET expired_at=? WHERE id=?").run(expiredAt, bean.id);
    const manifest = await createBackup({ root: source }, destination);
    expect(manifest.files.map(({ path }) => path)).toEqual(["factory.db"]);
    expect(await verifyBackup(destination)).toEqual({ ok: true, problems: [] });
    await restoreBackup(destination, restoreRoot);
    const restored = Context.open(restoreRoot);
    try {
      expect(restored.warehouse.get(bean.id).expiredAt).toBe(expiredAt);
      await expect(restored.warehouse.materialize(bean.id, join(root, "materialized"), "port")).rejects.toMatchObject({ code: "BEAN_EXPIRED" });
    } finally { restored.close(); }
  } finally { context.close(); }
}));

test("creates, verifies and restores a checkpoint through the factory CLI", { timeout: 120_000 }, async () => inTemporaryDirectory(async (root) => {
  const source = join(root, "source");
  const destination = join(root, "backup");
  const restoreRoot = join(root, "restored");
  const registry = join(root, "example registry");
  const oid = await registryFixture(registry);
  const database = join(source, "factory.db");
  const store = new SqliteStore(database, database);
  try {
    const system = store.createBusinessSystem({ name: "CLI restore" });
    const run = (args: string[]) => runCommand([Bun.argv[0], join(import.meta.dir, "../src/cli.ts"), "backup", ...args], {
      env: { BUSINESS_DB_PATH: database, FACTORY_DB_PATH: database },
    });
    const json = (bytes: Uint8Array) => JSON.parse(new TextDecoder().decode(bytes));
    const created = await run(["create", destination, "--root", source, "--registry", registry, "--json"]);
    assertCliExit("create", created, 0);
    expect(json(created.stdout)).toMatchObject({ format: "mao-backup/bun/1", registry: { head_oid: oid } });
    const verify = await run(["verify", destination, "--json"]);
    assertCliExit("verify", verify, 0);
    expect(json(verify.stdout)).toEqual({ ok: true, problems: [] });
    const restored = await run(["restore", destination, "--root", restoreRoot, "--json"]);
    assertCliExit("restore --root", restored, 0);
    const report = json(restored.stdout);
    expect(report.business_database).toBe(report.factory_database);
    expect(await git(report.registry, "rev-parse", "HEAD")).toBe(oid);
    const positional = await run(["restore", destination, join(root, "restored-positional"), "--json"]);
    assertCliExit("restore", positional, 0);
    expect(await git(json(positional.stdout).registry, "rev-parse", "HEAD")).toBe(oid);
    const result = new SqliteStore(report.business_database, report.factory_database);
    try { expect(result.listBusinessSystems().map(({ id }) => id)).toEqual([system.id]); }
    finally { result.close(); }
    await Bun.write(join(destination, "extra.txt"), "extra");
    const corrupt = await run(["verify", destination, "--json"]);
    assertCliExit("verify corrupt backup", corrupt, 1);
    expect(json(corrupt.stdout)).toMatchObject({ ok: false, problems: [{ kind: "extra", path: "extra.txt" }] });
  } finally { store.close(); }
}));

test("restores a Registry Git bundle including HEAD, tags and factory refs", { timeout: 120_000 }, async () => inTemporaryDirectory(async (root) => {
  const source = join(root, "live");
  const registry = join(root, "source registry");
  const destination = join(root, "backup");
  const restoreRoot = join(root, "restored");
  const oid = await registryFixture(registry);
  await git(registry, "tag", "release-1");
  await git(registry, "update-ref", "refs/factory/active", oid);
  await git(registry, "update-ref", "refs/changesets/review", oid);
  await Bun.write(join(registry, "untracked.txt"), "keep local\n");
  Context.open(source).close();
  const manifest = await createBackup({ root: source, registry }, destination);
  expect(manifest.registry).toMatchObject({
    bundle: "registry.bundle", head: "refs/heads/main", head_oid: oid,
    refs: { "refs/heads/main": oid, "refs/tags/release-1": oid, "refs/factory/active": oid, "refs/changesets/review": oid },
  });
  expect(await verifyBackup(destination)).toEqual({ ok: true, problems: [] });
  const restored = await restoreBackup(destination, restoreRoot);
  expect(await git(restored.registry!, "symbolic-ref", "HEAD")).toBe("refs/heads/main");
  expect(await git(restored.registry!, "rev-parse", "refs/factory/active")).toBe(oid);
  expect(await git(restored.registry!, "rev-parse", "refs/tags/release-1")).toBe(oid);
  expect(await Bun.file(join(restored.registry!, "station.yaml")).text()).toBe("name: example\n");
  expect(existsSync(join(restored.registry!, "untracked.txt"))).toBe(false);
  expect(existsSync(join(restoreRoot, "registry.bundle"))).toBe(false);
  expect(existsSync(join(restored.registry!, ".git", "FETCH_HEAD"))).toBe(false);

  manifest.registry!.refs["refs/factory/active"] = "b".repeat(oid.length);
  await Bun.write(join(destination, "manifest.json"), JSON.stringify(manifest));
  expect(await verifyBackup(destination)).toEqual({ ok: false, problems: [{ kind: "bundle", path: "registry.bundle" }] });
  const invalidTarget = join(root, "invalid-restore");
  await expect(restoreBackup(destination, invalidTarget)).rejects.toMatchObject({ code: "BACKUP_CORRUPT" });
  expect(existsSync(invalidTarget)).toBe(false);
}));

test("recovers a detached Registry HEAD whose commit has no named ref", { timeout: 120_000 }, async () => inTemporaryDirectory(async (root) => {
  const source = join(root, "live");
  const registry = join(root, "source-registry");
  const destination = join(root, "backup");
  const oid = await registryFixture(registry);
  await git(registry, "checkout", "--detach", oid);
  await git(registry, "update-ref", "-d", "refs/heads/main");
  Context.open(source).close();
  const manifest = await createBackup({ root: source, registry }, destination);
  expect(manifest.registry).toMatchObject({ head: null, head_oid: oid, refs: {} });
  const restored = await restoreBackup(destination, join(root, "restored"));
  expect(await git(restored.registry!, "rev-parse", "HEAD")).toBe(oid);
  const symbolic = await runCommand(["git", "-C", restored.registry!, "symbolic-ref", "--quiet", "HEAD"]);
  expect(symbolic.exitCode).toBe(1);
}));
