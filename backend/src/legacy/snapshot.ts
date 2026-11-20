import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { checkRegistry } from "../../mrl/factory/check";
import { documentKey, documentSubject } from "../../mrl/factory/registry";
import { canonicalBytes, digestBytes, digestFile, type Clock } from "./foundation";
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

async function treeEpoch(root: string): Promise<string> {
  const { listFiles } = await import("../../mrl/factory/paths");
  const entries: Record<string, string> = {};
  for (const file of listFiles(root)) {
    const relative = file.slice(root.length + (root.endsWith("\\") || root.endsWith("/") ? 0 : 1)).replaceAll("\\", "/");
    entries[relative] = await digestFile(file);
  }
  const digest = digestBytes(canonicalBytes(entries));
  return `e-${digest.slice("sha256:".length, "sha256:".length + 16)}`;
}

export async function loadSnapshot(root: string, _clock?: Clock): Promise<Snapshot> {
  const absolute = resolve(root);
  if (!existsSync(absolute)) throw new Error(`Registry root does not exist: ${root}`);
  const result = checkRegistry(absolute);
  const errors = result.diagnostics.filter((diagnostic) => diagnostic.severity === "error");
  if (errors.length) throw new FactoryError("REGISTRY_INVALID", `Registry 有 ${errors.length} 个错误，不能作为运行快照`, { diags: errors });
  const docs: Record<string, any> = {};
  for (const document of result.registry.docs) { const key = documentKey(document); if (key) docs[key] = clone(document.data); }
  return new Snapshot(absolute, await treeEpoch(absolute), docs, result);
}
