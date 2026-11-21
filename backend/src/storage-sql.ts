import { ProviderError, type ProviderStatus, type StateProvider, type StateProviderKind } from "./providers";
import { join } from "node:path";
import { SqliteStateProvider } from "./providers";
import { ProviderStateDatabase, type StateDatabase } from "./state-database";

type SqlValue = string | number | boolean | null | Uint8Array;

export type SqlDialect = "generic" | "postgres" | "mysql";

function quote(value: unknown, dialect: SqlDialect = "generic"): string {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "boolean") return dialect === "postgres" ? (value ? "TRUE" : "FALSE") : (value ? "1" : "0");
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new ProviderError("PROVIDER_CONFIG_INVALID", "SQL 参数必须是有限数字");
    return String(value);
  }
  if (typeof value === "string") {
    if (value.includes("\u0000")) throw new ProviderError("PROVIDER_CONFIG_INVALID", "SQL 参数不能包含 NUL 字符");
    return `'${value.replaceAll("'", "''")}'`;
  }
  if (value instanceof Uint8Array) {
    const hex = Buffer.from(value).toString("hex");
    return dialect === "postgres" ? `decode('${hex}', 'hex')` : `X'${hex}'`;
  }
  throw new ProviderError("PROVIDER_CONFIG_INVALID", "SQL 参数类型不受支持");
}

function bind(sql: string, params: readonly unknown[], dialect: SqlDialect = "generic"): string {
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
      result += quote(params[index++], dialect);
      continue;
    }
    result += character;
  }
  if (index !== params.length) throw new ProviderError("PROVIDER_CONFIG_INVALID", "SQL 参数数量不匹配");
  return result;
}

