import { createHash } from "node:crypto";
import { chmodSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, renameSync, rmdirSync, rmSync, statSync } from "node:fs";
import { copyFile, open } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve, sep } from "node:path";
import type { Database } from "bun:sqlite";
import { resolveGitExecutable } from "../runtime/git";
import { FactoryError } from "./legacy/errors";
import { canonicalJson, SystemClock, utcIso, type Clock } from "./legacy/foundation";
import { readGovernedRegistry } from "./legacy/registry-config";
import { ManagedDatabase } from "./sqlite";

const format = "mao-backup/bun/1";
const manifestName = "manifest.json";
const factoryName = "factory.db";
const businessName = "business.db";
const bundleName = "registry.bundle";
const hashPattern = /^sha256:[0-9a-f]{64}$/;
const casPattern = /^cas\/sha256\/([0-9a-f]{2})\/([0-9a-f]{62})$/;

type BackupOptions = { root: string; businessDatabase?: string; withLogs?: boolean; by?: string; registry?: string; clock?: Clock };
type BackupFile = { path: string; sha256: string; size: number };
type ObjectFormat = "sha1" | "sha256";
type RegistryBundle = {
  bundle: typeof bundleName;
  head: string | null;
  head_oid: string;
  object_format: ObjectFormat;
  refs: Record<string, string>;
};
type BackupManifest = {
  format: typeof format;
  created_at: string;
  by: string;
  databases: { factory: typeof factoryName; business: typeof factoryName | typeof businessName | null };
  files: BackupFile[];
  secrets: string[];
  with_logs: boolean;
  registry?: RegistryBundle;
};
type Problem = { kind: string; path: string };
type Verification = { ok: boolean; problems: Problem[] };

function stat(path: string): ReturnType<typeof lstatSync> | undefined {
  try { return lstatSync(path); }
  catch (error) {
    if ((error as { code?: string }).code === "ENOENT") return undefined;
    throw error;
  }
}

function regularFile(path: string): boolean {
  return Boolean(stat(path)?.isFile());
}

function availableTarget(target: string): void {
  const info = stat(target);
  if (info && (!info.isDirectory() || readdirSync(target).length > 0)) {
    throw new FactoryError("BACKUP_TARGET_NOT_EMPTY", "备份或恢复目标非空", { path: target });
  }
}

function stageBeside(target: string): string {
  availableTarget(target);
  const parent = dirname(target);
  mkdirSync(parent, { recursive: true });
  return mkdtempSync(join(parent, `.${basename(target)}-`));
}

async function retryWindowsFileOperation(operation: () => void): Promise<void> {
  for (let attempt = 0; ; attempt += 1) {
    try { operation(); return; }
    catch (error) {
      const code = (error as { code?: string }).code;
      if (process.platform !== "win32" || attempt === 6 || (code !== "EBUSY" && code !== "EPERM" && code !== "EACCES" && code !== "ENOTEMPTY")) throw error;
      // A Git subprocess or Windows file scanner can briefly hold a directory open after checkout.
      await new Promise((resolve) => setTimeout(resolve, 25 * 2 ** attempt));
    }
  }
}

async function publish(stage: string, target: string): Promise<void> {
  await retryWindowsFileOperation(() => {
    availableTarget(target);
    if (stat(target)) rmdirSync(target);
    renameSync(stage, target);
  });
}

async function removeStage(stage: string): Promise<void> {
  if (process.platform === "win32" && stat(stage)?.isDirectory()) {
    // Git packs are read-only on Windows, which prevents Bun from deleting a failed staging tree.
    for (const relative of listTree(stage).files) {
      const path = join(stage, relative);
      const info = stat(path);
      if (info?.isFile() && (info.mode & 0o200) === 0) chmodSync(path, info.mode | 0o200);
    }
  }
  await retryWindowsFileOperation(() => rmSync(stage, { recursive: true, force: true }));
}

async function discardStage(stage: string, error: unknown): Promise<never> {
  try { await removeStage(stage); }
  catch (cleanupError) {
    throw new Error(`${String(error)}\nUnable to remove the backup staging directory: ${String(cleanupError)}`, { cause: error });
  }
  throw error;
}

