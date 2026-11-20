import type { Database } from "bun:sqlite";
import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, renameSync, unlinkSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { ManagedDatabase } from "../sqlite";
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
    const db = new ManagedDatabase(dbPath, { create: true });
    db.exec("PRAGMA busy_timeout = 5000");
    db.exec("PRAGMA journal_mode = WAL");
    return new Context(absolute, db, { provider: options.provider, clock: options.clock ?? new SystemClock(), ids: options.ids ?? new RandomIds() });
  }
  close(): void { this.db.close(); }
}
let clientBundle: Promise<string> | undefined;

export async function installFactoryctl(root: string): Promise<string> {
  clientBundle ??= Bun.build({ entrypoints: [join(import.meta.dir, "factoryctl.ts")], target: "bun" })
    .then(async ({ outputs }) => {
      const body = (await outputs[0].text()).replace(/^#![^\n]*\n/, "");
      return `#!/usr/bin/env bun\n${body}`;
    })
    .catch((error: unknown) => {
      clientBundle = undefined;
      throw error;
    });
  const data = await clientBundle;
  const target = join(root, "tools", "factory", "bin", "factoryctl");
  try {
    if (readFileSync(target, "utf8") === data) return target;
  } catch (error) {
    if ((error as { code?: string }).code !== "ENOENT") throw error;
  }
  const directory = dirname(target);
  mkdirSync(directory, { recursive: true });
  const part = join(directory, `.factoryctl.${randomUUID()}.part`);
  writeFileSync(part, data, { mode: 0o755, flag: "wx" });
  try {
    renameSync(part, target);
  } catch (error) {
    unlinkSync(part);
    throw error;
  }
  return target;
}
