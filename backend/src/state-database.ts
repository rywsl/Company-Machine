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