function samePath(left: string, right: string): boolean {
  return process.platform === "win32" ? left.toLowerCase() === right.toLowerCase() : left === right;
}

function underPath(root: string, child: string): boolean {
  return samePath(root, child) || samePath(child.slice(0, root.length + 1), root + sep);
}

function listTree(root: string, relativeRoot = ""): { files: string[]; links: string[] } {
  const files: string[] = [];
  const links: string[] = [];
  function visit(path: string): void {
    const absolute = join(root, path);
    const info = stat(absolute);
    if (!info) return;
    if (info.isFile()) files.push(path);
    else if (info.isDirectory()) {
      for (const name of readdirSync(absolute) as string[]) visit(path ? `${path}/${name}` : name);
    } else links.push(path);
  }
  visit(relativeRoot);
  return { files, links };
}

function safePath(path: string): boolean {
  return path.split("/").every((part) => part.length > 0 && part !== "." && part !== ".."
    && !/[<>:"\\|?*\x00-\x1f]/.test(part) && !/[ .]$/.test(part)
    && !/^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(part));
}

function allowedPath(path: string, withLogs: boolean): boolean {
  if (!safePath(path)) return false;
  if (path === factoryName || path === businessName || casPattern.test(path)) return true;
  if (!withLogs || /(?:^|\/)(?:console\.token|vault\.(?:key|json)|\.env)(?:\/|$)/i.test(path) || /(?:^|\/)secrets\/values(?:\/|$)/i.test(path)) return false;
  return /^logs\/(?:jobs\/[^/]+\/[^/]+|foreman\/sessions\/[^/]+)\/.+/.test(path);
}

function openReadonly(path: string): ManagedDatabase {
  const db = new ManagedDatabase(path, { readonly: true });
  try {
    db.exec("PRAGMA busy_timeout=5000; PRAGMA trusted_schema=OFF");
    return db;
  } catch (error) {
    db.close();
    throw error;
  }
}

function tables(db: Database): Set<string> {
  return new Set(db.query<{ name: string }>("SELECT name FROM sqlite_schema WHERE type='table'").all().map(({ name }) => name));
}

function inspectDatabase(root: string, name: string): { liveBlobs: string[]; secrets: string[]; hasBusiness: boolean } {
  const db = openReadonly(join(root, name));
  try {
    const integrity = db.query<{ integrity_check: string }>("PRAGMA integrity_check").all();
    if (integrity.length !== 1 || integrity[0].integrity_check !== "ok") throw new Error("SQLite integrity check failed");
    const known = tables(db);
    const liveBlobs = known.has("payload_files") && known.has("beans") ? db.query<{ digest: string }>(`
      SELECT DISTINCT p.digest FROM payload_files p JOIN beans b ON b.id=p.bean_id WHERE b.expired_at IS NULL
    `).all().map(({ digest }) => digest) : [];
    if (known.has("intake_uploads") && known.has("project_sources")) {
      const uploaded = db.query<{ digest: string }>(`
        SELECT DISTINCT upload.digest FROM project_sources AS source
        JOIN intake_uploads AS upload ON upload.digest = substr(source.file_ref, 7)
        WHERE source.kind = 'uploaded_file' AND source.file_ref GLOB 'cas://sha256:*'
      `).all();
      for (const { digest } of uploaded) liveBlobs.push(digest);
    }
    const secrets = known.has("secret_requests") ? db.query<{ name: string }>("SELECT DISTINCT name FROM secret_requests").all().map(({ name }) => name) : [];
    return { liveBlobs, secrets, hasBusiness: known.has("business_systems") };
  } finally { db.close(); }
}

function snapshotDatabase(source: string, target: string): void {
  if (!regularFile(source)) throw new FactoryError("BACKUP_SOURCE_MISSING", "找不到可备份的 SQLite 数据库", { path: source });
  const db = openReadonly(source);
  try {
    db.query("VACUUM INTO ?").run(target);
  } catch (error) {
    const code = (error as { code?: string }).code;
    if (code === "SQLITE_BUSY" || code === "SQLITE_LOCKED") throw new FactoryError("BACKUP_BUSY", "SQLite 暂时无法取得快照", { path: source });
    if (code === "SQLITE_NOTADB" || code === "SQLITE_CORRUPT") sourceCorrupt(source);
    throw error;
  } finally { db.close(); }
}

function sourceCorrupt(path: string): never {
  throw new FactoryError("BACKUP_SOURCE_CORRUPT", "源数据库或仓库文件无法备份", { path });
}

async function digestFile(path: string): Promise<string> {
  const hash = createHash("sha256");
  const buffer = new Uint8Array(256 * 1024);
  // An explicitly closed descriptor lets Windows rename the staging directory immediately.
  const file = await open(path, "r");
  try {
    while (true) {
      const { bytesRead } = await file.read(buffer, 0, buffer.length, null);
      if (bytesRead === 0) break;
      hash.update(buffer.subarray(0, bytesRead));
    }
  } finally {
    await file.close();
  }
  return `sha256:${hash.digest("hex")}`;
}

async function describeFile(root: string, path: string): Promise<BackupFile> {
  const absolute = join(root, path);
  const info = stat(absolute);
  if (!info?.isFile()) return sourceCorrupt(path);
  return { path, sha256: await digestFile(absolute), size: info.size };
}

async function copyBackupFile(source: string, stage: string, path: string): Promise<BackupFile> {
  if (!regularFile(source)) return sourceCorrupt(path);
  const target = join(stage, path);
  mkdirSync(dirname(target), { recursive: true });
  await copyFile(source, target);
  const item = await describeFile(stage, path);
  const cas = casPattern.exec(path);
  if (cas && item.sha256 !== `sha256:${cas[1]}${cas[2]}`) return sourceCorrupt(path);
  return item;
}

function logFiles(root: string): Array<{ source: string; path: string }> {
  const files: Array<{ source: string; path: string }> = [];
  for (const prefix of ["jobs", "foreman/sessions"]) {
    const prefixPath = join(root, prefix);
    if (!stat(prefixPath)?.isDirectory() || (prefix.startsWith("foreman/") && !stat(join(root, "foreman"))?.isDirectory())) continue;
    for (const first of readdirSync(prefixPath) as string[]) {
      if (!stat(join(prefixPath, first))?.isDirectory()) continue;
      const attempts = prefix === "jobs" ? readdirSync(join(prefixPath, first)) as string[] : [""];
      for (const attempt of attempts) {
        if (!stat(join(prefixPath, first, attempt))?.isDirectory()) continue;
        const logs = join(prefixPath, first, attempt, "logs");
        const archivePrefix = ["logs", prefix, first, attempt].filter(Boolean).join("/");
        for (const name of listTree(logs).files) {
          const path = `${archivePrefix}/${name}`;
          if (allowedPath(path, true)) files.push({ source: join(logs, name), path });
        }
      }
    }
  }
  return files;
}

function secretNames(root: string): string[] {
  const parent = join(root, "secrets");
  const path = join(parent, "manifest.json");
  if (!stat(parent)?.isDirectory() || !regularFile(path)) return [];
  try {
    const names: unknown = JSON.parse(readFileSync(path, "utf8"));
    return object(names) ? Object.keys(names) : [];
  } catch { return []; }
}

type GitProcess = { exited: Promise<number>; stdout: ReadableStream; stderr: ReadableStream; kill(signal?: number): void };

async function git(cwd: string, args: string[], allowedExit = [0]): Promise<string> {
  const environment = Object.fromEntries(Object.entries(process.env).filter((entry): entry is [string, string] =>
    !/^GIT_/i.test(entry[0]) && typeof entry[1] === "string"));
  const argv = [resolveGitExecutable(), "-C", cwd, ...args];
  const bun = Bun as unknown as { spawn(argv: string[], options: Record<string, unknown>): GitProcess };
  let child: GitProcess;
  try {
    child = bun.spawn(argv, {
      stdin: "ignore", stdout: "pipe", stderr: "pipe",
      env: {
        ...environment,
        GIT_CONFIG_GLOBAL: process.platform === "win32" ? "NUL" : "/dev/null",
        GIT_CONFIG_NOSYSTEM: "1", GIT_NO_LAZY_FETCH: "1", GIT_NO_REPLACE_OBJECTS: "1", GIT_TERMINAL_PROMPT: "0",
      },
    });
  } catch (error) {
    throw new FactoryError("GIT_FAILED", "无法启动 Git", { command: args[0], reason: error instanceof Error ? error.message : String(error) });
  }
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; child.kill(9); }, 120_000);
  try {
    const [code, stdout, stderr] = await Promise.all([
      child.exited, new Response(child.stdout).text(), new Response(child.stderr).text(),
    ]);
    if (timedOut || !allowedExit.includes(code)) {
      throw new FactoryError("GIT_FAILED", timedOut ? "Git 命令超时" : "Git 命令失败", { command: args[0], stderr: stderr.trim().slice(0, 4000) });
    }
    return stdout.trimEnd();
  } finally { clearTimeout(timer); }
}

