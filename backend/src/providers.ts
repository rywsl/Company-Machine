import { createHash, createHmac, createCipheriv, createDecipheriv, randomBytes } from "node:crypto";
import { mkdirSync, readFileSync, readdirSync, renameSync, rmSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { mkdir, open, readFile, readdir, rename, rm, stat, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { Database } from "bun:sqlite";
import { ManagedDatabase } from "./sqlite";
import { VaultStore } from "./vault";

export type StorageMode = "local" | "production";
export type StateProviderKind = "sqlite" | "postgres" | "mysql";
export type BlobProviderKind = "local" | "s3";
export type SecretProviderKind = "local" | "openbao";

export type ProviderErrorCode = "PROVIDER_UNAVAILABLE" | "PROVIDER_CONFIG_INVALID" | "CAS_CONFLICT" | "CAS_MISSING";

/** A stable error boundary for provider failures. Secret values are never copied into details. */
export class ProviderError extends Error {
  readonly name = "ProviderError";
  constructor(readonly code: ProviderErrorCode, message: string, readonly details: Record<string, unknown> = {}) {
    super(message);
  }
}

export interface ProviderStatus {
  kind: string;
  mode: StorageMode;
  available: boolean;
  code?: ProviderErrorCode;
  reason?: string;
}

export interface StateProvider {
  readonly kind: StateProviderKind;
  readonly database?: Database;
  query<T = Record<string, unknown>>(sql: string, params?: readonly unknown[]): T[];
  execute(sql: string, params?: readonly unknown[]): { changes: number; lastInsertRowid?: number };
  transaction<T>(work: () => T): T;
  migrate(): number;
  status(): ProviderStatus;
  close(): void;
}

export type BlobInput = string | Uint8Array | ArrayBuffer;
export interface BlobHead { digest: string; size: number; }
export interface BlobProvider {
  readonly kind: BlobProviderKind;
  put(input: BlobInput): Promise<string>;
  putFile(path: string): Promise<string>;
  get(digest: string): Promise<Uint8Array>;
  getFile(digest: string, destination: string): Promise<void>;
  head(digest: string): Promise<BlobHead | undefined>;
  exists(digest: string): Promise<boolean>;
  blobPath(digest: string): string;
  verify(): Promise<Array<Record<string, string>>>;
  status(): ProviderStatus;
}

export interface SecretProvider {
  readonly kind: SecretProviderKind;
  put(ref: string, value: string): Promise<void>;
  get(ref: string): Promise<string | undefined>;
  has(ref: string): Promise<boolean>;
  delete(ref: string): Promise<boolean>;
  status(): ProviderStatus;
}

export interface EncryptedValue {
  keyId: string;
  algorithm: "aes-256-gcm";
  iv: string;
  tag: string;
  ciphertext: string;
}

export interface KmsProvider {
  readonly kind: string;
  encrypt(value: Uint8Array | string, keyId?: string): Promise<EncryptedValue>;
  decrypt(value: EncryptedValue): Promise<Uint8Array>;
  sign(value: Uint8Array | string, keyId?: string): Promise<string>;
  hmac(value: Uint8Array | string, keyId?: string): Promise<string>;
  rotate(keyId?: string): Promise<string>;
  auditRef(operation: string, keyId?: string): string;
  status(): ProviderStatus;
}

export interface StorageProviderConfig {
  mode: StorageMode;
  state: StateProviderKind;
  blob: BlobProviderKind;
  secret: SecretProviderKind;
}

const envValue = (env: NodeJS.ProcessEnv, name: string): string | undefined => {
  const value = env[name];
  return value && value.trim() ? value.trim() : undefined;
};

export function resolveStorageProviderConfig(env: NodeJS.ProcessEnv = process.env): StorageProviderConfig {
  const modeValue = envValue(env, "MAO_STORAGE_MODE") ?? "local";
  if (modeValue !== "local" && modeValue !== "production") {
    throw new ProviderError("PROVIDER_CONFIG_INVALID", "MAO_STORAGE_MODE must be local or production", { name: "MAO_STORAGE_MODE" });
  }
  const state = envValue(env, "MAO_STATE_PROVIDER") ?? "sqlite";
  const blob = envValue(env, "MAO_BLOB_PROVIDER") ?? "local";
  const secret = envValue(env, "MAO_SECRET_PROVIDER") ?? (modeValue === "production" ? "openbao" : "local");
  if (!["sqlite", "postgres", "mysql"].includes(state)) throw new ProviderError("PROVIDER_CONFIG_INVALID", "MAO_STATE_PROVIDER is invalid", { name: "MAO_STATE_PROVIDER" });
  if (!["local", "s3"].includes(blob)) throw new ProviderError("PROVIDER_CONFIG_INVALID", "MAO_BLOB_PROVIDER is invalid", { name: "MAO_BLOB_PROVIDER" });
  if (!["local", "openbao"].includes(secret)) throw new ProviderError("PROVIDER_CONFIG_INVALID", "MAO_SECRET_PROVIDER is invalid", { name: "MAO_SECRET_PROVIDER" });
  return { mode: modeValue, state: state as StateProviderKind, blob: blob as BlobProviderKind, secret: secret as SecretProviderKind };
}

function digestBytes(bytes: Uint8Array): string {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}
function checkDigest(digest: string): string {
  if (!/^sha256:[0-9a-f]{64}$/.test(digest)) throw new ProviderError("CAS_MISSING", "blob digest is invalid", { digest });
  return digest.slice("sha256:".length);
}
function bytes(input: BlobInput): Uint8Array {
  if (typeof input === "string") return new Uint8Array(readFileSync(input));
  return input instanceof ArrayBuffer ? new Uint8Array(input) : input;
}

/** Local content-addressed storage. It performs no network access. */
export class LocalBlobProvider implements BlobProvider {
  readonly kind = "local" as const;
  private readonly root: string;
  constructor(root = join(process.env.FACTORY_ROOT ?? ".factory", "cas")) {
    this.root = resolve(root);
    mkdirSync(this.root, { recursive: true });
  }
  blobPath(digest: string): string {
    const hex = checkDigest(digest);
    return join(this.root, "sha256", hex.slice(0, 2), hex.slice(2));
  }
  async put(input: BlobInput): Promise<string> {
    const data = bytes(input);
    const digest = digestBytes(data);
    const destination = this.blobPath(digest);
    if (statSync(destination, { throwIfNoEntry: false })?.isFile()) {
      const existing = new Uint8Array(await readFile(destination));
      if (digestBytes(existing) !== digest) throw new ProviderError("CAS_CONFLICT", "existing blob does not match its digest", { digest });
      return digest;
    }
    await mkdir(dirname(destination), { recursive: true });
    const temporary = `${destination}.${cryptoRandomUuid()}.part`;
    await writeFile(temporary, data, { flag: "wx" });
    try {
      await rename(temporary, destination);
    } catch (error) {
      if ((error as { code?: string }).code === "EEXIST") await rm(temporary, { force: true });
      else { await rm(temporary, { force: true }); throw error; }
    }
    return digest;
  }
  async putFile(path: string): Promise<string> { return this.put(path); }
  async get(digest: string): Promise<Uint8Array> {
    const path = this.blobPath(digest);
    try {
      const data = new Uint8Array(await readFile(path));
      if (digestBytes(data) !== digest) throw new ProviderError("CAS_CONFLICT", "blob digest verification failed", { digest });
      return data;
    } catch (error) {
      if (error instanceof ProviderError) throw error;
      if ((error as { code?: string }).code === "ENOENT") throw new ProviderError("CAS_MISSING", "blob is missing", { digest });
      throw error;
    }
  }
  async getFile(digest: string, destination: string): Promise<void> {
    const data = await this.get(digest);
    await mkdir(dirname(resolve(destination)), { recursive: true });
    await writeFile(destination, data);
  }
  async head(digest: string): Promise<BlobHead | undefined> {
    const path = this.blobPath(digest);
    try { const info = await stat(path); return info.isFile() ? { digest, size: info.size } : undefined; }
    catch (error) { if ((error as { code?: string }).code === "ENOENT") return undefined; throw error; }
  }
  async exists(digest: string): Promise<boolean> { return Boolean(await this.head(digest)); }
  async verify(): Promise<Array<Record<string, string>>> {
    const problems: Array<Record<string, string>> = [];
    const base = join(this.root, "sha256");
    const walk = async (directory: string): Promise<void> => {
      let entries: Awaited<ReturnType<typeof readdir>>;
      try { entries = await readdir(directory, { withFileTypes: true }); } catch (error) { if ((error as { code?: string }).code === "ENOENT") return; throw error; }
      for (const entry of entries) {
        const path = join(directory, entry.name);
        if (entry.isDirectory()) await walk(path);
        else if (entry.isFile()) {
          const relative = path.slice(base.length + 1).replaceAll("\\", "/");
          const parts = relative.split("/");
          const expected = parts.length === 2 ? `sha256:${parts[0]}${parts[1]}` : "sha256:invalid";
          const actual = digestBytes(new Uint8Array(await readFile(path)));
          if (actual !== expected) problems.push({ kind: "digest_mismatch", path, digest: expected });
        }
      }
    };
    await walk(base);
    return problems;
  }
  status(): ProviderStatus { return { kind: this.kind, mode: "local", available: true }; }
}

export class SqliteStateProvider implements StateProvider {
  readonly kind = "sqlite" as const;
  readonly database: ManagedDatabase;
  constructor(path = process.env.BUSINESS_DB_PATH ?? join(process.env.FACTORY_ROOT ?? ".factory", "factory.db")) {
    if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true });
    this.database = new ManagedDatabase(path, { create: true });
    this.database.exec("PRAGMA busy_timeout = 5000");
    this.database.exec("PRAGMA journal_mode = WAL");
  }
  query<T = Record<string, unknown>>(sql: string, params: readonly unknown[] = []): T[] { return this.database.query<T>(sql).all(...params); }
  execute(sql: string, params: readonly unknown[] = []): { changes: number; lastInsertRowid?: number } {
    const result = this.database.query(sql).run(...params);
    return { changes: result.changes, lastInsertRowid: Number(result.lastInsertRowid) };
  }
  transaction<T>(work: () => T): T { return this.database.transaction(work).immediate(); }
  migrate(): number { return Number(this.database.query<{ user_version: number }>("PRAGMA user_version").get()?.user_version ?? 0); }
  status(): ProviderStatus { return { kind: this.kind, mode: "local", available: true }; }
  close(): void { this.database.close(); }
}

