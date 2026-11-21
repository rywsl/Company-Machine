import { createHash, createHmac } from "node:crypto";
import { mkdirSync, readFileSync, statSync, writeFileSync, renameSync, unlinkSync } from "node:fs";
import { dirname, join } from "node:path";
import { randomUuid, SystemClock, type Clock } from "./legacy/foundation";
import { ProviderError, providerFailure } from "./provider-errors";
import type { BlobInput, BlobProvider } from "./providers";

export interface S3BlobOptions {
  endpoint: string;
  bucket: string;
  region?: string;
  accessKeyEnv?: string;
  secretKeyEnv?: string;
  sessionTokenEnv?: string;
  addressing?: "path" | "virtual";
  cacheRoot?: string;
  timeoutMs?: number;
  clock?: Clock;
  environment?: NodeJS.ProcessEnv;
}

const DIGEST = /^sha256:[0-9a-f]{64}$/;
const hex = (bytes: Uint8Array) => Buffer.from(bytes).toString("hex");
const sha256 = (value: Uint8Array | string) => createHash("sha256").update(value).digest("hex");
const hmac = (key: Uint8Array | string, value: string) => createHmac("sha256", key).update(value).digest();

/** Standard-library S3/MinIO CAS implementation with a local read cache. */
export class S3BlobProvider implements BlobProvider {
  readonly kind = "s3" as const;
  private readonly endpoint: URL;
  private readonly region: string;
  private readonly addressing: "path" | "virtual";
  private readonly cacheRoot: string;
  private readonly timeoutMs: number;
  private readonly accessKeyEnv: string;
  private readonly secretKeyEnv: string;
  private readonly sessionTokenEnv?: string;
  private readonly clock: Clock;
  private readonly environment: NodeJS.ProcessEnv;
  private readonly knownDigests = new Set<string>();

