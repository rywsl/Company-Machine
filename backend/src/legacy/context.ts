import { Database } from "bun:sqlite";
import { existsSync, mkdirSync, readFileSync, writeFileSync, renameSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { RandomIds, SystemClock, type Clock, type IdSource } from "./foundation";
import { Warehouse } from "./warehouse";

export interface ContextOptions { provider?: unknown; clock?: Clock; ids?: IdSource; }
export class Context {
  readonly root: string;
  readonly db: Database;
  readonly clock: Clock;
  readonly ids: IdSource;
  readonly warehouse: Warehouse;
  readonly provider: unknown;
  private constructor(root: string, db: Database, options: Required<ContextOptions>) {
    this.root = root;
    this.db = db;
    this.clock = options.clock;
    this.ids = options.ids;
    this.provider = options.provider;
    this.warehouse = new Warehouse(root, db, this.clock, this.ids);
  }
  static open(root: string, options: ContextOptions = {}): Context {
    const absolute = resolve(root);
    for (const directory of [absolute, join(absolute, "cas"), join(absolute, "jobs"), join(absolute, "tmp"), join(absolute, "tools", "factory", "bin")]) mkdirSync(directory, { recursive: true });
    const dbPath = join(absolute, "factory.db");
    const db = new Database(dbPath, { create: true });
    db.exec("PRAGMA journal_mode = WAL");
    return new Context(absolute, db, { provider: options.provider, clock: options.clock ?? new SystemClock(), ids: options.ids ?? new RandomIds() });
  }
  close(): void { this.db.close(); }
}
export function installFactoryctl(root: string, source?: string): string | undefined {
  if (!source || !existsSync(source)) return undefined;
  const target = join(root, "tools", "factory", "bin", "factoryctl");
  mkdirSync(dirname(target), { recursive: true });
  const data = readFileSync(source).toString().replaceAll("\r\n", "\n");
  const part = join(root, "tmp", `factoryctl.${Date.now()}.part`);
  writeFileSync(part, data);
  renameSync(part, target);
  return target;
}