export class LocalSecretProvider implements SecretProvider {
  readonly kind = "local" as const;
  constructor(readonly vault = new VaultStore()) {}
  async put(ref: string, value: string): Promise<void> { this.vault.put(ref, value); }
  async get(ref: string): Promise<string | undefined> { return this.vault.get(ref); }
  async has(ref: string): Promise<boolean> { return this.vault.has(ref); }
  async delete(ref: string): Promise<boolean> { return this.vault.delete(ref); }
  status(): ProviderStatus { return { kind: this.kind, mode: "local", available: true }; }
}

function cryptoRandomUuid(): string {
  return randomBytes(16).toString("hex");
}

/** KMS-compatible local implementation backed by the existing AES-256-GCM Vault. */
export class LocalKmsProvider implements KmsProvider {
  readonly kind = "local-vault";
  constructor(readonly vault = new VaultStore()) {}
  private key(keyId: string): Buffer {
    if (!keyId || !/^[A-Za-z0-9._/-]+$/.test(keyId)) throw new ProviderError("PROVIDER_CONFIG_INVALID", "KMS key id is invalid");
    const ref = `vault://kms/key/${keyId}`;
    const existing = this.vault.get(ref);
    if (existing) {
      const key = Buffer.from(existing, "base64");
      if (key.length === 32) return key;
      throw new ProviderError("PROVIDER_CONFIG_INVALID", "KMS key material is invalid", { keyId });
    }
    const key = randomBytes(32);
    this.vault.put(ref, key.toString("base64"));
    return key;
  }
  async encrypt(value: Uint8Array | string, keyId = "default"): Promise<EncryptedValue> {
    const iv = randomBytes(12);
    const cipher = createCipheriv("aes-256-gcm", this.key(keyId), iv);
    const plaintext = typeof value === "string" ? Buffer.from(value, "utf8") : Buffer.from(value);
    const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
    return { keyId, algorithm: "aes-256-gcm", iv: iv.toString("base64"), tag: cipher.getAuthTag().toString("base64"), ciphertext: ciphertext.toString("base64") };
  }
  async decrypt(value: EncryptedValue): Promise<Uint8Array> {
    if (value.algorithm !== "aes-256-gcm") throw new ProviderError("PROVIDER_CONFIG_INVALID", "unsupported KMS algorithm");
    const cipher = createDecipheriv("aes-256-gcm", this.key(value.keyId), Buffer.from(value.iv, "base64"));
    cipher.setAuthTag(Buffer.from(value.tag, "base64"));
    return new Uint8Array(Buffer.concat([cipher.update(Buffer.from(value.ciphertext, "base64")), cipher.final()]));
  }
  async sign(value: Uint8Array | string, keyId = "default"): Promise<string> { return this.hmac(value, keyId); }
  async hmac(value: Uint8Array | string, keyId = "default"): Promise<string> {
    const input = typeof value === "string" ? Buffer.from(value, "utf8") : Buffer.from(value);
    return createHmac("sha256", this.key(keyId)).update(input).digest("base64");
  }
  async rotate(keyId = "default"): Promise<string> {
    const next = `${keyId}/${Date.now().toString(36)}-${cryptoRandomUuid().slice(0, 8)}`;
    this.vault.put(`vault://kms/key/${next}`, randomBytes(32).toString("base64"));
    return next;
  }
  auditRef(operation: string, keyId = "default"): string { return `kms-audit://${this.kind}/${encodeURIComponent(keyId)}/${encodeURIComponent(operation)}`; }
  status(): ProviderStatus { return { kind: this.kind, mode: "local", available: true }; }
}

