import { Database, type Statement } from "bun:sqlite";

export class ManagedDatabase extends Database {
  private readonly statements = new Map<string, Statement>();
  private closed = false;

  override query<T = any>(sql: string): Statement<T> {
    let statement = this.statements.get(sql);
    if (!statement || statement.isFinalized) {
      statement = this.prepare(sql);
      this.statements.set(sql, statement);
    }
    return statement;
  }

  override close(throwOnError = true): void {
    if (this.closed) return;
    // Bun leaves query statements beyond its default cache limit to GC.
    for (const statement of this.statements.values()) {
      if (!statement.isFinalized) statement.finalize();
    }
    this.statements.clear();
    super.close(throwOnError);
    this.closed = true;
  }
}
