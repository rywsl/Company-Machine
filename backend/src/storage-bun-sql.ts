import { ProviderError, type ProviderStatus, type StateProviderKind } from "./providers";

/** The small async boundary needed by Bun.SQL-backed state storage. */
export interface AsyncStateTransaction {
  query<T = Record<string, unknown>>(sql: string, params?: readonly unknown[]): Promise<T[]>;
  execute(sql: string, params?: readonly unknown[]): Promise<{ changes: number; lastInsertRowid?: number }>;
}

/**
 * Async counterpart to StateProvider. It intentionally does not extend
 * StateProvider because the existing repository is synchronous.
 */
export interface AsyncStateProvider {
  readonly kind: Extract<StateProviderKind, "postgres" | "mysql">;
  query<T = Record<string, unknown>>(sql: string, params?: readonly unknown[]): Promise<T[]>;
  execute(sql: string, params?: readonly unknown[]): Promise<{ changes: number; lastInsertRowid?: number }>;
  transaction<T>(work: (transaction: AsyncStateTransaction) => Promise<T> | T): Promise<T>;
  migrate(migrations?: readonly string[]): Promise<number>;
  verify(): Promise<Array<Record<string, string>>>;
  status(): Promise<ProviderStatus>;
  close(): Promise<void>;
}

export interface BunSqlClient {
  unsafe<T = Record<string, unknown>>(sql: string, params?: readonly unknown[]): Promise<T[]>;
  begin<T>(work: (transaction: BunSqlClient) => Promise<T> | T): Promise<T>;
  close(): Promise<void>;
  readonly options?: Record<string, unknown>;
}

export interface BunSqlStateProviderOptions {
  /** A DSN. If omitted, the provider reads its kind-specific environment variable. */
  dsn?: string;
  env?: NodeJS.ProcessEnv;
  /** Bun.SQL pool options. Bun.SQL interprets idle/connection/lifetime values as seconds. */
  max?: number;
  idleTimeout?: number;
  connectionTimeout?: number;
  maxLifetime?: number;
  prepare?: boolean;
  client?: BunSqlClient;
  sqlFactory?: (dsn: string, options: Record<string, unknown>) => BunSqlClient;
}

type SqlResult = Array<Record<string, unknown>> & {
  count?: number;
  changes?: number;
  lastInsertRowid?: number | bigint;
};

function environmentName(kind: AsyncStateProvider["kind"]): "MAO_POSTGRES_DSN" | "MAO_MYSQL_DSN" {
  return kind === "postgres" ? "MAO_POSTGRES_DSN" : "MAO_MYSQL_DSN";
}

function requireDsn(kind: AsyncStateProvider["kind"], options: BunSqlStateProviderOptions): string {
  const name = environmentName(kind);
  const dsn = options.dsn?.trim() || options.env?.[name]?.trim() || process.env[name]?.trim();
  if (!dsn) throw new ProviderError("PROVIDER_UNAVAILABLE", `${kind} provider is not configured`, { reason: `${name} is missing` });
  const expected = kind === "postgres" ? /^(postgres|postgresql):\/\//i : /^mysql:\/\//i;
  if (!expected.test(dsn)) throw new ProviderError("PROVIDER_CONFIG_INVALID", `${kind} DSN is invalid`, { name });
  return dsn;
}

/** Converts the repository's SQLite-compatible ? placeholders to PostgreSQL $n. */
function postgresPlaceholders(sql: string): string {
  let result = "";
  let parameter = 0;
  let quote: string | undefined;
  let lineComment = false;
  let blockComment = false;
  for (let index = 0; index < sql.length; index += 1) {
    const character = sql[index];
    const next = sql[index + 1];
    if (lineComment) {
      result += character;
      if (character === "\n") lineComment = false;
      continue;
    }
    if (blockComment) {
      result += character;
      if (character === "*" && next === "/") {
        result += next;
        index += 1;
        blockComment = false;
      }
      continue;
    }
    if (quote) {
      result += character;
      if (character === quote) {
        if (next === quote) {
          result += next;
          index += 1;
        } else quote = undefined;
      }
      continue;
    }
    if ((character === "'" || character === '"' || character === "`") && !quote) {
      quote = character;
      result += character;
      continue;
    }
    if (character === "-" && next === "-") {
      result += "--";
      index += 1;
      lineComment = true;
      continue;
    }
    if (character === "/" && next === "*") {
      result += "/*";
      index += 1;
      blockComment = true;
      continue;
    }
    if (character === "?") {
      parameter += 1;
      result += `$${parameter}`;
      continue;
    }
    result += character;
  }
  return result;
}

function translate(kind: AsyncStateProvider["kind"], sql: string): string {
  const statement = sql.replace(/;\s*$/, "");
  return kind === "postgres" ? postgresPlaceholders(statement) : statement;
}

function resultMetadata(result: unknown): { changes: number; lastInsertRowid?: number } {
  const metadata = result as SqlResult;
  const changes = Number(metadata?.changes ?? metadata?.count ?? 0);
  const rawLastInsert = metadata?.lastInsertRowid;
  const lastInsertRowid = rawLastInsert === undefined ? undefined : Number(rawLastInsert);
  return { changes: Number.isFinite(changes) ? changes : 0, ...(lastInsertRowid === undefined || !Number.isFinite(lastInsertRowid) ? {} : { lastInsertRowid }) };
}

