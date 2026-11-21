import { expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createApp } from "../src/server";
import { OpenBaoKmsProvider } from "../src/storage-openbao";
import { S3BlobProvider } from "../src/storage-s3";
import { resolveStorageProviderConfig } from "../src/providers";
import { MysqlStateProvider, PostgresStateProvider } from "../src/storage-sql";

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
    return Response.json({ errors: ["missing"] }, { status: 404 });
  }});
  try {
    const provider = new OpenBaoKmsProvider({ addr: `http://127.0.0.1:${server.port}`, token: "token-that-must-not-be-logged" });
    await provider.put("vault://secret/example", "value");
    expect(await provider.get("vault://secret/example")).toBe("value");
    const encrypted = await provider.encrypt("plain");
    expect(encrypted.ciphertext).toBe("vault:v1:opaque");
    expect(Buffer.from(await provider.decrypt(encrypted)).toString()).toBe("plain");
    expect(observed.length).toBe(4);
    expect(observed).toHaveLength(4);
  } finally { server.stop(); }
});

test("S3 provider reports missing credentials without making a request", async () => {
  const provider = new S3BlobProvider({ endpoint: "http://127.0.0.1:1", bucket: "mao" });
  expect(provider.status()).toMatchObject({ kind: "s3", available: false, code: "PROVIDER_CONFIG_INVALID" });
  await expect(provider.put(new Uint8Array([1, 2, 3]))).rejects.toMatchObject({ code: "PROVIDER_CONFIG_INVALID" });
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
