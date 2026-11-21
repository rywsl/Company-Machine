import { ProviderError, type ProviderStatus, type StateProvider, type StateProviderKind } from "./providers";
import { join } from "node:path";
import { SqliteStateProvider } from "./providers";

type SqlValue = string | number | boolean | null | Uint8Array;

function quote(value: unknown): string {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "boolean") return value ? "1" : "0";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new ProviderError("PROVIDER_CONFIG_INVALID", "SQL 参数必须是有限数字");
    return String(value);
  }
  if (typeof value === "string") {
    if (value.includes("\u0000")) throw new ProviderError("PROVIDER_CONFIG_INVALID", "SQL 参数不能包含 NUL 字符");
    return `'${value.replaceAll("'", "''")}'`;
  }
  if (value instanceof Uint8Array) return `X'${Buffer.from(value).toString("hex")}'`;
  throw new ProviderError("PROVIDER_CONFIG_INVALID", "SQL 参数类型不受支持");
}

function bind(sql: string, params: readonly unknown[]): string {
  let index = 0;
  const result = sql.replaceAll("?", () => {
    if (index >= params.length) throw new ProviderError("PROVIDER_CONFIG_INVALID", "SQL 参数数量不匹配");
    return quote(params[index++]);
  });
  if (index !== params.length) throw new ProviderError("PROVIDER_CONFIG_INVALID", "SQL 参数数量不匹配");
  return result;
}

function outputText(result: ReturnType<typeof Bun.spawnSync>): string {
  if (result.exitCode !== 0) {
    const stderr = result.stderr instanceof Uint8Array ? new TextDecoder().decode(result.stderr) : "";
    throw new ProviderError("PROVIDER_UNAVAILABLE", "SQL provider command failed", { reason: stderr.trim().slice(0, 240) });
  }
  return result.stdout instanceof Uint8Array ? new TextDecoder().decode(result.stdout) : "";
}

abstract class CliStateProvider implements StateProvider {
  abstract readonly kind: StateProviderKind;
  abstract readonly command: string;
  abstract readonly dsnEnv: string;
  abstract readonly mode: "production";

  protected dsn(): string {
    const value = process.env[this.dsnEnv]?.trim();
    if (!value) throw new ProviderError("PROVIDER_UNAVAILABLE", `${this.kind} provider is not configured`, { reason: `${this.dsnEnv} is missing` });
    return value;
  }

  protected run(sql: string): string {
    this.dsn();
    if (!Bun.which(this.command)) throw new ProviderError("PROVIDER_UNAVAILABLE", `${this.command} is not installed`, { reason: "command-not-found" });
    return outputText(Bun.spawnSync(this.argv(sql), { stdout: "pipe", stderr: "pipe", env: this.environment() }));
  }

  protected environment(): Record<string, string> {
    return { ...process.env } as Record<string, string>;
  }

  protected abstract argv(sql: string): string[];

  query<T = Record<string, unknown>>(sql: string, params: readonly unknown[] = []): T[] {
    const text = this.run(bind(sql, params)).trim();
    if (!text) return [];
    return text.split(/\r?\n/).filter(Boolean).map((line) => {
      try { return JSON.parse(line) as T; }
      catch { return { value: line } as T; }
    });
  }

  execute(sql: string, params: readonly unknown[] = []): { changes: number; lastInsertRowid?: number } {
    this.run(bind(sql, params));
    return { changes: 0 };
  }

  transaction<T>(_work: () => T): T {
    throw new ProviderError("PROVIDER_UNAVAILABLE", `${this.kind} transactions require the SQL provider session adapter`, { reason: "transaction-session-not-configured" });
  }

  migrate(): number {
    this.run("SELECT 1");
    return 0;
  }

  verify(): Array<Record<string, string>> {
    try {
      this.run("SELECT 1");
      return [];
    } catch (error) {
      return [{ kind: "provider_unavailable", reason: error instanceof Error ? error.message : String(error) }];
    }
  }