function referenceMap(text: string): Record<string, string> {
  const refs: Array<[string, string]> = [];
  for (const line of text.split(/\r?\n/).filter(Boolean)) {
    const match = /^([0-9a-f]{40}|[0-9a-f]{64}) (HEAD|refs\/\S+)$/.exec(line);
    if (!match) throw new FactoryError("GIT_FAILED", "Git 引用列表格式不合法");
    refs.push([match[2], match[1]]);
  }
  return Object.fromEntries(refs);
}

async function createRegistryBundle(directory: string, stage: string): Promise<RegistryBundle> {
  const source = resolve(directory);
  if (!stat(source)?.isDirectory()) throw new FactoryError("BACKUP_SOURCE_MISSING", "找不到 Registry 仓库目录", { path: source });
  const isBare = await git(source, ["rev-parse", "--is-bare-repository"]);
  const root = await git(source, ["rev-parse", isBare === "true" ? "--absolute-git-dir" : "--show-toplevel"]);
  if (!samePath(source, resolve(root))) {
    // Git may expand the Windows short spelling of the same directory.
    const sourceEntry = statSync(source, { bigint: true });
    const rootEntry = statSync(root, { bigint: true });
    if (sourceEntry.ino === 0n || sourceEntry.ino !== rootEntry.ino || sourceEntry.dev !== rootEntry.dev) {
      throw new FactoryError("BACKUP_SOURCE_INVALID", "请选择 Registry Git 仓库的根目录", { path: source });
    }
  }
  const objectFormat = await git(source, ["rev-parse", "--show-object-format"]);
  if (objectFormat !== "sha1" && objectFormat !== "sha256") throw new FactoryError("BACKUP_UNSUPPORTED", "Registry 使用不受支持的 Git 对象格式", { path: source });
  const head = await git(source, ["symbolic-ref", "--quiet", "HEAD"], [0, 1]) || null;
  const headOid = await git(source, ["rev-parse", "--verify", "HEAD"]);
  const bundlePath = join(stage, bundleName);
  await git(source, ["bundle", "create", bundlePath, "--all", "HEAD"]);
  const { HEAD: capturedHead, ...refs } = referenceMap(await git(source, ["bundle", "list-heads", bundlePath]));
  if (capturedHead !== headOid || (head && refs[head] !== headOid) || (await git(source, ["symbolic-ref", "--quiet", "HEAD"], [0, 1]) || null) !== head) {
    throw new FactoryError("BACKUP_BUSY", "Registry 的 HEAD 在备份期间发生变化", { path: source });
  }
  return { bundle: bundleName, head, head_oid: headOid, object_format: objectFormat, refs };
}

