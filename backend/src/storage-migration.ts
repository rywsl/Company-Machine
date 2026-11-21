import { createHash } from "node:crypto";
import { closeSync, existsSync, mkdirSync, openSync, readdirSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { canonicalJson, randomUuid, utcIso, SystemClock, type Clock } from "./legacy/foundation";
import { ManagedDatabase } from "./sqlite";
import { VaultStore } from "./vault";
import { ProviderError } from "./provider-errors";
import type { StorageProviders } from "./storage-factory";

export interface StorageMigrationOptions {
  root: string;
  sourceDatabase?: string;
  sourceCas?: string;
  sourceVault?: VaultStore;
  target: StorageProviders;
  dryRun?: boolean;
  clock?: Clock;
}

export interface StorageMigrationReport {
  schema: "mao.storage-migration/1";
  source: { state: string; blob: string; secret: string };
  target: { state: string; blob: string; secret: string };
  tables: Array<{ name: string; rows: number; digest: string }>;
  blobs: { count: number; digest: string };
  secrets: { count: number; digest: string };
  digest: string;
  dryRun: boolean;
  switched: boolean;
}

type SqliteTable = { name: string; sql: string };
type SqliteColumn = { name: string };

const digest = (value: unknown): string => `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
const identifier = (value: string): string => {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(value)) throw new ProviderError("PROVIDER_CONFIG_INVALID", "storage identifier is invalid", { value });
  return `"${value}"`;
};

function files(root: string): string[] {
  if (!existsSync(root)) return [];
  const output: string[] = [];
  const walk = (directory: string) => {
    for (const entry of readdirSync(directory, { withFileTypes: true }) as Array<{ name: string; isDirectory(): boolean; isFile(): boolean }>) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) walk(path);
      else if (entry.isFile()) output.push(path);
    }
  };
  walk(root);
  return output.sort();
}

function blobRecords(root: string): Array<{ digest: string; path: string; size: number }> {
  const base = join(root, "sha256");
  return files(base).flatMap((path) => {
    const relative = path.slice(base.length + 1).replaceAll("\\", "/").split("/");
    if (relative.length !== 2 || !/^[0-9a-f]{2}$/.test(relative[0]) || !/^[0-9a-f]{62}$/.test(relative[1])) return [];
    return [{ digest: `sha256:${relative[0]}${relative[1]}`, path, size: statSync(path).size }];
  });
}

function sourceTables(database: ManagedDatabase): Array<{ table: SqliteTable; columns: string[]; rows: Array<Record<string, unknown>> }> {
  const tables = database.query<SqliteTable>("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").all();
  return tables.flatMap((table) => {
    if (!table.sql) return [];
    const columns = database.query<SqliteColumn>(`PRAGMA table_info(${identifier(table.name)})`).all().map(({ name }) => name);
    const rows = database.query<Record<string, unknown>>(`SELECT * FROM ${identifier(table.name)}`).all();
    return [{ table, columns, rows }];
  });
}

function tableDigest(rows: Array<Record<string, unknown>>): string {
  const normalized = rows.map((row) => Object.fromEntries(Object.entries(row).sort(([left], [right]) => left.localeCompare(right))));
  normalized.sort((left, right) => canonicalJson(left).localeCompare(canonicalJson(right)));
  return digest(normalized);
}

function targetTableRows(target: StorageProviders, tableName: string): Array<Record<string, unknown>> {
  try {
    return target.state.query<Record<string, unknown>>(`SELECT * FROM ${identifier(tableName)}`);
  } catch (error) {
    throw new ProviderError("MIGRATE_VERIFY_FAILED", "target table cannot be read after migration", {
      table: tableName,
      reason: error instanceof Error ? error.message : String(error),
    });
  }
}

function writePointer(root: string, report: StorageMigrationReport, clock: Clock): void {
  const path = join(root, "storage-active.json");
  mkdirSync(dirname(path), { recursive: true });
  const body = canonicalJson({
    schema: "mao.storage-active/1",
    provider: report.target,
    migration_digest: report.digest,
    switched_at: utcIso(clock.now()),
  });
  const temporary = `${path}.${randomUuid()}.part`;
  writeFileSync(temporary, body, { encoding: "utf8", mode: 0o600, flag: "wx" });
  renameSync(temporary, path);
}

export async function migrateStorage(options: StorageMigrationOptions): Promise<StorageMigrationReport> {
  const root = resolve(options.root);
  const sourceDatabase = resolve(options.sourceDatabase ?? join(root, "factory.db"));
  const sourceCas = resolve(options.sourceCas ?? join(root, "cas"));
  const clock = options.clock ?? new SystemClock();
  if (!existsSync(sourceDatabase)) throw new ProviderError("PROVIDER_UNAVAILABLE", "source SQLite database is missing", { reason: sourceDatabase });
  mkdirSync(root, { recursive: true });
  const lockPath = join(root, "storage-migration.lock");
  let lockHandle: number | undefined;
  try {
    lockHandle = openSync(lockPath, "wx");
    writeFileSync(lockHandle, Buffer.from(`${process.pid}\n`, "utf8"));
  } catch (error) {
    if ((error as { code?: string }).code === "EEXIST") {
      throw new ProviderError("MIGRATE_IN_PROGRESS", "storage migration is already in progress");
    }
    try { if (lockHandle !== undefined) closeSync(lockHandle); } catch { /* preserve the primary error */ }
    try { unlinkSync(lockPath); } catch { /* the lock was not published */ }
    throw error;
  }
  let source: ManagedDatabase | undefined;
  try {
    source = new ManagedDatabase(sourceDatabase, { create: false, readonly: true });
    const tables = sourceTables(source);
    const tableReports = tables.map(({ table, rows }) => ({ name: table.name, rows: rows.length, digest: tableDigest(rows) }));
    const blobs = blobRecords(sourceCas);
    const sourceVault = options.sourceVault ?? new VaultStore(join(root, "vault.json"), join(root, "vault.key"));
    const refs = sourceVault.refs();
    const reportBase = {
      schema: "mao.storage-migration/1" as const,
      source: { state: "sqlite", blob: "local", secret: "local" },
      target: { state: options.target.config.state, blob: options.target.config.blob, secret: options.target.config.secret },
      tables: tableReports,
      blobs: { count: blobs.length, digest: digest(blobs.map(({ digest: value, size }) => ({ digest: value, size }))) },
      secrets: { count: refs.length, digest: digest(refs) },
    };
    if (!options.dryRun) {
      options.target.state.migrate(tables.map(({ table }) => table.sql));
      options.target.state.transaction(() => {
        for (const { table, columns, rows } of tables) {
          if (!columns.length) continue;
          const names = columns.map(identifier).join(",");
          const placeholders = columns.map(() => "?").join(",");
          for (const row of rows) options.target.state.execute(`INSERT INTO ${identifier(table.name)} (${names}) VALUES (${placeholders})`, columns.map((column) => row[column] ?? null));
        }
      });
      for (const blob of blobs) {
        const stored = await options.target.blob.putFile(blob.path);
        if (stored !== blob.digest) throw new ProviderError("CAS_CONFLICT", "migrated blob digest does not match", { digest: blob.digest });
      }
      for (const ref of refs) {
        const value = sourceVault.get(ref);
        if (value !== undefined) await options.target.secret.put(ref, value);
      }
      const stateProblems = options.target.state.verify();
      const blobProblems = await options.target.blob.verify(blobs.map(({ digest: value }) => value));
      const tableProblems: Array<Record<string, string>> = [];
      for (const table of tableReports) {
        const rows = targetTableRows(options.target, table.name);
        if (rows.length !== table.rows || tableDigest(rows) !== table.digest) {
          tableProblems.push({ kind: "table_mismatch", table: table.name, expected_rows: String(table.rows), actual_rows: String(rows.length) });
        }
      }
      const secretProblems: Array<Record<string, string>> = [];
      for (const ref of refs) {
        if (!(await options.target.secret.has(ref))) secretProblems.push({ kind: "missing_secret_reference", ref });
      }
      if (stateProblems.length || blobProblems.length || tableProblems.length || secretProblems.length) {
        throw new ProviderError("MIGRATE_VERIFY_FAILED", "target provider verification failed", {
          state_problems: stateProblems.length,
          table_problems: tableProblems.length,
          blob_problems: blobProblems.length,
          secret_problems: secretProblems.length,
        });
      }
    }
    const reportWithoutDigest = { ...reportBase, dryRun: Boolean(options.dryRun), switched: false };
    const report: StorageMigrationReport = { ...reportWithoutDigest, digest: digest(reportWithoutDigest), switched: false };
    if (!options.dryRun) {
      writePointer(root, report, clock);
      report.switched = true;
    }
    return report;
  } finally {
    source?.close();
    if (lockHandle !== undefined) closeSync(lockHandle);
    try { unlinkSync(lockPath); } catch { /* preserve the migration result when the lock was removed externally */ }
  }
}

export function readActiveStoragePointer(root: string): Record<string, unknown> | undefined {
  const path = join(resolve(root), "storage-active.json");
  if (!existsSync(path)) return undefined;
  try {
    const value = JSON.parse(readFileSync(path, "utf8"));
    return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
  } catch {
    throw new ProviderError("PROVIDER_CONFIG_INVALID", "storage-active.json is invalid");
  }
}
