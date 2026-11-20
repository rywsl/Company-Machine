import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import type { Context } from "./context";
import { FactoryError } from "./errors";
import { canonicalJson, utcIso } from "./foundation";
import { loadSnapshot, Snapshot } from "./snapshot";

const pointerNames = ["active", "candidate", "previous"] as const;
type Pointer = typeof pointerNames[number];
type Pointers = Record<Pointer, string | null>;
type EpochRow = { commit: string; tree: string; built_at: string };
type GitChild = { exited: Promise<number>; stdout: ReadableStream<Uint8Array>; stderr: ReadableStream<Uint8Array>; kill(signal: number): void };
type GitRuntime = {
  spawn(argv: string[], options: Record<string, unknown>): GitChild;
  Archive: new (file: Blob) => { files(): Promise<Map<string, Blob>> };
};
const bun = Bun as unknown as GitRuntime;
const gitObjectId = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;

function validArchivePart(part: string): boolean {
  if (!part || part === "." || part === ".." || /[\\:\0]/.test(part)) return false;
  return process.platform !== "win32" || !/[<>"|?*\x00-\x1f]|[. ]$|^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(part);
}

function publishDirectory(source: string, target: string): void {
  try {
    renameSync(source, target);
  } catch (error) {
    if (process.platform !== "win32" || (error as { code?: string }).code !== "EPERM") throw error;
    // Windows may retain a directory handle briefly after the files were verified.
    try {
      cpSync(source, target, { recursive: true, errorOnExist: true, force: false });
    } catch (copyError) {
      rmSync(target, { recursive: true, force: true });
      throw copyError;
    }
  }
}

export type Epoch = EpochRow & { retain: boolean };

interface EpochOptions {
  registry?: string;
  gitTimeoutSeconds?: number;
}

export class EpochStore {
  private readonly cache = new Map<string, Snapshot>();

  private constructor(readonly context: Context, readonly registry: string, private readonly timeoutMs: number) {
    context.db.exec(`
      CREATE TABLE IF NOT EXISTS epochs ("commit" TEXT PRIMARY KEY, tree TEXT NOT NULL, built_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS epoch_pointers (name TEXT PRIMARY KEY, "commit" TEXT NOT NULL REFERENCES epochs("commit"));
      CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, at TEXT NOT NULL,
        kind TEXT NOT NULL, job_id TEXT, data TEXT NOT NULL, run_id TEXT
      );
    `);
  }

  static async open(context: Context, options: EpochOptions = {}): Promise<EpochStore> {
    let config: { registry?: unknown; git_timeout_s?: unknown } | undefined;
    if (!options.registry) {
      try { config = JSON.parse(readFileSync(join(context.root, "factory.json"), "utf8"))?.governance; } catch { /* Handled below. */ }
    }
    const registry = options.registry ?? (typeof config?.registry === "string" ? config.registry : undefined);
    if (!registry) throw new FactoryError("REGISTRY_NOT_CONFIGURED", "没有配置 Registry Git 仓库");
    const absolute = options.registry ? resolve(registry) : resolve(context.root, registry);
    if (!existsSync(join(absolute, ".git"))) {
      throw new FactoryError("REGISTRY_NOT_CONFIGURED", "Registry 必须是 Git 工作树", { path: absolute });
    }
    const seconds = options.gitTimeoutSeconds ?? config?.git_timeout_s ?? 60;
    const timeout = typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0 ? seconds * 1_000 : 60_000;
    const store = new EpochStore(context, absolute, timeout);
    await store.git(["rev-parse", "--is-inside-work-tree"]);
    return store;
  }

  private async git(args: string[], input?: string): Promise<string> {
    const environment = Object.fromEntries(Object.entries(process.env).filter((entry): entry is [string, string] =>
      !/^GIT_/i.test(entry[0]) && typeof entry[1] === "string"));
    let child: GitChild;
    try {
      child = bun.spawn(["git", "-C", this.registry, "-c", `core.hooksPath=${process.platform === "win32" ? "NUL" : "/dev/null"}`, ...args], {
        stdin: input === undefined ? "ignore" : new Blob([input]), stdout: "pipe", stderr: "pipe",
        env: { ...environment, GIT_CONFIG_NOSYSTEM: "1", GIT_NO_LAZY_FETCH: "1", GIT_NO_REPLACE_OBJECTS: "1", GIT_TERMINAL_PROMPT: "0" },
      });
    } catch {
      throw new FactoryError("GIT_FAILED", "无法启动 Git", { args });
    }
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; child.kill(9); }, this.timeoutMs);
    try {
      const [status, stdout, stderr] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()]);
      if (timedOut || status !== 0) {
        throw new FactoryError("GIT_FAILED", timedOut ? "Git 命令超时" : "Git 命令失败", { args, stderr: stderr.trim().slice(0, 4_000) });
      }
      return stdout.trimEnd();
    } finally { clearTimeout(timer); }
  }

  private async verify(commit: string): Promise<string> {
    try {
      const id = await this.git(["rev-parse", "--verify", "--end-of-options", `${commit}^{commit}`]);
      if (gitObjectId.test(id)) return id;
    } catch { /* Give all unresolved commit references the same public error. */ }
    throw new FactoryError("COMMIT_NOT_FOUND", "找不到提交", { commit });
  }

  private find(commit: string): EpochRow | null {
    return this.context.db.query<EpochRow>('SELECT "commit", tree, built_at FROM epochs WHERE "commit" = ?').get(commit);
  }

  private hasCache(directory: string): boolean {
    return existsSync(join(directory, "registry")) && existsSync(join(directory, "resolutions.json"));
  }

  private async expand(commit: string, stage: string): Promise<Snapshot> {
    const archivePath = join(stage, "registry.tar");
    await this.git(["archive", "--format=tar", `--output=${archivePath}`, commit]);
    const registry = join(stage, "registry");
    mkdirSync(registry);
    const archive = new bun.Archive(new Blob([readFileSync(archivePath)]));
    const files = await archive.files();
    const seen = new Set<string>();
    for (const [path, file] of files) {
      const parts = path.split("/");
      const identity = process.platform === "win32" ? path.toLowerCase() : path;
      if (!parts.every(validArchivePart) || seen.has(identity)) throw new FactoryError("GIT_FAILED", "Registry 归档包含不安全的路径", { path });
      seen.add(identity);
      const target = join(registry, ...parts);
      mkdirSync(dirname(target), { recursive: true });
      await Bun.write(target, file);
    }
    rmSync(archivePath);
    const checked = await loadSnapshot(registry);
    writeFileSync(join(stage, "resolutions.json"), canonicalJson(checked.result.resolutions.map((item) => ({ ...item, origin: "precomputed" }))));
    const root = join(this.context.root, "epochs", commit, "registry");
    return new Snapshot(root, commit, checked.docs, { ...checked.result, root, registry: { ...checked.result.registry, root } });
  }

  async build(reference: string): Promise<Epoch> {
    const commit = await this.verify(reference);
    return this.buildVerified(commit);
  }

  private async buildVerified(commit: string): Promise<Epoch> {
    const epochs = join(this.context.root, "epochs");
    const destination = join(epochs, commit);
    const existing = this.find(commit);
    if (existing && this.hasCache(destination)) return { ...existing, retain: this.isRetained(commit) };
    const tree = await this.git(["rev-parse", "--verify", `${commit}^{tree}`]);
    mkdirSync(epochs, { recursive: true });
    const stage = mkdtempSync(join(epochs, `${commit}.part-`));
    const { db, clock } = this.context;
    try {
      const snapshot = await this.expand(commit, stage);
      let published = false;
      try {
        const built = db.transaction(() => {
          const concurrent = this.find(commit);
          if (concurrent && concurrent.tree !== tree) {
            throw new FactoryError("EPOCH_CORRUPT", "Git 提交的目录树与已登记的 epoch 不符", { commit });
          }
          if (concurrent) {
            const registry = join(destination, "registry");
            const resolutions = join(destination, "resolutions.json");
            mkdirSync(destination, { recursive: true });
            if (!existsSync(registry)) {
              publishDirectory(join(stage, "registry"), registry);
              published = true;
            }
            if (!existsSync(resolutions)) renameSync(join(stage, "resolutions.json"), resolutions);
            return concurrent;
          }
          // A previous interrupted build can leave an unregistered directory.
          rmSync(destination, { recursive: true, force: true });
          publishDirectory(stage, destination);
          published = true;
          const builtAt = utcIso(clock.now());
          db.query('INSERT INTO epochs ("commit", tree, built_at) VALUES (?, ?, ?)').run(commit, tree, builtAt);
          this.event("epoch.built", { commit, tree });
          return { commit, tree, built_at: builtAt };
        }).immediate();
        if (published) this.cache.set(commit, snapshot);
        return { ...built, retain: this.isRetained(commit) };
      } catch (error) {
        if (published && !this.find(commit)) rmSync(destination, { recursive: true, force: true });
        throw error;
      }
    } finally { rmSync(stage, { recursive: true, force: true }); }
  }

  async materialize(reference: string): Promise<Snapshot> {
    const commit = await this.verify(reference);
    const cached = this.cache.get(commit);
    if (cached && this.hasCache(join(this.context.root, "epochs", commit))) return cached;
    await this.buildVerified(commit);
    const built = this.cache.get(commit);
    if (built) return built;
    const registry = join(this.context.root, "epochs", commit, "registry");
    const source = await loadSnapshot(registry);
    const snapshot = new Snapshot(registry, commit, source.docs, source.result);
    this.cache.set(commit, snapshot);
    return snapshot;
  }

  current(): string {
    const active = this.pointers().active;
    if (!active) throw new FactoryError("EPOCH_UNSET", "没有 active epoch");
    return active;
  }

  pointers(): Pointers {
    const pointers: Pointers = { active: null, candidate: null, previous: null };
    const rows = this.context.db.query<{ name: Pointer; commit: string }>('SELECT name, "commit" FROM epoch_pointers').all();
    for (const row of rows) if (pointerNames.includes(row.name)) pointers[row.name] = row.commit;
    return pointers;
  }

  async init(reference: string, by: string): Promise<string> {
    if (this.pointers().active) throw new FactoryError("EPOCH_STATE_INVALID", "active 指针已经存在", { op: "init" });
    const { commit } = await this.build(reference);
    this.context.db.transaction(() => {
      if (this.pointers().active) throw new FactoryError("EPOCH_STATE_INVALID", "active 指针已经存在", { op: "init" });
      this.writePointer("active", commit, { by, reason: "init" });
    }).immediate();
    await this.syncRefs();
    return commit;
  }

  async setPointer(name: Pointer, reference: string | null, change: { by: string; reason: string }): Promise<void> {
    const commit = reference === null ? null : await this.verify(reference);
    this.context.db.transaction(() => this.writePointer(name, commit, change)).immediate();
    await this.syncRefs();
  }

  private writePointer(name: Pointer, commit: string | null, change: { by: string; reason: string }): void {
    if (!pointerNames.includes(name)) throw new Error("Invalid epoch pointer");
    const { db } = this.context;
    const from = this.pointers()[name];
    if (commit === null) db.query("DELETE FROM epoch_pointers WHERE name = ?").run(name);
    else {
      if (!this.find(commit)) throw new FactoryError("EPOCH_NOT_FOUND", "epoch 还没有构建", { commit });
      db.query('INSERT INTO epoch_pointers (name, "commit") VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET "commit" = excluded."commit"').run(name, commit);
    }
    this.event("epoch.pointer", { name, from, to: commit, ...change });
  }

  async syncRefs(): Promise<void> {
    // SQLite is authoritative. Re-read after publishing to converge with concurrent updates.
    for (let attempt = 0; attempt < 5; attempt++) {
      const pointers = this.pointers();
      if (pointerNames.some((name) => pointers[name] !== null && !gitObjectId.test(pointers[name]!))) {
        throw new FactoryError("EPOCH_CORRUPT", "数据库的 epoch 指针格式异常");
      }
      const commands = pointerNames.map((name) => pointers[name]
        ? `update refs/factory/${name} ${pointers[name]}`
        : `delete refs/factory/${name}`);
      await this.git(["update-ref", "--stdin"], commands.join("\n") + "\n");
      const current = this.pointers();
      if (pointerNames.every((name) => current[name] === pointers[name])) return;
    }
    throw new FactoryError("GIT_FAILED", "同步 Git 引用期间 epoch 指针持续变化");
  }

  async rollback(reference: string | undefined, by: string): Promise<string> {
    const verified = reference === undefined ? undefined : await this.verify(reference);
    const target = this.context.db.transaction(() => {
      const pointers = this.pointers();
      const commit = verified ?? pointers.previous;
      if (!commit) throw new FactoryError("EPOCH_UNSET", "没有可回滚的 previous");
      if (!this.find(commit)) throw new FactoryError("EPOCH_NOT_FOUND", "回滚目标还没有构建", { commit });
      if (pointers.active === commit) throw new FactoryError("EPOCH_STATE_INVALID", "不能回滚到当前 active", { commit });
      const change = { by, reason: "rollback" };
      this.writePointer("previous", pointers.active, change);
      this.writePointer("active", commit, change);
      if (pointers.candidate) this.writePointer("candidate", null, change);
      return commit;
    }).immediate();
    await this.syncRefs();
    return target;
  }

  async adopt(reference: string, by: string): Promise<string> {
    const commit = await this.verify(reference);
    const active = this.current();
    const reject = (reason: string, detail: object = {}) => new FactoryError("EPOCH_ADOPT_INVALID", "不满足宪法采纳条件", { reason, ...detail });
    const parents = (await this.git(["rev-list", "--parents", "-n", "1", commit])).split(" ");
    if (parents[1] !== active) throw reject("parent");
    const paths = (await this.git(["diff", "--name-only", "--no-renames", "-z", active, commit])).split("\0").filter(Boolean);
    if (!paths.length || paths.some((path) => !path.startsWith("constitution/"))) throw reject("paths", { paths });
    try {
      await this.git(["verify-commit", commit]);
    } catch {
      throw reject("unsigned");
    }
    await this.buildVerified(commit);
    this.context.db.transaction(() => {
      if (this.current() !== active) throw reject("parent");
      const change = { by, reason: "adopt" };
      this.writePointer("previous", active, change);
      this.writePointer("active", commit, change);
    }).immediate();
    await this.syncRefs();
    return commit;
  }

  async retain(reference: string): Promise<boolean> {
    return this.isRetained(await this.verify(reference));
  }

  list(): Epoch[] {
    return this.context.db.query<EpochRow>('SELECT "commit", tree, built_at FROM epochs ORDER BY built_at, "commit"').all()
      .map((row) => ({ ...row, retain: this.isRetained(row.commit) }));
  }

  async show(reference?: string): Promise<Epoch & { pointers: Pointer[] }> {
    const commit = reference === undefined ? this.current() : await this.verify(reference);
    const epoch = this.find(commit);
    if (!epoch) throw new FactoryError("COMMIT_NOT_FOUND", "找不到已构建的提交", { commit: reference ?? commit });
    const pointers = this.pointers();
    return { ...epoch, retain: this.isRetained(commit), pointers: pointerNames.filter((name) => pointers[name] === commit) };
  }

  private isRetained(commit: string): boolean {
    if (Object.values(this.pointers()).includes(commit)) return true;
    const { db } = this.context;
    const existing = new Set(db.query<{ name: string }>("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('runs','jobs','changesets')").all().map(({ name }) => name));
    return Boolean(
      existing.has("runs") && db.query("SELECT 1 FROM runs WHERE epoch = ? AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED') LIMIT 1").get(commit) ||
      existing.has("jobs") && db.query("SELECT 1 FROM jobs WHERE epoch = ? AND state NOT IN ('SEALED','DEAD') LIMIT 1").get(commit) ||
      existing.has("changesets") && db.query(`SELECT 1 FROM changesets WHERE (base_epoch = ? OR "commit" = ?) AND state NOT IN ('rejected','rolled-back','withdrawn') LIMIT 1`).get(commit, commit),
    );
  }

  private event(kind: string, data: object): void {
    const { db, clock, ids } = this.context;
    db.query("INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, NULL, ?)")
      .run(ids.new("ev"), utcIso(clock.now()), kind, canonicalJson(data));
  }
}