function validRegistryBundle(value: unknown): value is RegistryBundle {
  if (!object(value) || value.bundle !== bundleName || (value.object_format !== "sha1" && value.object_format !== "sha256")) return false;
  const oid = value.object_format === "sha1" ? /^[0-9a-f]{40}$/ : /^[0-9a-f]{64}$/;
  if (typeof value.head_oid !== "string" || !oid.test(value.head_oid) || !object(value.refs)) return false;
  if (!Object.entries(value.refs).every(([name, id]) => /^refs\/\S+$/.test(name) && typeof id === "string" && oid.test(id))) return false;
  return value.head === null || (typeof value.head === "string" && value.refs[value.head] === value.head_oid);
}

async function verifyRegistryBundle(path: string, registry: RegistryBundle): Promise<boolean> {
  const temporary = mkdtempSync(join(tmpdir(), "mao-bundle-"));
  try {
    await git(temporary, ["init", "--bare", "--template=", `--object-format=${registry.object_format}`]);
    await git(temporary, ["bundle", "verify", path]);
    const refs = referenceMap(await git(temporary, ["bundle", "list-heads", path]));
    return canonicalJson(refs) === canonicalJson({ ...registry.refs, HEAD: registry.head_oid });
  } catch { return false; }
  finally { await retryWindowsFileOperation(() => rmSync(temporary, { recursive: true, force: true })); }
}

