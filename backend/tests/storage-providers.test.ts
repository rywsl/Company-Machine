import { expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createApp } from "../src/server";
import { OpenBaoKmsProvider } from "../src/storage-openbao";
import { S3BlobProvider } from "../src/storage-s3";
import { resolveStorageProviderConfig, SqliteStateProvider } from "../src/providers";
import { bindSqlParameters, MysqlStateProvider, PostgresStateProvider } from "../src/storage-sql";
import { ManagedDatabase } from "../src/sqlite";
import { createStorageProviders } from "../src/storage-factory";
import { migrateStorage, readActiveStoragePointer } from "../src/storage-migration";
import { VaultStore } from "../src/vault";

test("local storage configuration never selects external providers", () => {
  expect(resolveStorageProviderConfig({} as NodeJS.ProcessEnv)).toEqual({ mode: "local", state: "sqlite", blob: "local", secret: "local" });
  expect(resolveStorageProviderConfig({ MAO_STORAGE_MODE: "production" } as NodeJS.ProcessEnv)).toEqual({ mode: "production", state: "postgres", blob: "s3", secret: "openbao" });
});

test("OpenBao KV and transit use the HTTP API without exposing the token", async () => {
  const observed: Request[] = [];
  const server = Bun.serve({ port: 0, async fetch(request) {
    observed.push(request);
    const path = new URL(request.url).pathname;
    if (path === "/v1/secret/data/example" && request.method === "PUT") return Response.json({ data: {} });
    if (path === "/v1/secret/data/example") return Response.json({ data: { data: { value: "value" } } });
    if (path === "/v1/transit/encrypt/default") return Response.json({ data: { ciphertext: "vault:v1:opaque" } });
    if (path === "/v1/transit/decrypt/default") return Response.json({ data: { plaintext: Buffer.from("plain").toString("base64") } });
    if (path === "/v1/transit/sign/default") return Response.json({ data: { signature: "vault:v1:signature" } });
    if (path === "/v1/transit/hmac/default") return Response.json({ data: { hmac: "vault:v1:hmac" } });
    if (path === "/v1/transit/keys/default/rotate") return Response.json({ data: { latest_version: 2 } });
    return Response.json({ errors: ["missing"] }, { status: 404 });
  }});
  try {
    const provider = new OpenBaoKmsProvider({ addr: `http://127.0.0.1:${server.port}`, token: "token-that-must-not-be-logged" });
    await provider.put("vault://secret/example", "value");
    expect(await provider.get("vault://secret/example")).toBe("value");
    const encrypted = await provider.encrypt("plain");
    expect(encrypted.ciphertext).toBe("vault:v1:opaque");
    expect(Buffer.from(await provider.decrypt(encrypted)).toString()).toBe("plain");
    expect(await provider.sign("plain")).toBe("vault:v1:signature");
    expect(await provider.hmac("plain")).toBe("vault:v1:hmac");
    expect(await provider.rotate()).toBe("2");
    expect(observed.length).toBe(7);
    expect(observed).toHaveLength(7);
  } finally { server.stop(); }
});

test("S3 provider reports missing credentials without making a request", async () => {
  const provider = new S3BlobProvider({ endpoint: "http://127.0.0.1:1", bucket: "mao" });
  expect(provider.status()).toMatchObject({ kind: "s3", available: false, code: "PROVIDER_CONFIG_INVALID" });
  await expect(provider.put(new Uint8Array([1, 2, 3]))).rejects.toMatchObject({ code: "PROVIDER_CONFIG_INVALID" });
});

