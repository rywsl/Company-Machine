import type { SqliteStore } from "./db";
import type { Artifact, BusinessConversion, BusinessSystem, ProjectSourceKind } from "./models";
import { projectCredentialName } from "./operator";

type Json = Record<string, unknown>;
type Rewrite = (value: unknown) => string | undefined;
type GraphValidator = (graph: unknown) => string | undefined;
const SCHEMA = "mao.business-system/1";
const REDACTED = "[REDACTED]";
const LOCAL_PATH = "[LOCAL_PATH]";
const SENSITIVE = /^(?:api[_-]?key|(?:access|refresh|auth)[_-]?token|token|password|passwd|secret(?:s|[_-]?(?:value|ref|key))?|authorization|credentials?|cookie|vault[_-]?ref|local[_-]?folder|host[_-]?path|cwd|workspace(?:[_-]?(?:path|root))?|session[_-]?id)$/i;
const SECRET_FIELD_SUFFIX = /(?:[_-](?:api[_-]?key|password|secret|access[_-]?key|token)|[_-]secret[_-]access[_-]key)$/i;
const PRIVATE_TEXT = /\b(api[_-]?key|secret|token|password|authorization|cookie)(\s*[:=]\s*)(?:(?:bearer|basic)\s+)?(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi;
const ADDRESS = /[a-z][a-z\d+.-]*:\/\/[^\s<>"']+|(?<![\w:/])(?:[a-z]:[\\/]|\\\\|\/(?!\/))[^\s<>"']*/gi;
const sensitiveKey = (key: string) => SENSITIVE.test(key) || SECRET_FIELD_SUFFIX.test(key);

function object(value: unknown): Json | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Json : undefined;
}

function rows(value: unknown, field: string): Json[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.some((item) => !object(item))) throw new Error("invalid business system " + field);
  return value as Json[];
}