async function restoreRegistryBundle(stage: string, registry: RegistryBundle): Promise<void> {
  const directory = join(stage, "registry");
  const bundle = join(stage, bundleName);
  mkdirSync(directory);
  await git(directory, ["init", "--template=", `--object-format=${registry.object_format}`]);
  const refspecs = Object.keys(registry.refs).length ? ["+refs/*:refs/*", "HEAD"] : ["HEAD"];
  await git(directory, ["fetch", "--update-head-ok", "--no-recurse-submodules", "--no-tags", "--no-auto-gc", "--no-write-fetch-head", bundle, ...refspecs]);
  if (registry.head) {
    await git(directory, ["symbolic-ref", "HEAD", registry.head]);
    await git(directory, ["reset", "--hard", registry.head_oid]);
  } else {
    await git(directory, ["checkout", "--detach", "--no-recurse-submodules", registry.head_oid]);
  }
  const refs = referenceMap(await git(directory, ["for-each-ref", "--format=%(objectname) %(refname)"]));
  const head = await git(directory, ["rev-parse", "--verify", "HEAD"]);
  if (head !== registry.head_oid || canonicalJson(refs) !== canonicalJson(registry.refs)) {
    throw new FactoryError("BACKUP_CORRUPT", "恢复后的 Registry 引用不一致", { path: bundleName });
  }
}