function providerError(kind: string, error: unknown): ProviderError {
  if (error instanceof ProviderError) return error;
  const reason = error instanceof Error ? error.message : String(error);
  const safeReason = reason.replace(/\b(postgres(?:ql)?|mysql):\/\/[^@\s]+@/gi, "$1://[REDACTED]@").replace(/\b(password|pwd|token|secret|api[_-]?key)=([^&\s]+)/gi, "$1=[REDACTED]");
  return new ProviderError("PROVIDER_UNAVAILABLE", `${kind} provider operation failed`, { reason: safeReason.slice(0, 240) });
}

/**
 * Bun.SQL-backed PostgreSQL/MySQL provider. Bun.SQL owns a persistent pool and
 * automatically reuses connections between calls. The provider deliberately
 * stays separate from synchronous StateProvider until the repository is async.
 */
export class BunSqlStateProvider implements AsyncStateProvider {
  readonly database = undefined;
  readonly kind: AsyncStateProvider["kind"];
  private readonly dsnValue: string;
  private readonly client: BunSqlClient;
  private closed = false;

  constructor(kind: AsyncStateProvider["kind"], options: BunSqlStateProviderOptions = {}) {
    this.kind = kind;
    this.dsnValue = requireDsn(kind, options);
    const sqlOptions: Record<string, unknown> = {};
    for (const name of ["max", "idleTimeout", "connectionTimeout", "maxLifetime", "prepare"] as const) {
      const value = options[name];
      if (value !== undefined) sqlOptions[name] = value;
    }
    this.client = options.client ?? options.sqlFactory?.(this.dsnValue, sqlOptions) ?? new Bun.SQL(this.dsnValue, sqlOptions) as unknown as BunSqlClient;
  }

  private ensureOpen(): void {
    if (this.closed) throw new ProviderError("PROVIDER_UNAVAILABLE", `${this.kind} provider is closed`, { reason: "closed" });
  }

  async query<T = Record<string, unknown>>(sql: string, params: readonly unknown[] = []): Promise<T[]> {
    this.ensureOpen();
    try { return await this.client.unsafe<T>(translate(this.kind, sql), params); }
    catch (error) { throw providerError(this.kind, error); }
  }

  async execute(sql: string, params: readonly unknown[] = []): Promise<{ changes: number; lastInsertRowid?: number }> {
    this.ensureOpen();
    try { return resultMetadata(await this.client.unsafe(translate(this.kind, sql), params)); }
    catch (error) { throw providerError(this.kind, error); }
  }

  async transaction<T>(work: (transaction: AsyncStateTransaction) => Promise<T> | T): Promise<T> {
    this.ensureOpen();
    try {
      return await this.client.begin(async (client) => work({
        query: <R = Record<string, unknown>>(sql: string, params: readonly unknown[] = []) => client.unsafe<R>(translate(this.kind, sql), params),
        execute: async (sql: string, params: readonly unknown[] = []) => resultMetadata(await client.unsafe(translate(this.kind, sql), params)),
      }));
    } catch (error) { throw providerError(this.kind, error); }
  }

  async migrate(migrations: readonly string[] = []): Promise<number> {
    this.ensureOpen();
    if (migrations.length === 0) {
      await this.execute("SELECT 1");
      return 0;
    }
    await this.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)");
    const applied = new Set((await this.query<{ version: number }>("SELECT version FROM schema_migrations")).map((row) => Number(row.version)).filter(Number.isFinite));
    await this.transaction(async (transaction) => {
      for (const [index, migration] of migrations.entries()) {
        const version = index + 1;
        if (applied.has(version)) continue;
        await transaction.execute(migration);
        await transaction.execute(this.kind === "postgres"
          ? "INSERT INTO schema_migrations(version) VALUES (?) ON CONFLICT DO NOTHING"
          : "INSERT IGNORE INTO schema_migrations(version) VALUES (?)", [version]);
      }
    });
    return migrations.length;
  }

  async verify(): Promise<Array<Record<string, string>>> {
    try {
      await this.query("SELECT 1");
      return [];
    } catch (error) {
      const wrapped = providerError(this.kind, error);
      return [{ kind: "provider_unavailable", reason: wrapped.message }];
    }
  }

  async status(): Promise<ProviderStatus> {
    if (this.closed) return { kind: this.kind, mode: "production", available: false, code: "PROVIDER_UNAVAILABLE", reason: "closed" };
    try {
      await this.query("SELECT 1");
      return { kind: this.kind, mode: "production", available: true };
    } catch (error) {
      const wrapped = providerError(this.kind, error);
      return { kind: this.kind, mode: "production", available: false, code: wrapped.code, reason: wrapped.message };
    }
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    try { await this.client.close(); }
    catch (error) { throw providerError(this.kind, error); }
  }
}

export class BunPostgresStateProvider extends BunSqlStateProvider {
  constructor(options: BunSqlStateProviderOptions = {}) { super("postgres", options); }
}

export class BunMysqlStateProvider extends BunSqlStateProvider {
  constructor(options: BunSqlStateProviderOptions = {}) { super("mysql", options); }
}

export interface AsyncSecretProvider {
  readonly kind: "local" | "openbao";
  put(ref: string, value: string): Promise<void>;
  get(ref: string): Promise<string | undefined>;
  has(ref: string): Promise<boolean>;
  delete(ref: string): Promise<boolean>;
  status(): Promise<ProviderStatus> | ProviderStatus;
}