function uri(value: string): string {
  if (/^(?:file|vault|secret):\/\//i.test(value)) return value.startsWith("file:") ? LOCAL_PATH : REDACTED;
  if (!/^[a-z][a-z\d+.-]*:\/\//i.test(value)) return value;
  try {
    const parsed = new URL(value);
    let changed = false;
    if (parsed.password || (/^https?:$/.test(parsed.protocol) && parsed.username)) {
      parsed.username = "";
      parsed.password = "";
      changed = true;
    }
    const keys: string[] = [];
    parsed.searchParams.forEach((_value, key) => { if (sensitiveKey(key)) keys.push(key); });
    for (const key of keys) { parsed.searchParams.delete(key); changed = true; }
    return changed ? parsed.toString() : value;
  } catch { return REDACTED; }
}

function text(value: unknown, secrets: readonly string[]): string | undefined {
  if (typeof value !== "string") return undefined;
  const scrubbed = value.replace(PRIVATE_TEXT, "$1$2" + REDACTED).replace(ADDRESS, (token) =>
    /^(?:[a-z][a-z\d+.-]*):\/\//i.test(token) ? uri(token) : LOCAL_PATH);
  return secrets.reduce((current, secret) => current.replaceAll(secret, REDACTED), scrubbed);
}

function portable(value: unknown, clean: Rewrite, key = ""): unknown {
  if (sensitiveKey(key)) return undefined;
  if (typeof value === "string") return clean(value);
  if (Array.isArray(value)) return value.map((item) => portable(item, clean));
  if (object(value)) {
    return Object.fromEntries(Object.entries(value as Json).flatMap(([name, item]) => {
      const safe = portable(item, clean, name);
      return safe === undefined ? [] : [[name, safe]];
    }));
  }
  return value;
}

function metadata(value: unknown, clean: Rewrite): Json {
  return object(portable(value, clean)) ?? {};
}

function ref(value: unknown, clean: Rewrite): string | undefined {
  const safe = clean(value);
  if (!safe || safe.includes(REDACTED) || safe.includes(LOCAL_PATH) || /^(?:[a-z]|data|blob|javascript):/i.test(safe)
    || /^~(?:[\\/]|$)/.test(safe) || /(?:^|\/)\.\.(?:\/|$)|\\|\x00/.test(safe)) return undefined;
  return safe;
}

function refs(value: unknown, clean: Rewrite): string[] {
  return Array.isArray(value) ? value.flatMap((item) => ref(item, clean) ?? []) : [];
}

function files(value: unknown, clean: Rewrite): Artifact["files"] {
  if (!Array.isArray(value)) return undefined;
  return value.flatMap((item) => {
    const file = object(item);
    const reference = ref(file?.ref, clean);
    if (!reference) return [];
    const path = ref(file?.path, clean);
    return [{ ref: reference, path, mediaType: clean(file?.mediaType), digest: ref(file?.digest, clean) }];
  });
}

function producer(value: unknown, clean: Rewrite): Artifact["producer"] {
  const data = object(value);
  if (!data) return undefined;
  return { operator: ref(data.operator, clean), machine: ref(data.machine, clean), harness: clean(data.harness) };
}

function recordsFields(record: Json, clean: Rewrite, useRef: string) {
  const { run_id: oldRun, unit_run_id: oldUnitRun, ...rest } = metadata(record.metadata, clean);
  return {
    recordType: record.recordType === "material" || record.record_type === "material" ? "material" as const : "artifact" as const,
    kind: clean(record.kind) ?? "artifact",
    ref: ref(record.ref, clean) ?? useRef,
    title: clean(record.title),
    description: clean(record.description),
    summary: clean(record.summary),
    purpose: clean(record.purpose),
    status: clean(record.status),
    producer: producer(record.producer, clean),
    files: files(record.files, clean),
    inputs: Array.isArray(record.inputs) ? refs(record.inputs, clean) : undefined,
    outputs: Array.isArray(record.outputs) ? refs(record.outputs, clean) : undefined,
    lineage: Array.isArray(record.lineage) ? refs(record.lineage, clean) : undefined,
    metadata: {
      ...rest,
      ...(typeof oldRun === "string" || typeof oldUnitRun === "string"
        ? { origin: { ...object(rest.origin), run_id: oldRun, unit_run_id: oldUnitRun } } : {}),
    },
  };
}

export function exportBusinessSystem(
  store: SqliteStore, id: string, fallbackGraph: () => unknown, readSecret?: (ref: string) => string | undefined,
) {
  const system = store.getBusinessSystem(id);
  if (!system) return undefined;
  const ownedProjects = store.listProjects(id);
  const units = store.listBusinessUnits(id);
  const records = store.listBusinessSystemArtifacts(id);
  const secrets = readSecret ? ownedProjects.flatMap((project) => project.secretRequests.flatMap((request) => {
    if (request.status !== "available") return [];
    const value = readSecret(request.vaultRef);
    return value ? [value] : [];
  })) : [];
  const knownIds = new Set([
    id, ...units.map((unit) => unit.id), ...records.map((record) => record.id),
    ...ownedProjects.flatMap((project) => [project.id, ...project.sources.map((source) => source.id)]),
  ]);
  const clean: Rewrite = (value) => typeof value === "string" && knownIds.has(value) ? value : text(value, secrets);
  const { imported_runs: importedRuns, graph: _graph, ...systemMetadata } = system.metadata;
  const projects = ownedProjects.map((project) => ({
    id: project.id,
    name: clean(project.name),
    metadata: metadata(project.metadata, clean),
    sources: project.sources.map((source) => ({
      id: source.id, kind: source.kind,
      ref: source.kind === "local_folder" ? "intake://" + project.id : ref(source.repoUrl ?? source.fileRef, clean),
      metadata: metadata(source.metadata, clean),
    })),
    secretRequests: project.secretRequests.map((request) => ({
      id: request.id, name: request.name, status: "requested",
      metadata: metadata(request.metadata, clean),
    })),
  }));
  const liveHistory = store.listBusinessRunsWithUnits(id).map((run) => ({
    ...run,
    units: run.units.map(({ sessionId: _sessionId, error: _error, ...unit }) => unit),
  }));
  return {
    schema: SCHEMA,
    exportedAt: store.timestamp(),
    system: { ...system, name: clean(system.name), description: clean(system.description), metadata: metadata(systemMetadata, clean) },
    graph: portable(system.metadata.graph ?? fallbackGraph(), clean),
    units: units.map((unit) => ({
      ...unit,
      name: clean(unit.name), model: clean(unit.model), harnessRuntime: clean(unit.harnessRuntime),
      machineRef: unit.machineRef === "local_folder" ? undefined : ref(unit.machineRef, clean),
      metadata: metadata(unit.metadata, clean),
    })),
    projects,
    records: records.map((record) => ({
      id: record.id, projectId: record.projectId, sourceId: record.sourceId, createdAt: record.createdAt,
      ...recordsFields(record as unknown as Json, clean, "artifact://" + record.id),
    })),
    conversions: store.listBusinessConversions(id).map((conversion) => portable(conversion, clean)),
    runs: portable([...(Array.isArray(importedRuns) ? importedRuns : []), ...liveHistory], clean),
  };
}

export function importBusinessSystem(store: SqliteStore, document: Json, validateGraph: GraphValidator): BusinessSystem {
  const sourceSystem = object(document.system);
  if (document.schema !== SCHEMA || typeof sourceSystem?.name !== "string" || !sourceSystem.name.trim()) {
    throw new Error("business system document schema or name is invalid");
  }
  const sourceName = sourceSystem.name;
  const sourceGraph = document.graph === undefined ? object(sourceSystem.metadata)?.graph : document.graph;
  if (sourceGraph !== undefined) {
    const error = validateGraph(sourceGraph);
    if (error) throw new Error("business system graph is invalid: " + error);
  }
  const projects = rows(document.projects, "projects");
  const units = rows(document.units, "units");
  const records = rows(document.records, "records");
  const conversions = rows(document.conversions, "conversions");
  const ids = new Map<string, string>();
  const allocate = (value: unknown, id = store.uuid()) => {
    if (typeof value === "string") {
      if (ids.has(value)) throw new Error("business system document contains duplicate IDs");
      ids.set(value, id);
    }
    return id;
  };
  const projectIds = new Map<string, string>();
  const sourceIds = new Map<string, { projectId: string; sourceId: string }>();
  const legacySourceIds = new Map<string, string>();
  const projectRows = projects.map((project) => {
    const id = allocate(project.id);
    if (typeof project.id === "string") projectIds.set(project.id, id);
    const sources = rows(project.sources, "sources").map((source) => {
      const sourceId = allocate(source.id);
      if (typeof source.id === "string") sourceIds.set(source.id, { projectId: id, sourceId });
      return { source, sourceId };
    });
    if (sources.length === 1 && sources[0].source.id === undefined) legacySourceIds.set(id, sources[0].sourceId);
    return { project, id, sources };
  });
  const applyUnits = units.length > 0;
  const unitRows = units.map((unit) => {
    const projectId = typeof unit.projectId === "string" ? projectIds.get(unit.projectId) : undefined;
    const kind = unit.kind === "operator" || unit.kind === "machine" ? unit.kind : undefined;
    const standardId = projectId && kind && unit.id === String(unit.projectId) + ":" + kind ? projectId + ":" + kind : undefined;
    return { unit, id: allocate(unit.id, standardId) };
  });
  for (const [oldProject, newProject] of projectIds) {
    for (const kind of ["operator", "machine"] as const) {
      const oldUnit = oldProject + ":" + kind;
      if (!ids.has(oldUnit) && !applyUnits) ids.set(oldUnit, newProject + ":" + kind);
    }
  }
  const recordRows = records.map((record) => ({ record, id: allocate(record.id) }));
  const clean: Rewrite = (value) => {
    if (typeof value !== "string") return undefined;
    const direct = ids.get(value);
    if (direct) return direct;
    const link = /^((?:intake|project|unit|source|record|artifact|material):\/\/)([^/]+)(.*)$/.exec(value);
    if (link && ids.has(link[2])) return text(link[1] + ids.get(link[2]) + link[3], []);
    return text(value, []);
  };
  return store.db.transaction(() => {
    const system = store.createBusinessSystem({
      name: clean(sourceName)!, description: clean(sourceSystem.description),
      status: sourceSystem.status === "archived" ? "archived" : "active",
    });
    if (typeof sourceSystem.id === "string") allocate(sourceSystem.id, system.id);
    for (const { project, id, sources } of projectRows) {
      if (typeof project.name !== "string" || !project.name.trim()) throw new Error("invalid project name");
      const portableSources = sources.map(({ source, sourceId }) => {
        if (source.kind !== "repo_url" && source.kind !== "uploaded_file" && source.kind !== "local_folder") {
          throw new Error("invalid project source kind");
        }
        const reference = ref(source.ref, clean);
        return {
          kind: source.kind as ProjectSourceKind,
          ref: source.kind === "local_folder" ? undefined : reference ?? "intake://" + sourceId,
          metadata: metadata(source.metadata, clean),
        };
      });
      const imported = store.createIntake({
        business_system_id: system.id, name: clean(project.name), metadata: metadata(project.metadata, clean),
        portable_sources: portableSources, skip_default_units: applyUnits,
      }, { projectId: id, sourceIds: sources.map(({ sourceId }) => sourceId) });
      for (const request of rows(project.secretRequests, "secret requests")) {
        if (!projectCredentialName(request.name)) throw new Error("invalid project credential name");
        store.createSecretRequest({
          projectId: imported.id, name: request.name, vaultRef: "vault://secret/" + store.uuid(),
          status: "requested", metadata: metadata(request.metadata, clean),
        });
      }
    }
    for (const { unit, id } of unitRows) {
      const projectId = typeof unit.projectId === "string" ? projectIds.get(unit.projectId) : undefined;
      if ((unit.kind !== "operator" && unit.kind !== "machine") || typeof unit.name !== "string"
        || (unit.projectId !== undefined && !projectId)) throw new Error("invalid business unit");
      store.createBusinessUnit({
        id, businessSystemId: system.id, projectId, kind: unit.kind, name: clean(unit.name)!,
        harnessRuntime: clean(unit.harnessRuntime), model: clean(unit.model), machineRef: ref(unit.machineRef, clean),
        status: unit.status === "archived" ? "archived" : unit.kind === "machine" ? "ready" : "pending",
        metadata: metadata(unit.metadata, clean),
      });
    }
    for (const { record, id } of recordRows) {
      const projectId = projectIds.get(String(record.projectId ?? record.project_id));
      if (!projectId || typeof record.kind !== "string") throw new Error("invalid business record project or kind");
      const oldSourceId = record.sourceId ?? record.source_id;
      const source = typeof oldSourceId === "string" ? sourceIds.get(oldSourceId) : undefined;
      if (source && source.projectId !== projectId) throw new Error("record source belongs to another project");
      store.addArtifact({
        projectId, sourceId: source?.sourceId ?? (typeof oldSourceId === "string" ? legacySourceIds.get(projectId) : undefined),
        ...recordsFields(record, clean, "artifact://" + id),
      }, id);
    }
    for (const conversion of conversions) {
      if (typeof conversion.kind !== "string" || typeof conversion.summary !== "string") throw new Error("invalid business conversion");
      const status = conversion.status === "completed" || conversion.status === "failed" ? conversion.status : "planned";
      store.createBusinessConversion({
        businessSystemId: system.id, kind: clean(conversion.kind)!, summary: clean(conversion.summary)!,
        status: status satisfies BusinessConversion["status"], inputs: refs(conversion.inputs, clean), outputs: refs(conversion.outputs, clean),
        producer: producer(conversion.producer, clean), metadata: metadata(conversion.metadata, clean),
      });
    }
    const { graph: _oldGraph, graph_updated_by: _oldBy, imported_runs: _oldRuns, ...rest } = metadata(sourceSystem.metadata, clean);
    const graph = portable(sourceGraph, clean);
    if (graph !== undefined) {
      const error = validateGraph(graph);
      if (error) throw new Error("business system graph is invalid after import: " + error);
    }
    const history = portable(rows(document.runs, "runs"), clean);
    return store.updateBusinessSystemMetadata(system.id, {
      ...rest,
      ...(graph === undefined ? {} : { graph, graph_updated_by: "import" }),
      ...(Array.isArray(history) && history.length ? { imported_runs: history } : {}),
    }) ?? system;
  }).immediate();
}
