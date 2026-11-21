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