export function bindSqlParameters(sql: string, params: readonly unknown[], dialect: SqlDialect = "generic"): string {
  return bind(sql, params, dialect);
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

  readonly database: StateDatabase;
  protected constructor(protected readonly env: NodeJS.ProcessEnv = process.env) {
    this.database = new ProviderStateDatabase(this);
  }
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

  protected bindParameters(sql: string, params: readonly unknown[]): string {
    const dialect: SqlDialect = this.kind === "postgres" || this.kind === "mysql" ? this.kind : "generic";
    return bindSqlParameters(sql, params, dialect);
  }

  protected querySql(sql: string): string {
    return this.runImmediate(sql);
  }

  protected parseRows(text: string): Array<Record<string, unknown>> {
    const trimmed = text.trim();
    if (!trimmed) return [];
    return trimmed.split(/\r?\n/).filter(Boolean).map((line) => {
      try {
        const parsed = JSON.parse(line);
        if (Array.isArray(parsed)) return { value: parsed };
        if (parsed && typeof parsed === "object") return parsed as Record<string, unknown>;
      } catch {
        // The MySQL adapter below parses its tabular output after this method.
      }
      return { value: line };
    });
  }

  query<T = Record<string, unknown>>(sql: string, params: readonly unknown[] = []): T[] {
    const statement = this.translateQuery(this.bindParameters(sql, params));
    if (this.transactionStatements) {
      const script = ["BEGIN", ...this.transactionStatements, statement, "ROLLBACK"].join(";\n");
      return this.parseRows(this.querySql(script)) as T[];
    }
    return this.parseRows(this.querySql(statement)) as T[];
  }

  protected translateQuery(sql: string): string { return sql; }

  execute(sql: string, params: readonly unknown[] = []): { changes: number; lastInsertRowid?: number } {
    const statement = this.translateExecute(this.bindParameters(sql, params));
    if (statement.trim()) this.run(statement);
    return { changes: 0 };
  }

  protected translateExecute(sql: string): string { return sql; }

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
    this.runImmediate("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)");
    const applied = new Set(this.query<Record<string, unknown>>("SELECT version FROM schema_migrations").map((row) => Number(row.version ?? row.value)).filter(Number.isFinite));
    this.transaction(() => {
      migrations.forEach((migration, index) => {
        if (applied.has(index + 1)) return;
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
    const hadIgnore = /\bINSERT\s+OR\s+IGNORE\b/i.test(sql);
    const dialect = this.kind === "postgres" ? "postgres" : "mysql";
    const translated = normalizeSql(sql, dialect);
    if (this.kind === "postgres" && hadIgnore && !/\bON\s+CONFLICT\b/i.test(translated)) {
      return `${translated.replace(/;\s*$/, "")} ON CONFLICT DO NOTHING;`;
    }
    return translated;
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
    const env = { ...this.env } as Record<string, string>;
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
  protected translateQuery(sql: string): string {
    const statement = sql.replace(/;\s*$/, "");
    const pragma = /^PRAGMA\s+table_info\(([^)]+)\)$/i.exec(statement.trim());
    if (pragma) {
      const table = pragma[1].replace(/["'`]/g, "");
      return `SELECT column_name AS name FROM information_schema.columns WHERE table_name=${quoteSql(table)} ORDER BY ordinal_position`;
    }
    if (/^PRAGMA\s+data_version$/i.test(statement.trim())) return "SELECT 1 AS data_version";
    if (/^PRAGMA\b/i.test(statement.trim())) return "SELECT 1";
    return `SELECT COALESCE(json_agg(row_to_json(q))::text, '[]') FROM (${statement}) AS q`;
  }
  protected translateExecute(sql: string): string { return normalizeSql(sql, "postgres"); }
  protected parseRows(text: string): Array<Record<string, unknown>> {
    const trimmed = text.trim();
    if (!trimmed) return [];
    try {
      const parsed = JSON.parse(trimmed);
      if (Array.isArray(parsed)) return parsed as Array<Record<string, unknown>>;
    } catch {
      // Fall back to the bounded diagnostic shape for a malformed client response.
    }
    return super.parseRows(text);
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
    return [this.command, "--batch", "--raw", "--protocol=TCP", "-e", sql];
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
  protected translateQuery(sql: string): string {
    const statement = sql.replace(/;\s*$/, "");
    const pragma = /^PRAGMA\s+table_info\(([^)]+)\)$/i.exec(statement.trim());
    if (pragma) {
      const table = pragma[1].replace(/["'`]/g, "");
      return `SELECT COLUMN_NAME AS name FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=${quoteSql(table)} ORDER BY ORDINAL_POSITION`;
    }
    if (/^PRAGMA\s+data_version$/i.test(statement.trim())) return "SELECT 1 AS data_version";
    if (/^PRAGMA\b/i.test(statement.trim())) return "SELECT 1";
    return normalizeSql(statement, "mysql");
  }
  protected translateExecute(sql: string): string { return normalizeSql(sql, "mysql"); }
  protected parseRows(text: string): Array<Record<string, unknown>> {
    const lines = text.trim().split(/\r?\n/).filter(Boolean);
    if (lines.length < 2) return [];
    const headers = lines.shift()!.split("\t");
    return lines.map((line) => {
      const values = line.split("\t");
      return Object.fromEntries(headers.map((header, index) => [header, values[index] ?? null]));
    });
  }
}

function quoteSql(value: string): string { return `'${value.replaceAll("'", "''")}'`; }

function normalizeSql(sql: string, dialect: "postgres" | "mysql"): string {
  let value = sql.replace(/PRAGMA\s+[^;]+;?/gi, "").replace(/\bWITHOUT\s+ROWID\b/gi, "");
  value = value.replace(/\bSTRICT\b/gi, "");
  if (dialect === "postgres") value = value.replace(/\bREAL\b/gi, "DOUBLE PRECISION");
  value = value.replace(/\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b/gi, dialect === "postgres" ? "BIGSERIAL PRIMARY KEY" : "BIGINT AUTO_INCREMENT PRIMARY KEY");
  value = value.replace(/\bAUTOINCREMENT\b/gi, dialect === "postgres" ? "" : "AUTO_INCREMENT");
  value = value.replace(/CREATE\s+(?:UNIQUE\s+)?INDEX[^;]*(?:json_valid|CASE\s+WHEN)[^;]*;?/gi, "");
  value = value.replace(/UPDATE\s+[^;]*json_set[^;]*;?/gi, "");
  if (dialect === "mysql") value = value.replace(/\bINSERT\s+OR\s+IGNORE\b/gi, "INSERT IGNORE");
  else value = value.replace(/\bINSERT\s+OR\s+IGNORE\b/gi, "INSERT");
  if (dialect === "mysql") value = value.replace(/"([A-Za-z_][A-Za-z0-9_]*)"/g, "`$1`");
  return value;
}

export function stateProviderFor(kind: StateProviderKind, root: string, env: NodeJS.ProcessEnv = process.env): StateProvider {
  if (kind === "sqlite") return new SqliteStateProvider(join(root, "factory.db"));
  return kind === "postgres" ? new PostgresStateProvider(env) : new MysqlStateProvider(env);
}

export type { SqlValue };
