import { Database } from "bun:sqlite";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, renameSync, statSync, unlinkSync } from "node:fs";
import { open } from "node:fs/promises";
import { dirname, join } from "node:path";
import { canonicalBytes, canonicalJson, digestBytes, digestFile, randomUuid, utcIso, type Clock, type IdSource } from "./foundation";
import { FactoryError } from "./errors";
import { listFiles } from "../../mrl/factory/paths";

const TYPE = /^[A-Za-z0-9][A-Za-z0-9._-]*@[0-9]+\.[0-9]+$/;
const DIGEST = /^sha256:[0-9a-f]{64}$/;
const MEDIA: Record<string, string> = { txt: "text/plain", json: "application/json", md: "text/markdown", mp4: "video/mp4", srt: "application/x-subrip", vtt: "text/vtt" };
const bun = () => (globalThis as { Bun?: { file: (path: string) => unknown; write: (path: string, source: unknown) => Promise<number> } }).Bun;
async function copyFile(source: string, destination: string): Promise<void> {
  const runtime = bun();
  if (!runtime) throw new FactoryError("RUNTIME_UNAVAILABLE", "Bun runtime is required");
  await runtime.write(destination, runtime.file(source));
}
function checkDigest(digest: string): void { if (!DIGEST.test(digest)) throw new FactoryError("DIGEST_INVALID", "摘要格式不合法", { digest }); }
function jsonParse(value: unknown): any { return typeof value === "string" ? JSON.parse(value) : value; }
function isObject(value: unknown): value is Record<string, unknown> { return Boolean(value) && typeof value === "object" && !Array.isArray(value); }

export interface FileRef { path: string; digest: string; size: number; mediaType: string; }
export interface Bean { id: string; type: string; meta: Record<string, unknown>; facts: Record<string, unknown>; payload: FileRef[]; sealed: boolean; createdAt: string; expiredAt?: string; }
export interface BeanDraft { type: string; meta?: Record<string, unknown>; files: Record<string, string>; }

