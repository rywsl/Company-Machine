import type { Database } from "bun:sqlite";
import { existsSync, readFileSync } from "node:fs";
import { relative, resolve } from "node:path";
import { checkRegistry, type RegistryCheckResult } from "../../mrl/factory/check";
import { listFiles } from "../../mrl/factory/paths";
import { documentKey, isConstitution, layoutKind, registryFromTexts, type Registry } from "../../mrl/factory/registry";
import { canonicalBytes, canonicalJson, digestBytes, digestFile, type Clock } from "./foundation";
import { FactoryError } from "./errors";

function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)); }
function isObject(value: unknown): value is Record<string, any> { return Boolean(value) && typeof value === "object" && !Array.isArray(value); }

export class Snapshot {
  constructor(readonly root: string, readonly epoch: string, readonly docs: Record<string, any>, readonly result: ReturnType<typeof checkRegistry>) {}
  keys(kind?: string): string[] { return Object.keys(this.docs).filter((key) => !kind || key.startsWith(`${kind}:`)).sort(); }
  key(kind: string, ref: string): string { return ref.includes(":") ? ref : `${kind}:${ref}`; }
  doc(key: string): any { if (!(key in this.docs)) throw new FactoryError("SPEC_NOT_FOUND", `找不到 Spec ${key}`, { key }); return clone(this.docs[key]); }
  spec(key: string): any { return clone(this.doc(key).spec); }
  typeKey(typeId: string): string {
    const match = /^(.+)@([0-9]+)(?:\.([0-9]+))?$/.exec(typeId);
    if (!match) throw new FactoryError("TYPE_NOT_FOUND", `类型 ${typeId} 在快照中不存在`, { tid: typeId });
    const major = Number(match[2]);
    const candidates = this.keys("Type").filter((key) => {
      const document = this.docs[key];
      const version = String(document.metadata?.version ?? "");
      return document.metadata?.name === match[1] && version.startsWith(`${major}.`) && document.metadata?.lifecycle !== "retired";
    }).sort((a, b) => Number(String(this.docs[b].metadata.version).split(".")[1]) - Number(String(this.docs[a].metadata.version).split(".")[1]));
    const found = candidates[0];
    if (!found) throw new FactoryError("TYPE_NOT_FOUND", `类型 ${typeId} 在快照中不存在`, { tid: typeId });
    return found;
  }
  isSubtype(type: string, parent: string): boolean {
    if (type === parent) return this.keys("Type").some((key) => this.docs[key].metadata?.name === type.split("@")[0]);
    const visited = new Set<string>();
    const visit = (current: string): boolean => {
      if (visited.has(current)) return false;
      visited.add(current);
      let key: string;
      try { key = this.typeKey(current); } catch { return false; }
      const extendsRef = this.docs[key].spec?.extends;
      if (typeof extendsRef !== "string") return false;
      if (extendsRef === parent) return true;
      return visit(extendsRef);
    };
    return visit(type);
  }
  factsDecl(typeId: string): Record<string, string> {
    const spec = this.spec(this.typeKey(typeId));
    const parent = typeof spec.extends === "string" ? this.factsDecl(spec.extends) : {};
    return { ...parent, ...(isObject(spec.facts) ? clone(spec.facts) : {}) };
  }
  validators(typeId: string): any[] { const spec = this.spec(this.typeKey(typeId)); return Array.isArray(spec.validators) ? clone(spec.validators) : []; }
  properties(typeId: string): Record<string, unknown> { const spec = this.spec(this.typeKey(typeId)); return isObject(spec.properties) ? clone(spec.properties) : {}; }
  always(typeId: string): Set<string> {
    const spec = this.spec(this.typeKey(typeId));
    const values = typeof spec.extends === "string" ? this.always(spec.extends) : new Set<string>();
    for (const item of Array.isArray(spec.always) ? spec.always : []) if (typeof item === "string") values.add(item);
    return values;
  }
  compiled(subject: string): unknown { throw new FactoryError("MRL_COMPILED_MISSING", `没有可用的编译表达式 ${subject}`, { subject }); }
}

interface RegistryTree {
  root: string;
  epoch: string;
  digest: string;
  sources: Record<string, string>;
}

async function readRegistryTree(root: string): Promise<RegistryTree> {
  const absolute = resolve(root);
  if (!existsSync(absolute)) throw new Error(`Registry root does not exist: ${root}`);
  const entries: Record<string, string> = Object.create(null);
  const sources: Record<string, string> = Object.create(null);
  for (const file of listFiles(absolute)) {
    const path = relative(absolute, file).replaceAll("\\", "/");
    if (layoutKind(path) || isConstitution(path)) {
      const bytes = readFileSync(file);
      entries[path] = digestBytes(bytes);
      sources[path] = bytes.toString("utf8");
    } else {
      entries[path] = await digestFile(file);
    }
  }
  const digest = digestBytes(canonicalBytes(entries));
  return { root: absolute, epoch: epochOf(digest), digest, sources };
}

