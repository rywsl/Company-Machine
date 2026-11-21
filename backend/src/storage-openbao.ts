import { createHash, randomUUID } from "node:crypto";
import { ProviderError, providerFailure } from "./provider-errors";
import type { KmsProvider, SecretProvider } from "./providers";

export interface OpenBaoOptions {
  addr: string;
  token?: string;
  tokenEnv?: string;
  kvMount?: string;
  transitMount?: string;
  timeoutMs?: number;
  roleId?: string;
  secretId?: string;
  jwt?: string;
  jwtRole?: string;
}

type OpenBaoResponse = { data?: any; errors?: string[]; [key: string]: any };

/** OpenBao HTTP API adapter. OpenBao is deliberately kept outside the Bun process. */
export class OpenBaoKmsProvider implements KmsProvider, SecretProvider {
  readonly kind = "openbao" as const;
  private token?: string;
  private readonly timeoutMs: number;
  private readonly kvMount: string;
  private readonly transitMount: string;
  private readonly base: string;

  constructor(private readonly options: OpenBaoOptions) {
    if (!options.addr) throw new ProviderError("PROVIDER_CONFIG_INVALID", "MAO_OPENBAO_ADDR is required", { variable: "MAO_OPENBAO_ADDR" });
    let parsed: URL;
    try { parsed = new URL(options.addr); }
    catch { throw new ProviderError("PROVIDER_CONFIG_INVALID", "OpenBao address is invalid", { variable: "MAO_OPENBAO_ADDR" }); }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new ProviderError("PROVIDER_CONFIG_INVALID", "OpenBao address must use http or https", { variable: "MAO_OPENBAO_ADDR" });
    }
    this.base = options.addr.replace(/\/+$/, "");
    this.timeoutMs = options.timeoutMs ?? 10_000;
    this.kvMount = (options.kvMount ?? "secret").replace(/^\/+|\/+$/g, "");
    this.transitMount = (options.transitMount ?? "transit").replace(/^\/+|\/+$/g, "");
    this.token = options.token;
  }

  static fromEnvironment(env: NodeJS.ProcessEnv = process.env): OpenBaoKmsProvider {
    const addr = env.MAO_OPENBAO_ADDR;
    if (!addr) throw new ProviderError("PROVIDER_CONFIG_INVALID", "MAO_OPENBAO_ADDR is required", { variable: "MAO_OPENBAO_ADDR" });
    const tokenEnv = env.MAO_OPENBAO_TOKEN ? "MAO_OPENBAO_TOKEN" : undefined;
    return new OpenBaoKmsProvider({
      addr,
      token: tokenEnv ? env[tokenEnv] : undefined,
      tokenEnv,
      kvMount: env.MAO_OPENBAO_KV_MOUNT,
      transitMount: env.MAO_OPENBAO_TRANSIT_MOUNT,
      timeoutMs: env.MAO_OPENBAO_TIMEOUT_MS ? Number(env.MAO_OPENBAO_TIMEOUT_MS) : undefined,
      roleId: env.MAO_OPENBAO_ROLE_ID,
      secretId: env.MAO_OPENBAO_SECRET_ID,
      jwt: env.MAO_OPENBAO_JWT,
      jwtRole: env.MAO_OPENBAO_JWT_ROLE,
    });
  }

  async put(ref: string, value: string): Promise<void> {
    const path = this.kvPath(ref);
    await this.request("PUT", path, { data: { value } });
  }

  async get(ref: string): Promise<string | undefined> {
    const response = await this.request("GET", this.kvPath(ref), undefined, { allowNotFound: true });
    if (!response) return undefined;
    const value = response.data?.data?.value;
    if (value === undefined) return undefined;
    if (typeof value !== "string") throw new ProviderError("KMS_ERROR", "OpenBao secret value is invalid");
    return value;
  }

  async has(ref: string): Promise<boolean> { return (await this.get(ref)) !== undefined; }

  async delete(ref: string): Promise<boolean> {
    const response = await this.request("DELETE", this.kvPath(ref), undefined, { allowNotFound: true });
    return Boolean(response);
  }

  async transitEncrypt(key: string, plaintext: Uint8Array | string): Promise<{ ciphertext: string; auditRef: string }> {
    const input = typeof plaintext === "string" ? Buffer.from(plaintext) : Buffer.from(plaintext);
    const response = await this.request("POST", `${this.transitMount}/encrypt/${encodeURIComponent(key)}`, { plaintext: input.toString("base64") });
    return { ciphertext: String(response?.data?.ciphertext ?? ""), auditRef: this.responseAuditRef(response) };
  }

  async transitDecrypt(key: string, ciphertext: string): Promise<{ plaintext: Uint8Array; auditRef: string }> {
    const response = await this.request("POST", `${this.transitMount}/decrypt/${encodeURIComponent(key)}`, { ciphertext });
    const encoded = response?.data?.plaintext;
    if (typeof encoded !== "string") throw new ProviderError("KMS_ERROR", "OpenBao decrypt response is invalid");
    return { plaintext: Buffer.from(encoded, "base64"), auditRef: this.responseAuditRef(response) };
  }

  async transitSign(key: string, input: Uint8Array | string, hashAlgorithm = "sha2-256"): Promise<{ signature: string; auditRef: string }> {
    const bytes = typeof input === "string" ? Buffer.from(input) : Buffer.from(input);
    const response = await this.request("POST", `${this.transitMount}/sign/${encodeURIComponent(key)}`, { input: bytes.toString("base64"), hash_algorithm: hashAlgorithm });
    return { signature: String(response?.data?.signature ?? ""), auditRef: this.responseAuditRef(response) };
  }

  async transitHmac(key: string, input: Uint8Array | string, hashAlgorithm = "sha2-256"): Promise<{ hmac: string; auditRef: string }> {
    const bytes = typeof input === "string" ? Buffer.from(input) : Buffer.from(input);
    const response = await this.request("POST", `${this.transitMount}/hmac/${encodeURIComponent(key)}`, { input: bytes.toString("base64"), hash_algorithm: hashAlgorithm });
    return { hmac: String(response?.data?.hmac ?? ""), auditRef: this.responseAuditRef(response) };
  }

  async transitRotate(key: string): Promise<{ version?: number; auditRef: string }> {
    const response = await this.request("POST", `${this.transitMount}/keys/${encodeURIComponent(key)}/rotate`, {});
    const version = response?.data?.latest_version;
    return { ...(typeof version === "number" ? { version } : {}), auditRef: this.responseAuditRef(response) };
  }

  status(): any {
    const configured = Boolean(this.token || this.options.tokenEnv || (this.options.roleId && this.options.secretId) || (this.options.jwt && this.options.jwtRole));
    return configured
      ? { kind: this.kind, mode: "production", available: true }
      : { kind: this.kind, mode: "production", available: false, code: "PROVIDER_CONFIG_INVALID", reason: "OpenBao credentials are not configured" };
  }

  async checkAvailability(): Promise<any> {
    try {
      await this.request("GET", "sys/health", undefined, { unauthenticated: true });
      return { kind: this.kind, mode: "production", available: true };
    } catch (error) {
      return { kind: this.kind, mode: "production", available: false, code: "PROVIDER_UNAVAILABLE", reason: error instanceof Error ? error.message : String(error) };
    }
  }

  async encrypt(value: Uint8Array | string, keyId = "default"): Promise<any> {
    const result = await this.transitEncrypt(keyId, value);
    return { keyId, algorithm: "openbao-transit", iv: "", tag: "", ciphertext: result.ciphertext, auditRef: result.auditRef };
  }

  async decrypt(value: any): Promise<Uint8Array> {
    return (await this.transitDecrypt(value.keyId ?? "default", value.ciphertext)).plaintext;
  }

  async sign(value: Uint8Array | string, keyId = "default"): Promise<string> {
    return (await this.transitSign(keyId, value)).signature;
  }

  async hmac(value: Uint8Array | string, keyId = "default"): Promise<string> {
    return (await this.transitHmac(keyId, value)).hmac;
  }

  async rotate(keyId = "default"): Promise<string> {
    const result = await this.transitRotate(keyId);
    return String(result.version ?? keyId);
  }

  auditRef(operation: string, keyId = "default"): string {
    return `openbao-audit://${encodeURIComponent(keyId)}/${encodeURIComponent(operation)}`;
  }

  private kvPath(ref: string): string {
    if (!ref.startsWith("vault://")) throw new ProviderError("PROVIDER_CONFIG_INVALID", "secret reference must start with vault://");
    const raw = ref.slice("vault://".length).replace(/^secret\/+/, "").replace(/^\/+|\/+$/g, "");
    if (!raw || raw.includes("..") || /[?#]/.test(raw)) throw new ProviderError("PROVIDER_CONFIG_INVALID", "secret reference is invalid");
    return `${this.kvMount}/data/${raw.split("/").map(encodeURIComponent).join("/")}`;
  }

  private responseAuditRef(response: OpenBaoResponse | undefined): string {
    const requestId = typeof response?.request_id === "string" ? response.request_id : undefined;
    return requestId ?? `audit:${randomUUID()}`;
  }

  private async ensureToken(): Promise<string | undefined> {
    if (this.token) return this.token;
    if (this.options.tokenEnv) this.token = process.env[this.options.tokenEnv] || undefined;
    if (this.token) return this.token;
    if (this.options.roleId && this.options.secretId) {
      const response = await this.requestRaw("POST", "auth/approle/login", { role_id: this.options.roleId, secret_id: this.options.secretId }, true);
      const token = response?.auth?.client_token;
      if (typeof token !== "string" || token.length === 0) throw new ProviderError("KMS_ERROR", "OpenBao AppRole login did not return a token");
      this.token = token;
      return token;
    }
    if (this.options.jwt && this.options.jwtRole) {
      const response = await this.requestRaw("POST", "auth/jwt/login", { role: this.options.jwtRole, jwt: this.options.jwt }, true);
      const token = response?.auth?.client_token;
      if (typeof token !== "string" || token.length === 0) throw new ProviderError("KMS_ERROR", "OpenBao JWT login did not return a token");
      this.token = token;
      return token;
    }
    throw new ProviderError("PROVIDER_CONFIG_INVALID", "OpenBao token or AppRole/JWT credentials are required");
  }

  private async request(method: string, path: string, body?: unknown, options: { allowNotFound?: boolean; unauthenticated?: boolean } = {}): Promise<OpenBaoResponse | undefined> {
    try {
      const token = options.unauthenticated ? undefined : await this.ensureToken();
      return await this.requestRaw(method, path, body, options.allowNotFound, token);
    } catch (error) {
      if (error instanceof ProviderError) throw error;
      throw providerFailure(error, `OpenBao ${method}`, { operation: path });
    }
  }

  private async requestRaw(method: string, path: string, body?: unknown, allowNotFound = false, token?: string): Promise<OpenBaoResponse | undefined> {
    const headers: Record<string, string> = { accept: "application/json" };
    if (body !== undefined) headers["content-type"] = "application/json";
    if (token) headers["x-vault-token"] = token;
    let response: Response;
    try {
      response = await fetch(`${this.base}/v1/${path.replace(/^\/+/, "")}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(this.timeoutMs) });
    } catch (error) { throw providerFailure(error, "OpenBao request", { operation: path }); }
    const text = await response.text();
    let parsed: OpenBaoResponse = {};
    try { parsed = text ? JSON.parse(text) : {}; } catch { parsed = {}; }
    if (response.status === 404 && allowNotFound) return undefined;
    if (!response.ok || parsed.errors?.length) {
      if (response.status === 404 && allowNotFound) return undefined;
      throw new ProviderError(response.status >= 500 ? "PROVIDER_UNAVAILABLE" : "KMS_ERROR", `OpenBao request failed (${response.status})`, { operation: path, status: response.status, error_count: parsed.errors?.length ?? 0 });
    }
    return parsed;
  }
}

export const OpenBaoSecretProvider = OpenBaoKmsProvider;

export function openBaoAuditDigest(reference: string): string {
  return `sha256:${createHash("sha256").update(reference).digest("hex")}`;
}