export function mediaTypeOf(path: string): string {
  const name = path.replaceAll("\\", "/").split("/").pop() ?? path;
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "application/octet-stream" : MEDIA[name.slice(dot + 1).toLowerCase()] ?? "application/octet-stream";
}
function checkPayloadPath(path: string): void {
  if (!path || path.startsWith("/") || /[\\:*?"<>|]/.test(path)) throw new FactoryError("PAYLOAD_PATH_INVALID", "payload 路径不合法", { path });
  const parts = path.replaceAll("\\", "/").split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) throw new FactoryError("PAYLOAD_PATH_INVALID", "payload 路径不合法", { path });
}

export class Warehouse {
  private readonly root: string;
  constructor(root: string, readonly db: Database, readonly clock: Clock, readonly ids?: IdSource) {
    this.root = root;
    mkdirSync(join(root, "cas"), { recursive: true });
    mkdirSync(join(root, "tmp"), { recursive: true });
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS beans (id TEXT PRIMARY KEY, type TEXT NOT NULL, meta_json TEXT NOT NULL, facts_json TEXT NOT NULL, sealed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, expired_at TEXT);
      CREATE TABLE IF NOT EXISTS payload_files (bean_id TEXT NOT NULL REFERENCES beans(id), path TEXT NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL, media_type TEXT NOT NULL, PRIMARY KEY(bean_id,path));
      CREATE TABLE IF NOT EXISTS seals (id INTEGER PRIMARY KEY AUTOINCREMENT, bean_id TEXT NOT NULL REFERENCES beans(id), job_id TEXT, report_digest TEXT NOT NULL, facts_json TEXT NOT NULL, sealed_at TEXT NOT NULL, UNIQUE(bean_id, report_digest, job_id));
      CREATE TABLE IF NOT EXISTS lineage (job_id TEXT NOT NULL, direction TEXT NOT NULL, port TEXT NOT NULL, ordinal INTEGER NOT NULL, bean_id TEXT NOT NULL REFERENCES beans(id), PRIMARY KEY(job_id,direction,port,ordinal));
      CREATE TABLE IF NOT EXISTS bean_labels (bean_id TEXT NOT NULL REFERENCES beans(id), key TEXT NOT NULL, value TEXT NOT NULL, set_by TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(bean_id,key));
      CREATE INDEX IF NOT EXISTS beans_type ON beans(type);
      CREATE INDEX IF NOT EXISTS lineage_bean ON lineage(bean_id);
    `);
  }
  blobPath(digest: string): string {
    checkDigest(digest);
    const hex = digest.slice("sha256:".length);
    return join(this.root, "cas", "sha256", hex.slice(0, 2), hex.slice(2));
  }
  async putFile(source: string): Promise<string> {
    return (await this.storeFile(source)).digest;
  }
  private async storeFile(source: string): Promise<{ digest: string; size: number }> {
    const scratch = join(this.root, "tmp");
    mkdirSync(scratch, { recursive: true });
    const part = join(scratch, `cas-${randomUuid()}.part`);
    let ownsPart = false;
    const hash = createHash("sha256");
    const buffer = new Uint8Array(1024 * 1024);
    let size = 0;
    const input = await open(source, "r");
    try {
      try {
        const output = await open(part, "wx");
        ownsPart = true;
        try {
          while (true) {
            const { bytesRead } = await input.read(buffer, 0, buffer.length, null);
            if (bytesRead === 0) break;
            let offset = 0;
            while (offset < bytesRead) {
              const { bytesWritten } = await output.write(buffer, offset, bytesRead - offset, null);
              if (bytesWritten === 0) throw new Error("CAS staging file stopped accepting bytes");
              offset += bytesWritten;
            }
            hash.update(buffer.subarray(0, bytesRead));
            size += bytesRead;
          }
        } finally { await output.close(); }
      } finally { await input.close(); }

      const digest = `sha256:${hash.digest("hex")}`;
      const destination = this.blobPath(digest);
      if (!existsSync(destination)) {
        mkdirSync(dirname(destination), { recursive: true });
        // Serialize only publication across processes sharing the warehouse database.
        this.db.transaction(() => {
          if (!existsSync(destination)) {
            renameSync(part, destination);
            ownsPart = false;
          }
        }).immediate();
      }
      return { digest, size };
    } finally {
      if (ownsPart) unlinkSync(part);
    }
  }
  async add(draft: BeanDraft): Promise<Bean> {
    if (!TYPE.test(draft.type)) throw new FactoryError("BEAN_TYPE_INVALID", "Bean 类型必须带 minor", { type: draft.type });
    const meta = draft.meta ?? {};
    canonicalJson(meta);
    const payload: FileRef[] = [];
    for (const [path, source] of Object.entries(draft.files)) {
      checkPayloadPath(path);
      const { digest, size } = await this.storeFile(source);
      payload.push({ path, digest, size, mediaType: mediaTypeOf(path) });
    }
    payload.sort((a, b) => a.path.localeCompare(b.path));
    const identity = { type: draft.type, meta, payload: payload.map((item) => ({ path: item.path, digest: item.digest, size: item.size, media_type: item.mediaType })) };
    const id = digestBytes(canonicalBytes(identity));
    const timestamp = utcIso(this.clock.now());
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const existing = this.db.query("SELECT id FROM beans WHERE id = ?").get(id) as any;
      if (!existing) {
        this.db.query("INSERT INTO beans (id,type,meta_json,facts_json,sealed,created_at,expired_at) VALUES (?,?,?,?,0,?,NULL)").run(id, draft.type, canonicalJson(meta), canonicalJson({}), timestamp);
        for (const item of payload) this.db.query("INSERT INTO payload_files (bean_id,path,digest,size,media_type) VALUES (?,?,?,?,?)").run(id, item.path, item.digest, item.size, item.mediaType);
      }
      this.db.exec("COMMIT");
    } catch (error) { this.db.exec("ROLLBACK"); throw error; }
    return this.get(id);
  }
  get(id: string): Bean {
    const row = this.db.query("SELECT * FROM beans WHERE id = ?").get(id) as any;
    if (!row) throw new FactoryError("BEAN_NOT_FOUND", `Bean ${id} 不存在`, { beanId: id });
    const payload = (this.db.query("SELECT path,digest,size,media_type FROM payload_files WHERE bean_id = ? ORDER BY path").all(id) as any[]).map((item) => ({ path: item.path, digest: item.digest, size: item.size, mediaType: item.media_type }));
    return { id: row.id, type: row.type, meta: jsonParse(row.meta_json), facts: jsonParse(row.facts_json), payload, sealed: Boolean(row.sealed), createdAt: row.created_at, ...(row.expired_at ? { expiredAt: row.expired_at } : {}) };
  }
  list(options: { typePrefix?: string; sealedOnly?: boolean; includeExpired?: boolean } = {}): Bean[] {
    const clauses: string[] = [];
    if (options.sealedOnly ?? true) clauses.push("sealed = 1");
    if (!options.includeExpired) clauses.push("expired_at IS NULL");
    const where = clauses.length ? ` WHERE ${clauses.join(" AND ")}` : "";
    const rows = this.db.query(`SELECT id FROM beans${where} ORDER BY created_at,id`).all() as any[];
    return rows.map((row) => this.get(row.id)).filter((bean) => !options.typePrefix || bean.type.startsWith(options.typePrefix));
  }
  sealMany(items: Array<[string, Record<string, unknown>]>, jobId: string | null, reportDigest: string): void {
    checkDigest(reportDigest);
    this.db.transaction(() => {
      for (const [id, facts] of items) {
        const row = this.db.query("SELECT facts_json,sealed FROM beans WHERE id = ?").get(id) as any;
        if (!row) throw new FactoryError("BEAN_NOT_FOUND", `Bean ${id} 不存在`, { beanId: id });
        const factsJson = canonicalJson(facts);
        const firstSeal = this.db.query("SELECT 1 FROM seals WHERE bean_id = ? LIMIT 1").get(id);
        if (!firstSeal && !row.sealed) {
          this.db.query("UPDATE beans SET facts_json = ?, sealed = 1 WHERE id = ?").run(factsJson, id);
          this.db.query("INSERT INTO seals (bean_id,job_id,report_digest,facts_json,sealed_at) VALUES (?,?,?,?,?)").run(id, jobId, reportDigest, factsJson, utcIso(this.clock.now()));
        } else if (row.facts_json !== factsJson) throw new FactoryError("FACTS_CONFLICT", "封存 facts 与首次封存不一致", { beanId: id });
        else if (!this.db.query("SELECT 1 FROM seals WHERE bean_id = ? AND report_digest = ? AND ifnull(job_id,'') = ifnull(?,'')").get(id, reportDigest, jobId)) this.db.query("INSERT INTO seals (bean_id,job_id,report_digest,facts_json,sealed_at) VALUES (?,?,?,?,?)").run(id, jobId, reportDigest, factsJson, utcIso(this.clock.now()));
      }
    }).immediate();
  }
  async materialize(id: string, destination: string, layout: "port" | "bean"): Promise<void> {
    const bean = this.get(id);
    if (bean.expiredAt) throw new FactoryError("BEAN_EXPIRED", "Bean 已过期", { beanId: id });
    if (layout !== "port" && layout !== "bean") throw new Error("layout must be port or bean");
    mkdirSync(destination, { recursive: true });
    const prefix = layout === "bean" ? join(destination, "payload") : destination;
    for (const file of bean.payload) { const target = join(prefix, ...file.path.replaceAll("\\", "/").split("/")); mkdirSync(join(target, ".."), { recursive: true }); await copyFile(this.blobPath(file.digest), target); }
    const body = { id: bean.id, type: bean.type, facts: bean.facts, meta: bean.meta, payload: bean.payload.map((file) => ({ path: file.path, digest: file.digest, size: file.size, media_type: file.mediaType })) };
    const runtime = bun(); if (!runtime) throw new FactoryError("RUNTIME_UNAVAILABLE", "Bun runtime is required"); await runtime.write(join(destination, "bean.json"), canonicalBytes(body));
  }
  recordLineage(jobId: string, inputs: Record<string, string[]>, outputs: Record<string, string[]>): void {
    this.db.transaction(() => {
      for (const ids of [...Object.values(inputs), ...Object.values(outputs)]) for (const id of ids) { this.get(id); }
      this.db.query("DELETE FROM lineage WHERE job_id = ?").run(jobId);
      for (const [direction, ports] of [["in", inputs], ["out", outputs]] as const) for (const port of Object.keys(ports).sort()) ports[port].forEach((id, ordinal) => this.db.query("INSERT INTO lineage (job_id,direction,port,ordinal,bean_id) VALUES (?,?,?,?,?)").run(jobId, direction, port, ordinal, id));
    }).immediate();
  }
  lineage(id: string, direction: "up" | "down"): { id: string; direction: "up" | "down"; jobs: Array<{ jobId: string; inputs: Record<string, string[]>; outputs: Record<string, string[]> }>; beans: string[] } {
    this.get(id);
    const visitedBeans = new Set([id]); const visitedJobs = new Set<string>(); const queue = [id];
    while (queue.length) {
      const current = queue.shift()!;
      const sourceDirection = direction === "up" ? "out" : "in";
      const rows = this.db.query("SELECT job_id FROM lineage WHERE bean_id = ? AND direction = ?").all(current, sourceDirection) as any[];
      for (const row of rows) {
        if (visitedJobs.has(row.job_id)) continue;
        visitedJobs.add(row.job_id);
        const targetDirection = direction === "up" ? "in" : "out";
        for (const item of this.db.query("SELECT bean_id FROM lineage WHERE job_id = ? AND direction = ?").all(row.job_id, targetDirection) as any[]) if (!visitedBeans.has(item.bean_id)) { visitedBeans.add(item.bean_id); queue.push(item.bean_id); }
      }
    }
    const jobs = [...visitedJobs].sort().map((jobId) => ({ jobId, inputs: this.ports(jobId, "in"), outputs: this.ports(jobId, "out") }));
    return { id, direction, jobs, beans: [...visitedBeans].filter((item) => item !== id).sort() };
  }
  private ports(jobId: string, direction: string): Record<string, string[]> { const result: Record<string, string[]> = {}; for (const row of this.db.query("SELECT port,bean_id FROM lineage WHERE job_id = ? AND direction = ? ORDER BY port,ordinal").all(jobId, direction) as any[]) (result[row.port] ??= []).push(row.bean_id); return result; }
  labels(id: string): Record<string, string> { this.get(id); return Object.fromEntries((this.db.query("SELECT key,value FROM bean_labels WHERE bean_id = ?").all(id) as any[]).map((row) => [row.key, row.value])); }
  label(id: string, key: string, value: string, by: string): void { this.get(id); this.db.query("INSERT INTO bean_labels (bean_id,key,value,set_by,at) VALUES (?,?,?,?,?) ON CONFLICT(bean_id,key) DO UPDATE SET value=excluded.value,set_by=excluded.set_by,at=excluded.at").run(id, key, value, by, utcIso(this.clock.now())); }
  async verify(): Promise<Array<Record<string, string>>> {
    const problems: Array<Record<string, string>> = [];
    const cas = join(this.root, "cas", "sha256");
    for (const file of listFiles(cas)) if (statSync(file).isFile()) { const parts = file.replaceAll("\\", "/").split("/"); const expected = `sha256:${parts.at(-2)}${parts.at(-1)}`; if (await digestFile(file) !== expected) problems.push({ kind: "digest_mismatch", path: file, bean_id: "", digest: expected }); }
    for (const row of this.db.query("SELECT bean_id,path,digest FROM payload_files").all() as any[]) if (!existsSync(this.blobPath(row.digest))) problems.push({ kind: "missing_blob", path: row.path, bean_id: row.bean_id, digest: row.digest });
    for (const row of this.db.query("SELECT id,type,meta_json,facts_json,sealed FROM beans").all() as any[]) {
      const payload = (this.db.query("SELECT path,digest,size,media_type FROM payload_files WHERE bean_id = ? ORDER BY path").all(row.id) as any[]).map((item) => ({ path: item.path, digest: item.digest, size: item.size, media_type: item.media_type }));
      const expected = digestBytes(canonicalBytes({ type: row.type, meta: jsonParse(row.meta_json), payload }));
      if (expected !== row.id) problems.push({ kind: "id_mismatch", path: "", bean_id: row.id, digest: expected });
      const hasSeal = Boolean(this.db.query("SELECT 1 FROM seals WHERE bean_id = ? LIMIT 1").get(row.id));
      if (Boolean(row.sealed) !== hasSeal) problems.push({ kind: "seal_flag", path: "", bean_id: row.id, digest: "" });
      if (!row.sealed && row.facts_json !== canonicalJson({})) problems.push({ kind: "facts_unsealed", path: "", bean_id: row.id, digest: "" });
    }
    return problems.sort((a, b) => [a.kind, a.path, a.bean_id, a.digest].join("\0").localeCompare([b.kind, b.path, b.bean_id, b.digest].join("\0")));
  }
}
export async function rejectExpired(warehouse: Warehouse, inputs: Record<string, string | string[]>): Promise<void> { for (const values of Object.values(inputs)) for (const id of Array.isArray(values) ? values : [values]) if (warehouse.get(id).expiredAt) throw new FactoryError("BEAN_EXPIRED", "Bean 已过期", { beanId: id }); }