const epochOf = (digest: string) => `e-${digest.slice(7, 23)}`;

function loadRegistryTree(tree: RegistryTree): Snapshot {
  const result = checkRegistry(registryFromTexts(tree.root, tree.sources));
  const errors = result.diagnostics.filter((diagnostic) => diagnostic.severity === "error");
  if (errors.length) throw new FactoryError("REGISTRY_INVALID", `Registry 有 ${errors.length} 个错误，不能作为运行快照`, { diags: errors });
  return snapshotFromResult(tree.epoch, result);
}

function snapshotFromResult(epoch: string, result: RegistryCheckResult): Snapshot {
  const docs: Record<string, any> = {};
  for (const document of result.registry.docs) { const key = documentKey(document); if (key) docs[key] = clone(document.data); }
  return new Snapshot(result.root, epoch, docs, result);
}

export async function loadSnapshot(root: string, _clock?: Clock): Promise<Snapshot> {
  return loadRegistryTree(await readRegistryTree(root));
}

type FrozenResult = Omit<RegistryCheckResult, "root" | "registry"> & { registry: Omit<Registry, "root"> };
type FrozenRow = { tree_digest: string; payload: string; payload_digest: string };
const frozenDigest = (treeDigest: string, payload: string) => digestBytes(new TextEncoder().encode(`${treeDigest}\n${payload}`));

/** Persists the validated execution inputs that a Run or Job binds by epoch. */
export class RegistrySnapshotStore {
  constructor(private readonly db: Database) {
    db.exec(`CREATE TABLE IF NOT EXISTS registry_snapshots (
      epoch TEXT PRIMARY KEY,
      tree_digest TEXT NOT NULL,
      payload TEXT NOT NULL,
      payload_digest TEXT NOT NULL
    )`);
  }

  async capture(registry: string): Promise<Snapshot> {
    const tree = await readRegistryTree(registry);
    return this.freeze(tree, tree.root);
  }

  async materialize(epoch: string, fallbackRegistry?: string): Promise<Snapshot> {
    const stored = this.find(epoch);
    if (stored) return this.restore(epoch, stored);
    if (!fallbackRegistry || !existsSync(resolve(fallbackRegistry))) {
      throw new FactoryError("EPOCH_NOT_FOUND", "找不到运行记录绑定的 Registry 快照", { epoch });
    }
    const fallback = await readRegistryTree(fallbackRegistry);
    if (fallback.epoch !== epoch) {
      throw new FactoryError("EPOCH_MISMATCH", "Registry 内容与运行记录绑定的快照不符", { epoch, registry_epoch: fallback.epoch });
    }
    return this.freeze(fallback);
  }

  private find(epoch: string): FrozenRow | null {
    return this.db.query<FrozenRow>("SELECT tree_digest, payload, payload_digest FROM registry_snapshots WHERE epoch = ?").get(epoch);
  }

  private freeze(tree: RegistryTree, root?: string): Snapshot {
    const existing = this.find(tree.epoch);
    if (existing) return this.restore(tree.epoch, existing, tree.digest, root);

    const snapshot = loadRegistryTree(tree);
    const { root: _resultRoot, registry, ...result } = snapshot.result;
    const { root: _registryRoot, ...parsedRegistry } = registry;
    const payload = canonicalJson(clone({ version: 1, result: { ...result, registry: parsedRegistry } }));
    this.db.query(`
      INSERT INTO registry_snapshots (epoch, tree_digest, payload, payload_digest) VALUES (?, ?, ?, ?)
      ON CONFLICT(epoch) DO NOTHING
    `).run(tree.epoch, tree.digest, payload, frozenDigest(tree.digest, payload));
    return this.restore(tree.epoch, this.find(tree.epoch)!, tree.digest, root);
  }

  private restore(epoch: string, row: FrozenRow, expectedDigest?: string, root = `registry:${epoch}`): Snapshot {
    if (expectedDigest !== undefined && row.tree_digest !== expectedDigest) {
      throw new FactoryError("EPOCH_MISMATCH", "Registry 快照摘要冲突", { epoch });
    }
    if (!/^sha256:[0-9a-f]{64}$/.test(row.tree_digest) || epochOf(row.tree_digest) !== epoch ||
        frozenDigest(row.tree_digest, row.payload) !== row.payload_digest) {
      throw new FactoryError("EPOCH_CORRUPT", "Registry 快照的摘要不匹配", { epoch });
    }
    try {
      const stored = JSON.parse(row.payload) as { version: number; result: FrozenResult };
      if (stored.version !== 1 || stored.result.ok !== true) throw new Error("Invalid frozen Registry payload");
      return snapshotFromResult(epoch, { ...stored.result, root, registry: { ...stored.result.registry, root } });
    } catch {
      throw new FactoryError("EPOCH_CORRUPT", "Registry 快照的格式无法读取", { epoch });
    }
  }
}