export async function createBackup(options: BackupOptions, destination: string): Promise<BackupManifest> {
  const root = resolve(options.root);
  const target = resolve(destination);
  const factory = join(root, factoryName);
  const configuredBusiness = options.businessDatabase && resolve(options.businessDatabase);
  if (options.businessDatabase === ":memory:") throw new FactoryError("BACKUP_UNSUPPORTED", "内存数据库没有可备份的文件");
  if (underPath(join(root, "cas"), target) || underPath(join(root, "jobs"), target) || underPath(join(root, "foreman"), target)) {
    throw new FactoryError("BACKUP_TARGET_INVALID", "备份目录必须独立于仓库和执行目录", { path: target });
  }
  const source = regularFile(factory) ? factory : configuredBusiness ?? factory;
  const split = Boolean(configuredBusiness && !samePath(source, configuredBusiness));
  const stage = stageBeside(target);
  try {
    snapshotDatabase(source, join(stage, factoryName));
    if (split && configuredBusiness) snapshotDatabase(configuredBusiness, join(stage, businessName));
    const databases = [factoryName, ...(split ? [businessName] : [])];
    const files: BackupFile[] = [];
    const needed = new Set<string>();
    const secretSet = new Set(secretNames(root));
    let hasBusiness = false;
    for (const name of databases) {
      let summary;
      try { summary = inspectDatabase(stage, name); } catch { return sourceCorrupt(name); }
      if (name === businessName && !summary.hasBusiness) return sourceCorrupt(configuredBusiness ?? name);
      for (const secret of summary.secrets) secretSet.add(secret);
      for (const digest of summary.liveBlobs) needed.add(digest);
      hasBusiness ||= summary.hasBusiness;
      files.push(await describeFile(stage, name));
    }
    const casDir = join(root, "cas");
    if (stat(casDir)?.isDirectory()) {
      for (const path of listTree(root, "cas/sha256").files.filter((file) => casPattern.test(file))) {
        const item = await copyBackupFile(join(root, path), stage, path);
        files.push(item);
        needed.delete(item.sha256);
      }
    }
    for (const digest of needed) return sourceCorrupt(digest);
    if (options.withLogs) {
      for (const { source: log, path } of logFiles(root)) files.push(await copyBackupFile(log, stage, path));
    }
    const registryRoot = options.registry ?? readGovernedRegistry(root)?.registry;
    const registry = registryRoot ? await createRegistryBundle(registryRoot, stage) : undefined;
    if (registry) files.push(await describeFile(stage, bundleName));
    files.sort((a, b) => a.path.localeCompare(b.path, "en"));
    const manifest: BackupManifest = {
      format, created_at: utcIso((options.clock ?? new SystemClock()).now()), by: options.by ?? "human", files,
      databases: { factory: factoryName, business: split ? businessName : hasBusiness ? factoryName : null },
      secrets: [...secretSet].sort(), with_logs: Boolean(options.withLogs),
      ...(registry ? { registry } : {}),
    };
    await Bun.write(join(stage, manifestName), `${canonicalJson(manifest)}\n`);
    await publish(stage, target);
    return manifest;
  } catch (error) { return discardStage(stage, error); }
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

async function readManifest(root: string): Promise<BackupManifest | undefined> {
  const path = join(root, manifestName);
  if (!stat(root)?.isDirectory() || !regularFile(path)) return undefined;
  try {
    const value: unknown = JSON.parse(await Bun.file(path).text());
    if (!object(value) || value.format !== format || typeof value.created_at !== "string" || typeof value.by !== "string"
      || typeof value.with_logs !== "boolean" || !Array.isArray(value.secrets) || value.secrets.some((key) => typeof key !== "string")
      || !object(value.databases) || value.databases.factory !== factoryName || ![factoryName, businessName, null].includes(value.databases.business as string | null)
      || (value.registry !== undefined && !validRegistryBundle(value.registry))
      || !Array.isArray(value.files)) return undefined;
    const names = new Set<string>();
    for (const item of value.files) {
      if (!object(item) || typeof item.path !== "string" || (item.path !== bundleName && !allowedPath(item.path, value.with_logs)) || names.has(item.path.toLowerCase())
        || typeof item.sha256 !== "string" || !hashPattern.test(item.sha256) || typeof item.size !== "number" || !Number.isSafeInteger(item.size) || item.size < 0) return undefined;
      names.add(item.path.toLowerCase());
    }
    if (!names.has(factoryName) || names.has(businessName) !== (value.databases.business === businessName) || names.has(bundleName) !== (value.registry !== undefined)) return undefined;
    return value as BackupManifest;
  } catch { return undefined; }
}

export async function verifyBackup(destination: string): Promise<Verification> {
  const root = resolve(destination);
  const manifest = await readManifest(root);
  if (!manifest) return { ok: false, problems: [{ kind: "manifest", path: manifestName }] };
  return verifyManifest(root, manifest);
}

async function verifyManifest(root: string, manifest: BackupManifest): Promise<Verification> {
  const tree = listTree(root);
  const actual = new Set(tree.files);
  const listed = new Set(manifest.files.map(({ path }) => path));
  const valid = new Set<string>();
  const problems: Problem[] = tree.links.map((path) => ({ kind: "link", path }));
  for (const path of actual) if (path !== manifestName && !listed.has(path)) problems.push({ kind: "extra", path });
  for (const entry of manifest.files) {
    if (!actual.has(entry.path)) { problems.push({ kind: "missing", path: entry.path }); continue; }
    const current = await describeFile(root, entry.path);
    const cas = casPattern.exec(entry.path);
    if (current.sha256 !== entry.sha256 || current.size !== entry.size || (cas && current.sha256 !== `sha256:${cas[1]}${cas[2]}`)) {
      problems.push({ kind: "mismatch", path: entry.path });
    } else valid.add(entry.path);
  }
  const blobPaths = new Set(manifest.files.filter(({ path }) => casPattern.test(path) && valid.has(path)).map(({ sha256 }) => sha256));
  for (const name of [factoryName, businessName].filter((file) => valid.has(file))) {
    try {
      const { liveBlobs, hasBusiness } = inspectDatabase(root, name);
      if (manifest.databases.business === name && !hasBusiness) problems.push({ kind: "database", path: name });
      for (const digest of liveBlobs) if (!blobPaths.has(digest)) problems.push({ kind: "blob", path: digest });
    } catch { problems.push({ kind: "database", path: name }); }
  }
  if (manifest.registry && valid.has(bundleName) && !await verifyRegistryBundle(join(root, bundleName), manifest.registry)) {
    problems.push({ kind: "bundle", path: bundleName });
  }
  problems.sort((a, b) => a.kind.localeCompare(b.kind, "en") || a.path.localeCompare(b.path, "en"));
  return { ok: problems.length === 0, problems };
}

function restoredPath(path: string): string {
  if (!path.startsWith("logs/")) return path;
  const [, category, folder, id, ...file] = path.split("/");
  return `${category}/${folder}/${id}/logs/${file.join("/")}`;
}

function clearHostState(stage: string, name: string): boolean {
  const db = new ManagedDatabase(join(stage, name));
  try {
    db.exec("PRAGMA trusted_schema=OFF");
    const known = tables(db);
    db.transaction(() => {
      for (const table of ["job_execution_locks", "schedule_claims", "changeset_stage_locks", "leases"]) {
        if (known.has(table)) db.exec(`DELETE FROM ${table}`);
      }
      if (known.has("secret_requests")) db.exec("UPDATE secret_requests SET status='requested' WHERE status='available'");
    }).immediate();
    return known.has("epoch_pointers") && Boolean(db.query("SELECT 1 FROM epoch_pointers WHERE name = 'active'").get());
  } finally { db.close(); }
}

export async function restoreBackup(destination: string, root: string) {
  const source = resolve(destination);
  const target = resolve(root);
  availableTarget(target);
  if (underPath(source, target)) throw new FactoryError("BACKUP_TARGET_INVALID", "恢复目录必须独立于备份目录", { path: target });
  const manifest = await readManifest(source);
  if (!manifest) throw new FactoryError("BACKUP_CORRUPT", "备份清单不合法", { problems: [{ kind: "manifest", path: manifestName }] });
  const verification = await verifyManifest(source, manifest);
  if (!verification.ok) throw new FactoryError("BACKUP_CORRUPT", "备份校验失败", { problems: verification.problems });
  const stage = stageBeside(target);
  try {
    for (const entry of manifest.files) {
      const relative = restoredPath(entry.path);
      const item = await copyBackupFile(join(source, entry.path), stage, relative);
      if (item.sha256 !== entry.sha256 || item.size !== entry.size) {
        throw new FactoryError("BACKUP_CORRUPT", "备份文件在恢复期间发生变化", { path: entry.path });
      }
    }
    const governed = clearHostState(stage, factoryName);
    if (manifest.databases.business === businessName) clearHostState(stage, businessName);
    if (manifest.registry) {
      await restoreRegistryBundle(stage, manifest.registry);
      if (governed) await Bun.write(join(stage, "factory.json"), `${canonicalJson({ governance: { registry: "registry" } })}\n`);
      await retryWindowsFileOperation(() => rmSync(join(stage, bundleName)));
    }
    await publish(stage, target);
    return {
      root: target,
      factory_database: join(target, factoryName),
      business_database: manifest.databases.business ? join(target, manifest.databases.business) : null,
      secrets_missing: manifest.secrets,
      ...(manifest.registry ? { registry: join(target, "registry") } : {}),
    };
  } catch (error) { return discardStage(stage, error); }
}
