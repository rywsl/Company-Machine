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
  let result = "";
  for (let cursor = 0; cursor < sql.length; cursor += 1) {
    const character = sql[cursor];
    if (character === "'" || character === '"' || character === "`") {
      const quoteMark = character;
      result += character;
      while (++cursor < sql.length) {
        result += sql[cursor];
        if (sql[cursor] === quoteMark) {
          if (sql[cursor + 1] === quoteMark) result += sql[++cursor];
          else break;
        }
      }
      continue;
    }
    if (character === "-" && sql[cursor + 1] === "-") {
      const end = sql.indexOf("\n", cursor);
      const stop = end < 0 ? sql.length : end;
      result += sql.slice(cursor, stop);
      cursor = stop - 1;
      continue;
    }
    if (character === "/" && sql[cursor + 1] === "*") {
      const end = sql.indexOf("*/", cursor + 2);
      if (end < 0) throw new ProviderError("PROVIDER_CONFIG_INVALID", "SQL 注释没有闭合");
      result += sql.slice(cursor, end + 2);
      cursor = end + 1;
      continue;
    }
    if (character === "?") {
      if (index >= params.length) throw new ProviderError("PROVIDER_CONFIG_INVALID", "SQL 参数数量不匹配");
      result += quote(params[index++]);
      continue;
    }
    result += character;
  }
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

  protected constructor(protected readonly env: NodeJS.ProcessEnv = process.env) {}
  private transactionStatements: string[] | undefined;

  protected dsn(): string {
    const value = this.env[this.dsnEnv]?.trim();
    if (!value) throw new ProviderError("PROVIDER_UNAVAILABLE", `${this.kind} provider is not configured`, { reason: `${this.dsnEnv} is missing` });
    return value;
  }

  protected run(sql: string): string {
    if (this.transactionStatements) {
      this.transactionStatements.push(sql);
      return "";
    }
    return this.runImmediate(sql);
  }

  private runImmediate(sql: string): string {
    this.dsn();
    if (!Bun.which(this.command)) throw new ProviderError("PROVIDER_UNAVAILABLE", `${this.command} is not installed`, { reason: "command-not-found" });
    return outputText(Bun.spawnSync(this.argv(sql), { stdout: "pipe", stderr: "pipe", env: this.environment() }));
  }

  protected environment(): Record<string, string> {
    return { ...this.env } as Record<string, string>;
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
    if (this.transactionStatements) throw new ProviderError("PROVIDER_UNAVAILABLE", `${this.kind} transaction is already active`, { reason: "nested-transaction" });
    this.transactionStatements = [];
    try {
      const result = _work();
      const statements = this.transactionStatements;
      this.transactionStatements = undefined;
      if (statements.length > 0) this.runImmediate(["BEGIN", ...statements, "COMMIT"].join(";\n"));
      return result;
    } catch (error) {
      this.transactionStatements = undefined;
      throw error;
    }
  }

  migrate(migrations: readonly string[] = []): number {
    if (migrations.length === 0) {
      this.run("SELECT 1");
      return 0;
    }
    this.transaction(() => {
      this.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)");
      migrations.forEach((migration, index) => {
        const translated = this.translateMigration(migration);
        this.execute(translated);
        const version = index + 1;
        this.execute(this.kind === "postgres"
          ? `INSERT INTO schema_migrations(version) VALUES (${version}) ON CONFLICT DO NOTHING`
          : `INSERT IGNORE INTO schema_migrations(version) VALUES (${version})`);
      });
    });
    return migrations.length;
  }

  private translateMigration(sql: string): string {
    if (/\b(?:AUTOINCREMENT|WITHOUT\s+ROWID|STRICT)\b/i.test(sql)) {
      throw new ProviderError("PROVIDER_CONFIG_INVALID", `${this.kind} migration uses unsupported SQLite syntax`, { reason: "sql-dialect" });
    }
    if (this.kind === "postgres") return sql.replace(/\bINSERT\s+OR\s+IGNORE\b/gi, "INSERT");
    return sql.replace(/\bINSERT\s+OR\s+IGNORE\b/gi, "INSERT IGNORE");
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
  readonly command: string;
  readonly dsnEnv: string;
  readonly mode = "production" as const;
  constructor(env: NodeJS.ProcessEnv = process.env) {
    super(env);
    this.command = env.MAO_POSTGRES_COMMAND?.trim() || "psql";
    this.dsnEnv = env.MAO_POSTGRES_DSN ? "MAO_POSTGRES_DSN" : "MAO_PG_DSN";
  }
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
  readonly command: string;
  readonly dsnEnv: string;
  readonly mode = "production" as const;
  constructor(env: NodeJS.ProcessEnv = process.env) {
    super(env);
    this.command = env.MAO_MYSQL_COMMAND?.trim() || "mysql";
    this.dsnEnv = env.MAO_MYSQL_DSN ? "MAO_MYSQL_DSN" : "MAO_MYSQL_URL";
  }
  protected argv(sql: string): string[] {
    return [this.command, "--batch", "--raw", "--skip-column-names", "--protocol=TCP", "-e", sql];
  }
  protected environment(): Record<string, string> {
    const env = { ...this.env } as Record<string, string>;
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

export function stateProviderFor(kind: StateProviderKind, root: string, env: NodeJS.ProcessEnv = process.env): StateProvider {
  if (kind === "sqlite") return new SqliteStateProvider(join(root, "factory.db"));
  return kind === "postgres" ? new PostgresStateProvider(env) : new MysqlStateProvider(env);
}

export type { SqlValue };
