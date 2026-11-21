export interface StateStatement<T = any> {
  all(...params: any[]): T[];
  get(...params: any[]): T | null;
  run(...params: any[]): { changes?: number; lastInsertRowid?: number | bigint };
}

export interface StateTransaction<T = any> {
  (): T;
  immediate(): T;
}

/** Synchronous database surface used by the existing business repository. */
export interface StateDatabase {
  exec(sql: string): void;
  query<T = any>(sql: string): StateStatement<T>;
  transaction<T>(work: () => T): StateTransaction<T>;
  close(force?: boolean): void;
}

/** Promise-based repository surface for Bun.SQL-backed state providers. */
export interface AsyncStateStatement<T = any> {
  all(...params: any[]): Promise<T[]>;
  get(...params: any[]): Promise<T | null>;
  run(...params: any[]): Promise<{ changes?: number; lastInsertRowid?: number }>;
}

export interface AsyncStateDatabase {
  exec(sql: string): Promise<void>;
  query<T = any>(sql: string): AsyncStateStatement<T>;
  transaction<T>(work: (database: AsyncStateDatabase) => Promise<T>): Promise<T>;
  close(): Promise<void>;
}

type AsyncQuery = <T = Record<string, unknown>>(sql: string, params?: readonly unknown[]) => Promise<T[]>;
type AsyncExecute = (sql: string, params?: readonly unknown[]) => Promise<{ changes: number; lastInsertRowid?: number }>;

class AsyncProviderStatement<T> implements AsyncStateStatement<T> {
  constructor(private readonly query: AsyncQuery, private readonly execute: AsyncExecute, private readonly sql: string) {}
  all(...params: any[]): Promise<T[]> { return this.query<T>(this.sql, params); }
  async get(...params: any[]): Promise<T | null> { return (await this.query<T>(this.sql, params))[0] ?? null; }
  run(...params: any[]): Promise<{ changes?: number; lastInsertRowid?: number }> { return this.execute(this.sql, params); }
}

/**
 * Adapts an AsyncStateProvider to a repository-shaped async database. The
 * adapter deliberately exposes no synchronous escape hatch, so callers cannot
 * accidentally put Promise-backed providers behind the legacy SqliteStore.
 */
export class AsyncProviderStateDatabase implements AsyncStateDatabase {
  constructor(
    private readonly queryProvider: AsyncQuery,
    private readonly executeProvider: AsyncExecute,
    private readonly transactionProvider: <T>(work: (database: AsyncStateDatabase) => Promise<T>) => Promise<T>,
    private readonly closeProvider: () => Promise<void>,
  ) {}

  static from(provider: {
    query: AsyncQuery;
    execute: AsyncExecute;
    transaction<T>(work: (transaction: { query: AsyncQuery; execute: AsyncExecute }) => Promise<T> | T): Promise<T>;
    close(): Promise<void>;
  }): AsyncProviderStateDatabase {
    return new AsyncProviderStateDatabase(
      provider.query.bind(provider),
      provider.execute.bind(provider),
      (work) => provider.transaction((transaction) => work(new AsyncProviderStateDatabase(
        transaction.query.bind(transaction),
        transaction.execute.bind(transaction),
        async () => { throw new Error("nested async state transactions are unsupported"); },
        async () => {},
      ))),
      provider.close.bind(provider),
    );
  }

  exec(sql: string): Promise<void> { return this.executeProvider(sql).then(() => undefined); }
  query<T = any>(sql: string): AsyncStateStatement<T> { return new AsyncProviderStatement<T>(this.queryProvider, this.executeProvider, sql); }
  transaction<T>(work: (database: AsyncStateDatabase) => Promise<T>): Promise<T> { return this.transactionProvider(work); }
  close(): Promise<void> { return this.closeProvider(); }
}

import type { StateProvider } from "./providers";

class ProviderStatement<T> implements StateStatement<T> {
  constructor(private readonly provider: StateProvider, private readonly sql: string) {}
  all(...params: any[]): T[] { return this.provider.query<T>(this.sql, params); }
  get(...params: any[]): T | null { return this.provider.query<T>(this.sql, params)[0] ?? null; }
  run(...params: any[]): { changes?: number; lastInsertRowid?: number | bigint } { return this.provider.execute(this.sql, params); }
}

/** Adapts a synchronous StateProvider to the database surface used by the repository. */
export class ProviderStateDatabase implements StateDatabase {
  constructor(readonly provider: StateProvider) {}
  exec(sql: string): void { this.provider.execute(sql); }
  query<T = any>(sql: string): StateStatement<T> { return new ProviderStatement<T>(this.provider, sql); }
  transaction<T>(work: () => T): StateTransaction<T> {
    let ran = false;
    let value: T;
    const run = () => {
      if (!ran) {
        value = this.provider.transaction(work);
        ran = true;
      }
      return value;
    };
    return Object.assign(run, { immediate: run });
  }
  close(): void { this.provider.close(); }
}
