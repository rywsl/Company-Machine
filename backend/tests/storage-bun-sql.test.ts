import { expect, test } from "bun:test";
import { BunMysqlStateProvider, BunPostgresStateProvider, type BunSqlClient } from "../src/storage-bun-sql";

class FakeSql implements BunSqlClient {
  readonly options = { max: 4, idleTimeout: 30_000 };
  readonly queries: Array<{ sql: string; params: readonly unknown[] }> = [];
  begins = 0;
  closed = 0;
  async unsafe<T = Record<string, unknown>>(sql: string, params: readonly unknown[] = []): Promise<T[]> {
    this.queries.push({ sql, params });
    if (/^SELECT 1$/i.test(sql.trim())) return [{ value: 1 }] as T[];
    if (/^SELECT version FROM schema_migrations$/i.test(sql.trim())) return [];
    return [] as T[];
  }
  async begin<T>(work: (transaction: BunSqlClient) => Promise<T> | T): Promise<T> {
    this.begins += 1;
    return work(this);
  }
  async close(): Promise<void> { this.closed += 1; }
}

test("Bun PostgreSQL provider translates placeholders and uses a pooled transaction client", async () => {
  const fake = new FakeSql();
  const provider = new BunPostgresStateProvider({ dsn: "postgres://user:password@db.example/factory", client: fake });
  try {
    expect(await provider.query("SELECT '?' AS literal, ? AS value -- ?\n/* ? */", [7])).toEqual([]);
    await provider.transaction(async (transaction) => {
      await transaction.execute("INSERT INTO events(value) VALUES (?)", ["event"]);
    });
    expect(fake.begins).toBe(1);
    expect(fake.queries.map(({ sql }) => sql)).toEqual([
      "SELECT '?' AS literal, $1 AS value -- ?\n/* ? */",
      "INSERT INTO events(value) VALUES ($1)",
    ]);
    expect(fake.queries[0]?.params).toEqual([7]);
    expect((await provider.status()).available).toBe(true);
  } finally {
    await provider.close();
    await provider.close();
  }
  expect(fake.closed).toBe(1);
});

test("Bun MySQL provider keeps question-mark placeholders and performs idempotent migrations", async () => {
  const fake = new FakeSql();
  const provider = new BunMysqlStateProvider({ dsn: "mysql://user:password@db.example/factory", client: fake });
  try {
    await provider.migrate(["CREATE TABLE IF NOT EXISTS migration_probe (id INTEGER PRIMARY KEY)"]);
    expect(fake.begins).toBe(1);
    expect(fake.queries.map(({ sql }) => sql)).toEqual([
      "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)",
      "SELECT version FROM schema_migrations",
      "CREATE TABLE IF NOT EXISTS migration_probe (id INTEGER PRIMARY KEY)",
      "INSERT IGNORE INTO schema_migrations(version) VALUES (?)",
    ]);
  } finally { await provider.close(); }
});

test("Bun SQL provider fails closed when DSN is absent or malformed", () => {
  expect(() => new BunPostgresStateProvider({ env: {} })).toThrow(/not configured/);
  expect(() => new BunMysqlStateProvider({ dsn: "postgres://wrong" })).toThrow(/DSN is invalid/);
});

test("Bun SQL constructor receives pool settings without exposing the DSN to callers", async () => {
  const fake = new FakeSql();
  let observed: { dsn: string; options: Record<string, unknown> } | undefined;
  const provider = new BunPostgresStateProvider({
    dsn: "postgres://user:password@db.example/factory",
    max: 5,
    idleTimeout: 30,
    connectionTimeout: 2,
    maxLifetime: 600,
    prepare: false,
    sqlFactory: (dsn, options) => { observed = { dsn, options }; return fake; },
  });
  expect(observed).toEqual({
    dsn: "postgres://user:password@db.example/factory",
    options: { max: 5, idleTimeout: 30, connectionTimeout: 2, maxLifetime: 600, prepare: false },
  });
  await provider.close();
});

