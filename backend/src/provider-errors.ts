export type ProviderErrorCode =
  | "PROVIDER_UNAVAILABLE"
  | "PROVIDER_CONFIG_INVALID"
  | "CAS_CONFLICT"
  | "CAS_MISSING"
  | "CAS_INTEGRITY_FAILED"
  | "SECRET_NOT_FOUND"
  | "KMS_ERROR"
  | "KMS_OPERATION_FAILED"
  | "MIGRATE_VERIFY_FAILED"
  | "MIGRATE_IN_PROGRESS";

/** Stable, redacted error boundary for external storage and KMS providers. */
export class ProviderError extends Error {
  readonly code: ProviderErrorCode;
  readonly detail: Record<string, unknown>;

  constructor(code: ProviderErrorCode, message: string, detail: Record<string, unknown> = {}) {
    super(message);
    this.name = "ProviderError";
    this.code = code;
    this.detail = redactProviderDetail(detail);
  }

  /** Alias used by provider callers that expose structured error details. */
  get details(): Record<string, unknown> { return this.detail; }
}

function redactProviderDetail(detail: Record<string, unknown>): Record<string, unknown> {
  const sensitive = /(token|secret|password|authorization|api[_-]?key|signature|credential|dsn|private)/i;
  const walk = (value: unknown, key = ""): unknown => {
    if (sensitive.test(key)) return "[REDACTED]";
    if (typeof value === "string") return value.replace(/(token|secret|password|authorization|api[_-]?key|signature|credential|dsn)=([^&\s]+)/gi, "$1=[REDACTED]");
    if (Array.isArray(value)) return value.map((item) => walk(item));
    if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, walk(item, name)]));
    return value;
  };
  return walk(detail) as Record<string, unknown>;
}

export function providerFailure(error: unknown, operation: string, detail: Record<string, unknown> = {}): ProviderError {
  if (error instanceof ProviderError) return error;
  const message = error instanceof Error ? error.message : String(error);
  return new ProviderError("PROVIDER_UNAVAILABLE", `${operation} provider unavailable`, { ...detail, reason: message });
}