export interface ProviderFactoryOptions {
  root?: string;
  state?: StateProvider;
  blob?: BlobProvider;
  secret?: SecretProvider;
  kms?: KmsProvider;
  createState?: (kind: StateProviderKind, root: string) => StateProvider;
  createBlob?: (kind: BlobProviderKind, root: string) => BlobProvider;
  createSecret?: (kind: SecretProviderKind, root: string) => SecretProvider;
  createKms?: (kind: SecretProviderKind, root: string) => KmsProvider;
}

export function createLocalProviders(options: ProviderFactoryOptions = {}): { state: StateProvider; blob: BlobProvider; secret: SecretProvider; kms: KmsProvider; config: StorageProviderConfig } {
  const root = resolve(options.root ?? process.env.FACTORY_ROOT ?? ".factory");
  const config = resolveStorageProviderConfig({ ...process.env, MAO_STORAGE_MODE: "local", MAO_SECRET_PROVIDER: "local" });
  const state = options.state ?? options.createState?.("sqlite", root) ?? new SqliteStateProvider(join(root, "factory.db"));
  const blob = options.blob ?? options.createBlob?.("local", root) ?? new LocalBlobProvider(join(root, "cas"));
  const secret = options.secret ?? options.createSecret?.("local", root) ?? new LocalSecretProvider();
  const kms = options.kms ?? options.createKms?.("local", root) ?? new LocalKmsProvider((secret as LocalSecretProvider).vault);
  return { state, blob, secret, kms, config };
}

