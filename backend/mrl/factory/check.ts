import { documentKey, documentKind, documentPosition, documentSubject, loadRegistry, type HostDiagnostic, type Registry, type SpecDocument } from "./registry";
import { registryKinds, validate } from "./schema";

export interface RegistryDiagnostic extends HostDiagnostic { severity: "error" | "warning" | "info"; }
export interface RegistryCheckResult { root: string; ok: boolean; diagnostics: RegistryDiagnostic[]; stats: Record<string, number>; registry: Registry; }

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
  if (!kind || !registryKinds.includes(kind as typeof registryKinds[number])) {
    diagnostics.push(asDiagnostic(document, { code: "E-LAYOUT", subject: documentSubject(document), message: `unknown or non-registry kind ${kind || "(missing)"}`, path: "kind" }));
    return diagnostics;
  }
  if (kind !== document.expectedKind) {
    diagnostics.push(asDiagnostic(document, { code: "E-LAYOUT", subject: documentSubject(document), message: `this path requires ${document.expectedKind}, got ${kind}`, path: "kind" }));
    return diagnostics;
  }
  for (const error of validate(document.data)) {
    const position = documentPosition(document, error.path);
    diagnostics.push(asDiagnostic(document, { code: "E-SCHEMA", subject: documentSubject(document), message: `${error.path || "(root)"}: ${error.message}`, path: error.path, line: position[0], col: position[1] }));
  }
  return diagnostics;
}

export function checkRegistry(rootOrRegistry: string | Registry): RegistryCheckResult {
  const registry = typeof rootOrRegistry === "string" ? loadRegistry(rootOrRegistry) : rootOrRegistry;
  const diagnostics: RegistryDiagnostic[] = registry.diagnostics.map((diagnostic) => asDiagnostic(undefined, diagnostic));
  const seen = new Map<string, SpecDocument>();
  const stats: Record<string, number> = {};
  for (const document of registry.docs) {
    diagnostics.push(...validateDocument(document));
    const kind = documentKind(document);
    if (kind) stats[`kind:${kind}`] = (stats[`kind:${kind}`] ?? 0) + 1;
    const key = documentKey(document);
    if (key && !diagnostics.some((item) => item.file === document.file && item.doc === document.index && item.code === "E-SCHEMA")) {
      if (seen.has(key)) diagnostics.push(asDiagnostic(document, { code: "E-DUPLICATE", subject: key, message: `duplicate Spec key, first declared in ${seen.get(key)?.file}` }));
      else seen.set(key, document);
    }
  }
  if (registry.constitution.length === 0) diagnostics.push({ code: "E-CONSTITUTION", subject: "Registry", message: "constitution directory has no MRL source", severity: "error" });
  return { root: registry.root, ok: diagnostics.every((item) => item.severity !== "error"), diagnostics, stats, registry };
}