  status(): ProviderStatus {
    try {
      this.dsn();
      if (!Bun.which(this.command)) return { kind: this.kind, mode: this.mode, available: false, code: "PROVIDER_UNAVAILABLE", reason: "command-not-found" };
      return { kind: this.kind, mode: this.mode, available: true };
    } catch (error) {
      return { kind: this.kind, mode: this.mode, available: false, code: "PROVIDER_UNAVAILABLE", reason: error instanceof Error ? error.message : String(error) };
    }
  }

  close(): void {}
}

export class PostgresStateProvider extends CliStateProvider {
  readonly kind = "postgres" as const;
  readonly command = process.env.MAO_POSTGRES_COMMAND?.trim() || "psql";
  readonly dsnEnv = process.env.MAO_POSTGRES_DSN ? "MAO_POSTGRES_DSN" : "MAO_PG_DSN";
  readonly mode = "production" as const;
  protected argv(sql: string): string[] {
    const dsn = this.dsn();
    let database: string | undefined;
    try {
      if (dsn.startsWith("postgres://") || dsn.startsWith("postgresql://")) database = new URL(dsn).pathname.slice(1) || undefined;
    } catch { throw new ProviderError("PROVIDER_CONFIG_INVALID", "PostgreSQL DSN is invalid"); }
    return [this.command, "-X", "-q", "-t", "-A", "-F", "\t", "-v", "ON_ERROR_STOP=1", ...(database ? ["-d", database] : []), "-c", sql];
  }
  protected environment(): Record<string, string> {
    const env = { ...process.env } as Record<string, string>;
    const dsn = this.dsn();
    if (dsn.startsWith("postgres://") || dsn.startsWith("postgresql://")) {
      try {
        const url = new URL(dsn);
        if (url.hostname) env.PGHOST = url.hostname;
        if (url.port) env.PGPORT = url.port;
        if (url.username) env.PGUSER = decodeURIComponent(url.username);
        if (url.password) env.PGPASSWORD = decodeURIComponent(url.password);
        if (url.pathname.length > 1) env.PGDATABASE = url.pathname.slice(1);
      } catch { throw new ProviderError("PROVIDER_CONFIG_INVALID", "PostgreSQL DSN is invalid"); }
    } else {
      env.PGSERVICE = dsn;
    }
    return env;
  }
}

export class MysqlStateProvider extends CliStateProvider {
  readonly kind = "mysql" as const;
  readonly command = process.env.MAO_MYSQL_COMMAND?.trim() || "mysql";
  readonly dsnEnv = process.env.MAO_MYSQL_DSN ? "MAO_MYSQL_DSN" : "MAO_MYSQL_URL";
  readonly mode = "production" as const;
  protected argv(sql: string): string[] {
    return [this.command, "--batch", "--raw", "--skip-column-names", "--protocol=TCP", "-e", sql];
  }
  protected environment(): Record<string, string> {
    const env = { ...process.env } as Record<string, string>;
    const dsn = this.dsn();
    if (dsn.startsWith("mysql://")) {
      try {
        const url = new URL(dsn);
        if (url.username) env.MYSQL_USER = decodeURIComponent(url.username);
        if (url.password) env.MYSQL_PWD = decodeURIComponent(url.password);
        if (url.hostname) env.MYSQL_HOST = url.hostname;
        if (url.port) env.MYSQL_TCP_PORT = url.port;
        if (url.pathname.length > 1) env.MYSQL_DATABASE = url.pathname.slice(1);
      } catch { throw new ProviderError("PROVIDER_CONFIG_INVALID", "MAO_MYSQL_DSN is invalid"); }
    } else {
      env.MYSQL_PWD ??= dsn;
    }
    return env;
  }
}

export function stateProviderFor(kind: StateProviderKind, root: string): StateProvider {
  if (kind === "sqlite") return new SqliteStateProvider(join(root, "factory.db"));
  return kind === "postgres" ? new PostgresStateProvider() : new MysqlStateProvider();
}

export type { SqlValue };
