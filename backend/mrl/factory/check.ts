import { documentKind, documentPosition, documentSubject, loadRegistry, type HostDiagnostic, type Registry, type SpecDocument } from "./registry";
import { checkFactoryKernel, type FactoryResolution } from "./references";
import { registryKinds, schemaForKind, validate } from "./schema";
import type { RuleLocation } from "../rules";

export interface RegistryDiagnostic extends HostDiagnostic { severity: "error" | "warning" | "info"; }
export interface RegistryCheckResult {
  root: string;
  ok: boolean;
  diagnostics: RegistryDiagnostic[];
  resolutions: FactoryResolution[];
  stats: Record<string, number>;
  registry: Registry;
}

function asDiagnostic(document: SpecDocument | undefined, diagnostic: HostDiagnostic, severity: RegistryDiagnostic["severity"] = "error"): RegistryDiagnostic {
  if (!document) return { ...diagnostic, severity };
  return {
    ...diagnostic,
    severity,
    file: document.file,
    line: diagnostic.line ?? document.line,
    doc: document.index,
    subject: documentSubject(document),
  };
}
function validateDocument(document: SpecDocument): RegistryDiagnostic[] {
  const diagnostics: RegistryDiagnostic[] = [];
  const kind = documentKind(document);
  if (schemaForKind(kind) && !registryKinds.includes(kind as typeof registryKinds[number])) {
    diagnostics.push(asDiagnostic(document, { code: "E-LAYOUT", subject: documentSubject(document), message: `${kind} is a record and cannot be declared in the Registry`, path: "kind" }));
    return diagnostics;
  }
  if (schemaForKind(kind) && kind !== document.expectedKind) {
    diagnostics.push(asDiagnostic(document, { code: "E-LAYOUT", subject: documentSubject(document), message: `该位置只能放 ${document.expectedKind}，但文档的 kind 是 ${kind}`, path: "kind" }));
    return diagnostics;
  }
  for (const error of validate(document.data)) {
    const position = documentPosition(document, error.path);
    diagnostics.push(asDiagnostic(document, { code: "E-SCHEMA", subject: documentSubject(document), message: `${error.path || "(root)"}: ${error.message}`, path: error.path, line: position[0], col: position[1] }));
  }
  return diagnostics;
}

export function checkRegistry(rootOrRegistry: string | Registry, onDiagnosticRule?: (location: RuleLocation) => void): RegistryCheckResult {
  const registry = typeof rootOrRegistry === "string" ? loadRegistry(rootOrRegistry) : rootOrRegistry;
  const diagnostics: RegistryDiagnostic[] = registry.diagnostics.map((diagnostic) => asDiagnostic(undefined, diagnostic));
  const valid: SpecDocument[] = [];
  const stats: Record<string, number> = {};
  for (const document of registry.docs) {
    const documentDiagnostics = validateDocument(document);
    diagnostics.push(...documentDiagnostics);
    if (documentDiagnostics.length) continue;
    const kind = documentKind(document);
    if (kind) stats[`kind:${kind}`] = (stats[`kind:${kind}`] ?? 0) + 1;
    valid.push(document);
  }
  const kernel = checkFactoryKernel(valid, registry.constitution, diagnostics, onDiagnosticRule);
  diagnostics.splice(0, diagnostics.length, ...kernel.diagnostics);
  const { resolutions } = kernel;
  stats.edges = resolutions.length;
  stats.conversions = resolutions.filter((item) => item.status === "convert").length;
  return { root: registry.root, ok: diagnostics.every((item) => item.severity !== "error"), diagnostics, resolutions, stats, registry };
}