test("S3 provider performs idempotent CAS writes, HEAD, GET and digest verification", async () => {
  const objects = new Map<string, Uint8Array>();
  const calls: string[] = [];
  const server = Bun.serve({ port: 0, async fetch(request) {
    const key = new URL(request.url).pathname;
    calls.push(request.method + " " + key);
    if (request.method === "PUT") { objects.set(key, new Uint8Array(await request.arrayBuffer())); return new Response(null, { status: 200 }); }
    if (request.method === "HEAD") {
      const body = objects.get(key);
      return body ? new Response(null, { status: 200, headers: { "content-length": String(body.length), "x-amz-meta-sha256": createHash("sha256").update(body).digest("hex") } }) : new Response(null, { status: 404 });
    }
    if (request.method === "GET") {
      const body = objects.get(key);
      return body ? new Response(body as unknown as BodyInit) : new Response(null, { status: 404 });
    }
    return new Response(null, { status: 405 });
  }});
  const root = mkdtempSync(join(tmpdir(), "mao-s3-cache-"));
  const environment = { MAO_S3_ACCESS_KEY: "access", MAO_S3_SECRET_KEY: "secret" } as NodeJS.ProcessEnv;
  try {
    const provider = new S3BlobProvider({ endpoint: `http://127.0.0.1:${server.port}`, bucket: "mao", cacheRoot: root, environment });
    const digest = await provider.put(new Uint8Array([7, 8, 9]));
    expect(await provider.put(new Uint8Array([7, 8, 9]))).toBe(digest);
    const fresh = new S3BlobProvider({ endpoint: `http://127.0.0.1:${server.port}`, bucket: "mao", cacheRoot: join(root, "fresh"), environment });
    expect(await fresh.get(digest)).toEqual(new Uint8Array([7, 8, 9]));
    expect(calls.filter((call) => call.startsWith("PUT "))).toHaveLength(1);
    expect(calls.filter((call) => call.startsWith("HEAD "))).toHaveLength(2);
    expect(calls.filter((call) => call.startsWith("GET "))).toHaveLength(1);
    expect((await fresh.verify([digest])).length).toBe(0);
  } finally {
    server.stop();
    rmSync(root, { recursive: true, force: true });
  }
});

test("local app exposes provider status without changing the existing Vault path", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-storage-app-"));
  const database = join(root, "factory.db");
  const app = createApp({ storageRoot: root, dbPath: database, legacyPath: database, dispatchWork: false });
  try {
    const response = await app.fetch(new Request("http://localhost/api/v1/storage"));
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ mode: "local", state: { kind: "sqlite", available: true }, blob: { kind: "local", available: true }, secret: { kind: "local", available: true }, kms: { kind: "local-vault", available: true } });
  } finally {
    app.stop();
    rmSync(root, { recursive: true, force: true });
  }
});

test("SQL providers fail closed when their connection environment is absent", () => {
  expect(new PostgresStateProvider().status()).toMatchObject({ kind: "postgres", available: false, code: "PROVIDER_UNAVAILABLE" });
  expect(new MysqlStateProvider().status()).toMatchObject({ kind: "mysql", available: false, code: "PROVIDER_UNAVAILABLE" });
});

test("SQLite state migrations are transactional and idempotent", () => {
  const provider = new SqliteStateProvider(":memory:");
  try {
    const migration = "CREATE TABLE IF NOT EXISTS migration_probe (id TEXT PRIMARY KEY)";
    expect(provider.migrate([migration])).toBe(1);
    expect(provider.migrate([migration])).toBe(1);
    expect(provider.query("SELECT name FROM sqlite_master WHERE name='migration_probe'")).toHaveLength(1);
  } finally { provider.close(); }
});

test("SQL placeholder binding preserves literals and comments", () => {
  expect(bindSqlParameters("SELECT '?' AS literal, ? AS value -- ?\n/* ? */", ["O'Reilly"]))
    .toBe("SELECT '?' AS literal, 'O''Reilly' AS value -- ?\n/* ? */");
});

test("local migration copies rows, CAS bytes and secret references before switching", async () => {
  const sourceRoot = mkdtempSync(join(tmpdir(), "mao-storage-source-"));
  const targetRoot = mkdtempSync(join(tmpdir(), "mao-storage-target-"));
  const sourceDatabase = join(sourceRoot, "factory.db");
  const sourceDb = new ManagedDatabase(sourceDatabase, { create: true });
  sourceDb.exec("CREATE TABLE records (id TEXT PRIMARY KEY, body TEXT NOT NULL)");
  sourceDb.query("INSERT INTO records(id,body) VALUES (?,?)").run("one", "payload");
  sourceDb.close();
  const sourceVault = new VaultStore(join(sourceRoot, "vault.json"), join(sourceRoot, "vault.key"));
  sourceVault.put("vault://project/API_KEY", "secret-value");
  const sourceBlob = createStorageProviders({ root: sourceRoot, env: {} as NodeJS.ProcessEnv });
  await sourceBlob.blob.put(new Uint8Array([1, 2, 3]));
  const target = createStorageProviders({ root: targetRoot, env: {} as NodeJS.ProcessEnv });
  try {
    const report = await migrateStorage({ root: sourceRoot, target, sourceVault });
    expect(report.switched).toBe(true);
    expect(report.tables).toHaveLength(1);
    expect(report.tables[0]).toMatchObject({ name: "records", rows: 1 });
    expect(typeof report.tables[0]?.digest).toBe("string");
    expect(target.state.query<{ id: string; body: string }>("SELECT id,body FROM records")).toEqual([{ id: "one", body: "payload" }]);
    expect(await target.secret.has("vault://project/API_KEY")).toBe(true);
    expect(readActiveStoragePointer(sourceRoot)).toMatchObject({ schema: "mao.storage-active/1", migration_digest: report.digest });
  } finally {
    target.state.close();
    sourceBlob.state.close();
    rmSync(sourceRoot, { recursive: true, force: true });
    rmSync(targetRoot, { recursive: true, force: true });
  }
});

