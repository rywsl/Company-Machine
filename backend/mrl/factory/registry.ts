import { readFileSync } from "node:fs";
import { relative } from "node:path";
import { listFiles } from "./paths";
import { loadDocuments, YamlError } from "./yamlio";

export const layout: Array<[string, string]> = [
  ["types/*.yaml", "Type"], ["runtimes/*.yaml", "OperatorRuntime"], ["policies/*.yaml", "Policy"],
  ["lines/*.yaml", "Line"], ["goldens/*.yaml", "GoldenSet"], ["baselines/*.yaml", "Baseline"],
  ["stations/*.yaml", "Station"], ["stations/*/station.yaml", "Station"], ["converters/*/station.yaml", "Station"],
  ["machines/*/machine.yaml", "Machine"], ["agents/*.yaml", "Agent"],
];

export interface HostDiagnostic { code: string; subject: string; message: string; file?: string; doc?: number; path?: string; line?: number; col?: number; }
export interface SpecDocument { file: string; index: number; line: number; data: unknown; expectedKind: string; positions: Record<string, [number, number]>; }
export interface Registry { root: string; docs: SpecDocument[]; constitution: Array<{ file: string; source: string }>; diagnostics: HostDiagnostic[]; }

function globMatch(pattern: string, rel: string): boolean {
  const patternParts = pattern.split("/");
  const valueParts = rel.split("/");
  if (patternParts.length !== valueParts.length) return false;
  const escape = (value: string) => value.replace(/[\\^$.*+?()[\]{}|]/g, "\\$&");
  return patternParts.every((part, index) => {
    const regex = new RegExp(`^${part.split("*").map(escape).join(".*")}$`);
    return regex.test(valueParts[index]);
  });
}
export function layoutKind(rel: string): string | undefined {
  for (const [pattern, kind] of layout) if (globMatch(pattern, rel.replaceAll("\\", "/")) || globMatch(pattern.replace(/\.yaml$/, ".yml"), rel.replaceAll("\\", "/"))) return kind;
  return undefined;
}
export function isConstitution(rel: string): boolean { return /^constitution\/[^/]+\.mrl$/.test(rel.replaceAll("\\", "/")); }

export function readTree(root: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const file of listFiles(root)) {
    const rel = relative(root, file).replaceAll("\\", "/");
    if (layoutKind(rel) || isConstitution(rel)) result[rel] = readFileSync(file, "utf8");
  }
  return result;
}
export function registryFromTexts(root: string, files: Record<string, string>): Registry {
  const registry: Registry = { root, docs: [], constitution: [], diagnostics: [] };
  for (const file of Object.keys(files).sort()) {
    const text = files[file];
    if (isConstitution(file)) { registry.constitution.push({ file, source: text }); continue; }
    const expectedKind = layoutKind(file);
    if (!expectedKind) continue;
    try {
      for (const document of loadDocuments(text)) registry.docs.push({ file, index: document.index, line: document.line, data: document.data, expectedKind, positions: document.positions });
    } catch (error) {
      const yaml = error instanceof YamlError ? error : new YamlError(error instanceof Error ? error.message : "invalid YAML");
      registry.diagnostics.push({ code: "E-YAML", subject: `File:${file}`, message: `YAML error at ${yaml.line}:${yaml.col}: ${yaml.message}`, file, line: yaml.line, col: yaml.col });
    }
  }
  return registry;
}
export function loadRegistry(root: string): Registry { return registryFromTexts(root, readTree(root)); }

export function documentKind(document: SpecDocument): string { return isObject(document.data) && typeof document.data.kind === "string" ? document.data.kind : ""; }
export function documentKey(document: SpecDocument): string | undefined {
  if (!isObject(document.data) || !isObject(document.data.metadata)) return undefined;
  const kind = documentKind(document); const name = document.data.metadata.name; const version = document.data.metadata.version;
  if (!kind || typeof name !== "string" || (typeof version !== "string" && typeof version !== "number") || typeof version === "boolean") return undefined;
  return `${kind}:${name}@${version}`;
}
export function documentSubject(document: SpecDocument): string { return documentKey(document) ?? `File:${document.file}#${document.index}`; }
export function documentPosition(document: SpecDocument, path: string): [number, number] {
  let current = path;
  while (current) { const position = document.positions[current]; if (position) return position; const cut = Math.max(current.lastIndexOf("."), current.lastIndexOf("[")); current = cut > 0 ? current.slice(0, cut) : ""; }
  return document.positions[""] ?? [document.line, 1];
}
function isObject(value: unknown): value is Record<string, unknown> { return Boolean(value) && typeof value === "object" && !Array.isArray(value); }