  constructor(private readonly options: S3BlobOptions) {
    try { this.endpoint = new URL(options.endpoint); }
    catch { throw new ProviderError("PROVIDER_CONFIG_INVALID", "S3 endpoint is invalid", { variable: "MAO_S3_ENDPOINT" }); }
    if (this.endpoint.protocol !== "http:" && this.endpoint.protocol !== "https:") throw new ProviderError("PROVIDER_CONFIG_INVALID", "S3 endpoint must use http or https", { variable: "MAO_S3_ENDPOINT" });
    if (!options.bucket || /[\\/?#]/.test(options.bucket)) throw new ProviderError("PROVIDER_CONFIG_INVALID", "S3 bucket is invalid", { variable: "MAO_S3_BUCKET" });
    this.region = options.region ?? "us-east-1";
    this.addressing = options.addressing ?? "path";
    this.cacheRoot = options.cacheRoot ?? join(process.env.FACTORY_ROOT ?? ".factory", "cas-cache");
    this.timeoutMs = options.timeoutMs ?? 15_000;
    this.accessKeyEnv = options.accessKeyEnv ?? "MAO_S3_ACCESS_KEY";
    this.secretKeyEnv = options.secretKeyEnv ?? "MAO_S3_SECRET_KEY";
    this.sessionTokenEnv = options.sessionTokenEnv;
    this.clock = options.clock ?? new SystemClock();
    this.environment = options.environment ?? process.env;
    mkdirSync(this.cacheRoot, { recursive: true });
  }

  static fromEnvironment(env: NodeJS.ProcessEnv = process.env): S3BlobProvider {
    const endpoint = env.MAO_S3_ENDPOINT;
    const bucket = env.MAO_S3_BUCKET;
    if (!endpoint || !bucket) throw new ProviderError("PROVIDER_CONFIG_INVALID", "MAO_S3_ENDPOINT and MAO_S3_BUCKET are required");
    return new S3BlobProvider({
      endpoint,
      bucket,
      region: env.MAO_S3_REGION,
      accessKeyEnv: env.MAO_S3_ACCESS_KEY_ENV ?? "MAO_S3_ACCESS_KEY",
      secretKeyEnv: env.MAO_S3_SECRET_KEY_ENV ?? "MAO_S3_SECRET_KEY",
      sessionTokenEnv: env.MAO_S3_SESSION_TOKEN_ENV,
      addressing: env.MAO_S3_ADDRESSING === "virtual" ? "virtual" : "path",
      cacheRoot: env.MAO_S3_CACHE_ROOT,
      timeoutMs: env.MAO_S3_TIMEOUT_MS ? Number(env.MAO_S3_TIMEOUT_MS) : undefined,
      environment: env,
    });
  }

  async putFile(source: string): Promise<string> {
    let bytes: Buffer;
    try { bytes = readFileSync(source); }
    catch (error) { throw new ProviderError("PROVIDER_UNAVAILABLE", "cannot read blob source", { reason: error instanceof Error ? error.message : String(error) }); }
    return this.put(bytes);
  }

  async put(input: BlobInput, expectedDigest?: string): Promise<string> {
    const value = typeof input === "string"
      ? new Uint8Array(readFileSync(input))
      : input instanceof ArrayBuffer ? new Uint8Array(input) : input;
    const digest = `sha256:${sha256(value)}`;
    if (expectedDigest && expectedDigest !== digest) throw new ProviderError("CAS_CONFLICT", "blob digest does not match expected digest", { digest: expectedDigest });
    const existing = await this.head(digest);
    if (existing) {
      if (existing.digest && existing.digest !== digest) throw new ProviderError("CAS_CONFLICT", "remote object metadata digest mismatch", { digest });
      this.knownDigests.add(digest);
      return digest;
    }
    await this.request("PUT", digest, value, { "content-type": "application/octet-stream", "x-amz-meta-sha256": digest.slice(7) });
    this.knownDigests.add(digest);
    return digest;
  }

  async get(digest: string): Promise<Uint8Array> {
    this.checkDigest(digest);
    const cached = this.cachePath(digest);
    try {
      const bytes = readFileSync(cached);
    if (`sha256:${sha256(bytes)}` === digest) { this.knownDigests.add(digest); return bytes; }
      unlinkSync(cached);
    } catch { /* cache miss */ }
    const response = await this.request("GET", digest);
    const bytes = Buffer.from(await response.arrayBuffer());
    if (`sha256:${sha256(bytes)}` !== digest) throw new ProviderError("CAS_CONFLICT", "remote object digest mismatch", { digest });
    mkdirSync(dirname(cached), { recursive: true });
    const temporary = `${cached}.${randomUuid()}.part`;
    writeFileSync(temporary, bytes, { mode: 0o600 });
    renameSync(temporary, cached);
    this.knownDigests.add(digest);
    return bytes;
  }

  async getFile(digest: string, destination: string): Promise<void> {
    const bytes = await this.get(digest);
    mkdirSync(dirname(destination), { recursive: true });
    writeFileSync(destination, bytes, { mode: 0o600 });
  }

  blobPath(digest: string): string { this.checkDigest(digest); return this.cachePath(digest); }
  async exists(digest: string): Promise<boolean> { return Boolean(await this.head(digest)); }

  async head(digest: string): Promise<{ size: number; digest: string } | undefined> {
    this.checkDigest(digest);
    try {
      const response = await this.request("HEAD", digest);
      const metadata = response.headers.get("x-amz-meta-sha256") ?? undefined;
      const sizeText = response.headers.get("content-length");
      return { digest: metadata ? `sha256:${metadata.replace(/^sha256:/, "")}` : digest, size: sizeText ? Number(sizeText) : 0 };
    } catch (error) {
      if (error instanceof ProviderError && error.detail.status === 404) return undefined;
      throw error;
    }
  }

  async verify(): Promise<Array<Record<string, string>>> {
    const digests: string[] = [...this.knownDigests];
    const problems: Array<Record<string, string>> = [];
    for (const digest of digests) {
      try { const object = await this.head(digest); if (!object) problems.push({ kind: "missing_blob", digest }); else if (object.digest && object.digest !== digest) problems.push({ kind: "digest_mismatch", digest }); }
      catch (error) { problems.push({ kind: "provider_error", digest, reason: error instanceof Error ? error.message : String(error) }); }
    }
    return problems;
  }

  status(): any {
    const configured = Boolean(this.environment[this.accessKeyEnv] && this.environment[this.secretKeyEnv]);
    return configured
      ? { kind: this.kind, mode: "production", available: true }
      : { kind: this.kind, mode: "production", available: false, code: "PROVIDER_CONFIG_INVALID", reason: "S3 credentials are not configured" };
  }

  private checkDigest(digest: string): void { if (!DIGEST.test(digest)) throw new ProviderError("CAS_MISSING", "blob digest is invalid", { digest }); }
  private cachePath(digest: string): string { const value = digest.slice(7); return join(this.cacheRoot, "sha256", value.slice(0, 2), value.slice(2)); }

  private objectUrl(digest: string): URL {
    const value = digest.slice(7);
    const path = `sha256/${value.slice(0, 2)}/${value.slice(2)}`;
    const url = new URL(this.endpoint.toString());
    if (this.addressing === "virtual") {
      url.hostname = `${this.options.bucket}.${url.hostname}`;
      url.pathname = `${url.pathname.replace(/\/$/, "")}/${path}`;
    } else url.pathname = `${url.pathname.replace(/\/$/, "")}/${encodeURIComponent(this.options.bucket)}/${path}`;
    return url;
  }

  private async request(method: string, digest: string, body?: Uint8Array, extra: Record<string, string> = {}): Promise<Response> {
    const url = this.objectUrl(digest);
    const payloadHash = body === undefined ? sha256("") : sha256(body);
    const now = this.clock.now();
    const amzDate = now.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
    const date = amzDate.slice(0, 8);
    const access = this.environment[this.accessKeyEnv];
    const secret = this.environment[this.secretKeyEnv];
    if (!access || !secret) throw new ProviderError("PROVIDER_CONFIG_INVALID", "S3 credentials are missing", { accessKeyEnv: this.accessKeyEnv, secretKeyEnv: this.secretKeyEnv });
    const headers: Record<string, string> = { host: url.host, "x-amz-content-sha256": payloadHash, "x-amz-date": amzDate, ...extra };
    const session = this.sessionTokenEnv ? this.environment[this.sessionTokenEnv] : undefined;
    if (session) headers["x-amz-security-token"] = session;
    const signedNames = Object.keys(headers).map((key) => key.toLowerCase()).sort();
    const canonicalHeaders = signedNames.map((key) => `${key}:${headers[key]!.trim().replace(/\s+/g, " ")}\n`).join("");
    const canonical = `${method}\n${url.pathname.split("/").map((part, index) => index === 0 ? "" : encodeURIComponent(decodeURIComponent(part))).join("/")}\n${url.searchParams.toString()}\n${canonicalHeaders}\n${signedNames.join(";")}\n${payloadHash}`;
    const scope = `${date}/${this.region}/s3/aws4_request`;
    const stringToSign = `AWS4-HMAC-SHA256\n${amzDate}\n${scope}\n${sha256(canonical)}`;
    const signingKey = hmac(hmac(hmac(hmac(`AWS4${secret}`, date), this.region), "s3"), "aws4_request");
    headers.authorization = `AWS4-HMAC-SHA256 Credential=${access}/${scope}, SignedHeaders=${signedNames.join(";")}, Signature=${hex(hmac(signingKey, stringToSign))}`;
    try {
      const response = await fetch(url, { method, headers, body: body as BodyInit | undefined, signal: AbortSignal.timeout(this.timeoutMs) });
      if (!response.ok) throw new ProviderError(response.status >= 500 ? "PROVIDER_UNAVAILABLE" : response.status === 404 ? "CAS_MISSING" : "KMS_ERROR", `S3 request failed (${response.status})`, { status: response.status, operation: method });
      return response;
    } catch (error) {
      if (error instanceof ProviderError) throw error;
      throw providerFailure(error, `S3 ${method}`, { operation: method });
    }
  }
}

