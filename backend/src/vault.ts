import { randomBytes, createCipheriv, createDecipheriv } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { SystemClock, utcIso, type Clock } from "./legacy/foundation";

type VaultRecord = { ref: string; iv: string; tag: string; ciphertext: string; updatedAt: string };

function keyFromEnvironment(path: string): any {
  const configured = process.env.MAO_VAULT_KEY;
  if (configured) {
    const value = /^[0-9a-f]{64}$/i.test(configured)
      ? Buffer.from(configured, "hex")
      : Buffer.from(configured, "base64");
    if (value.length !== 32) throw new Error("MAO_VAULT_KEY must decode to 32 bytes");
    return value;
  }
  mkdirSync(dirname(path), { recursive: true });
  try {
    const existing = readFileSync(path);
    if (existing.length === 32) return existing;
  } catch {
    // A new local vault receives a fresh key below.
  }
  const created = randomBytes(32);
  writeFileSync(path, created, { mode: 0o600 });
  return created;
}

export class VaultStore {
  private readonly key: any;
  private readonly path: string;

  constructor(
    path = process.env.MAO_VAULT_PATH ?? join(process.env.FACTORY_ROOT ?? ".factory", "vault.json"),
    keyPath = process.env.MAO_VAULT_KEY_PATH ?? join(process.env.FACTORY_ROOT ?? ".factory", "vault.key"),
    private readonly clock: Clock = new SystemClock(),
  ) {
    this.path = path;
    this.key = keyFromEnvironment(keyPath);
    mkdirSync(dirname(path), { recursive: true });
  }

  put(ref: string, value: string): void {
    if (!ref.startsWith("vault://")) throw new Error("vault reference is invalid");
    if (value.length === 0 || value.length > 64 * 1024) throw new Error("secret value is invalid");
    const iv = randomBytes(12);
    const cipher = createCipheriv("aes-256-gcm", this.key, iv);
    const ciphertext = Buffer.concat([cipher.update(value, "utf8"), cipher.final()]);
    const records = this.read();
    records[ref] = {
      ref,
      iv: iv.toString("base64"),
      tag: cipher.getAuthTag().toString("base64"),
      ciphertext: ciphertext.toString("base64"),
      updatedAt: utcIso(this.clock.now()),
    };
    writeFileSync(this.path, JSON.stringify(records), { encoding: "utf8", mode: 0o600 });
  }

  has(ref: string): boolean {
    return Boolean(this.read()[ref]);
  }

  get(ref: string): string | undefined {
    const record = this.read()[ref];
    if (!record) return undefined;
    const cipher = createDecipheriv("aes-256-gcm", this.key, Buffer.from(record.iv, "base64"));
    cipher.setAuthTag(Buffer.from(record.tag, "base64"));
    return Buffer.concat([cipher.update(Buffer.from(record.ciphertext, "base64")), cipher.final()]).toString("utf8");
  }

  private read(): Record<string, VaultRecord> {
    try {
      const value = JSON.parse(readFileSync(this.path, "utf8"));
      return value && typeof value === "object" ? value as Record<string, VaultRecord> : {};
    } catch {
      return {};
    }
  }
}
