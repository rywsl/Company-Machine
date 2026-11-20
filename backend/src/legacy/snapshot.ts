import type { Database } from "bun:sqlite";
import { existsSync, readFileSync } from "node:fs";
import { relative, resolve } from "node:path";
import { checkRegistry, type RegistryCheckResult } from "../../mrl/factory/check";
import { listFiles } from "../../mrl/factory/paths";
import { documentKey, isConstitution, layoutKind, registryFromTexts, type Registry } from "../../mrl/factory/registry";
import { restoreCompiledSites, type CompiledSite } from "../../mrl/factory/sites";
import { canonicalBytes, canonicalJson, digestBytes, digestFile, type Clock } from "./foundation";
import { FactoryError } from "./errors";

function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)); }

type IndexedType = {
  key: string;
  supertypes: Set<string>;
  facts: Map<string, string>;
  properties: Map<string, { source: string; subject: string }>;
  always: Set<string>;
};

export function nominalTypeId(type: string): string {
  return type.replace(/(@\d+)\.\d+$/, "$1");
}

export class Snapshot {
  private readonly types = new Map<string, IndexedType>();

  constructor(readonly root: string, readonly epoch: string, readonly docs: Record<string, any>, readonly result: RegistryCheckResult) {
    const rows = result.typeRelations;
    for (const [type, key] of rows.type_key) {
      this.types.set(type, { key, supertypes: new Set(), facts: new Map(), properties: new Map(), always: new Set() });
    }
    for (const [type, parent] of rows.subtype) this.types.get(type)?.supertypes.add(parent);
    for (const [type, fact, declaration] of rows.type_fact) this.types.get(type)?.facts.set(fact, declaration);
    for (const [type, property] of rows.type_always_p) this.types.get(type)?.always.add(property);
    for (const [type, property, owner] of rows.type_prop) {
      const ownerKey = this.typeKey(owner);
      const source = docs[ownerKey]?.spec?.properties?.[property];
      if (typeof source === "string") this.types.get(type)?.properties.set(property, {
        source, subject: `${ownerKey}/properties.${property}`,
      });
    }
  }

  keys(kind?: string): string[] { return Object.keys(this.docs).filter((key) => !kind || key.startsWith(`${kind}:`)).sort(); }
  key(kind: string, ref: string): string { return ref.includes(":") ? ref : `${kind}:${ref}`; }
  doc(key: string): any { if (!(key in this.docs)) throw new FactoryError("SPEC_NOT_FOUND", `找不到 Spec ${key}`, { key }); return clone(this.docs[key]); }
  spec(key: string): any { return this.doc(key).spec; }
  private type(typeId: string): IndexedType {
    const found = this.types.get(typeId);
    if (!found) throw new FactoryError("TYPE_NOT_FOUND", `类型 ${typeId} 在快照中不存在`, { tid: typeId });
    return found;
  }
  typeKey(typeId: string): string { return this.type(typeId).key; }
  isSubtype(type: string, parent: string): boolean { return this.types.get(type)?.supertypes.has(parent) ?? false; }
  factsDecl(typeId: string): Record<string, string> {
    return Object.fromEntries(this.type(typeId).facts);
  }
  validators(typeId: string): any[] {
    const validators = this.docs[this.typeKey(typeId)]?.spec?.validators;
    return Array.isArray(validators) ? clone(validators) : [];
  }
  properties(typeId: string): Record<string, string> {
    return Object.fromEntries([...this.type(typeId).properties].map(([name, { source }]) => [name, source]));
  }
  propertySubject(typeId: string, name: string): string | undefined {
    return this.type(typeId).properties.get(name)?.subject;
  }
  always(typeId: string): Set<string> { return new Set(this.type(typeId).always); }
  compiled(subject: string): CompiledSite {
    const site = this.result.compiledSites.get(subject);
    if (!site) throw new FactoryError("SUBJECT_UNKNOWN", `快照中没有表达式位置 ${subject}`, { subject });
    return site;
  }
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

type FrozenResult = Omit<RegistryCheckResult, "root" | "registry" | "typeRelations" | "expressionSites" | "compiledSites"> & {
  registry: Omit<Registry, "root">;
  typeRelations?: RegistryCheckResult["typeRelations"];
  expressionSites?: RegistryCheckResult["expressionSites"];
};
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
    const { root: _resultRoot, registry, compiledSites: _compiledSites, ...result } = snapshot.result;
    const { root: _registryRoot, ...parsedRegistry } = registry;
    const payload = canonicalJson(clone({ version: 1, result: { ...result, registry: parsedRegistry } }));
    const inserted = this.db.query(`
      INSERT INTO registry_snapshots (epoch, tree_digest, payload, payload_digest) VALUES (?, ?, ?, ?)
      ON CONFLICT(epoch) DO NOTHING RETURNING epoch
    `).get(tree.epoch, tree.digest, payload, frozenDigest(tree.digest, payload));
    if (inserted && root !== undefined) return snapshot;
    if (inserted) {
      const frozenRoot = `registry:${tree.epoch}`;
      return snapshotFromResult(tree.epoch, { ...snapshot.result, root: frozenRoot, registry: { ...registry, root: frozenRoot } });
    }
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
      const registry = { ...stored.result.registry, root };
      const fresh = stored.result.typeRelations && stored.result.expressionSites ? undefined : checkRegistry(registry);
      const typeRelations = stored.result.typeRelations ?? fresh!.typeRelations;
      const expressionSites = stored.result.expressionSites ?? fresh!.expressionSites;
      const compiledSites = stored.result.expressionSites ? restoreCompiledSites(expressionSites) : fresh!.compiledSites;
      return snapshotFromResult(epoch, { ...stored.result, root, registry, typeRelations, expressionSites, compiledSites });
    } catch {
      throw new FactoryError("EPOCH_CORRUPT", "Registry 快照的格式无法读取", { epoch });
    }
  }
}