test("dry-run migration never writes the active provider pointer", async () => {
  const sourceRoot = mkdtempSync(join(tmpdir(), "mao-storage-dry-source-"));
  const targetRoot = mkdtempSync(join(tmpdir(), "mao-storage-dry-target-"));
  const database = join(sourceRoot, "factory.db");
  const sourceDb = new ManagedDatabase(database, { create: true });
  sourceDb.exec("CREATE TABLE records (id TEXT PRIMARY KEY)");
  sourceDb.close();
  const target = createStorageProviders({ root: targetRoot, env: {} as NodeJS.ProcessEnv });
  try {
    const report = await migrateStorage({ root: sourceRoot, target, dryRun: true });
    expect(report.dryRun).toBe(true);
    expect(report.switched).toBe(false);
    expect(readActiveStoragePointer(sourceRoot)).toBeUndefined();
  } finally {
    target.state.close();
    rmSync(sourceRoot, { recursive: true, force: true });
    rmSync(targetRoot, { recursive: true, force: true });
  }
});

test("migration verification failure leaves the active provider pointer untouched", async () => {
  const sourceRoot = mkdtempSync(join(tmpdir(), "mao-storage-fail-source-"));
  const targetRoot = mkdtempSync(join(tmpdir(), "mao-storage-fail-target-"));
  const database = join(sourceRoot, "factory.db");
  const sourceDb = new ManagedDatabase(database, { create: true });
  sourceDb.exec("CREATE TABLE records (id TEXT PRIMARY KEY)");
  sourceDb.close();
  const target = createStorageProviders({ root: targetRoot, env: {} as NodeJS.ProcessEnv });
  const failingState = Object.create(target.state) as typeof target.state;
  failingState.verify = () => [{ kind: "forced_failure" }];
  const failingTarget = { ...target, state: failingState } as typeof target;
  try {
    await expect(migrateStorage({ root: sourceRoot, target: failingTarget })).rejects.toMatchObject({ code: "PROVIDER_UNAVAILABLE" });
    expect(readActiveStoragePointer(sourceRoot)).toBeUndefined();
  } finally {
    target.state.close();
    rmSync(sourceRoot, { recursive: true, force: true });
    rmSync(targetRoot, { recursive: true, force: true });
  }
});

test("production mode fails before creating a local business database", () => {
  const root = mkdtempSync(join(tmpdir(), "mao-production-guard-"));
  const names = ["MAO_STORAGE_MODE", "FACTORY_ROOT", "FACTORY_DB_PATH", "BUSINESS_DB_PATH"];
  const previous = Object.fromEntries(names.map((name) => [name, process.env[name]]));
  try {
    process.env.MAO_STORAGE_MODE = "production";
    process.env.FACTORY_ROOT = root;
    process.env.FACTORY_DB_PATH = join(root, "factory.db");
    process.env.BUSINESS_DB_PATH = join(root, "business.db");
    expect(() => createApp({ storageRoot: root, dispatchWork: false })).toThrow(/provider/i);
    expect(existsSync(join(root, "factory.db"))).toBe(false);
    expect(existsSync(join(root, "business.db"))).toBe(false);
  } finally {
    for (const name of names) {
      const value = previous[name];
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
    rmSync(root, { recursive: true, force: true });
  }
});
