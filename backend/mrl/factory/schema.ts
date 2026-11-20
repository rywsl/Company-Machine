import { readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { schemasDir } from "./paths";

type JsonObject = Record<string, unknown>;
export interface SchemaDiagnostic { path: string; message: string; }

const kindSchema: Record<string, string> = {
  Type: "type.schema.json",
  Machine: "machine.schema.json",
  OperatorRuntime: "operator-runtime.schema.json",
  Station: "station.schema.json",
  Line: "line.schema.json",
  Policy: "policy.schema.json",
  GoldenSet: "records.schema.json#/$defs/goldenSet",
  Baseline: "records.schema.json#/$defs/baseline",
  Agent: "agent.schema.json",
  Plan: "records.schema.json#/$defs/plan",
  ChangeSet: "records.schema.json#/$defs/changeset",
  ConversionRequest: "records.schema.json#/$defs/conversionRequest",
};

const cache = new Map<string, { stamp: string; schema: JsonObject }>();
function isObject(value: unknown): value is JsonObject { return Boolean(value) && typeof value === "object" && !Array.isArray(value); }
function loadSchema(name: string): JsonObject {
  const filePath = join(schemasDir(), name);
  const { mtimeMs, ctimeMs, size } = statSync(filePath);
  const stamp = `${mtimeMs}:${ctimeMs}:${size}`;
  const cached = cache.get(filePath);
  if (cached?.stamp === stamp) return cached.schema;
  const document = JSON.parse(readFileSync(filePath, "utf8")) as JsonObject;
  cache.set(filePath, { stamp, schema: document });
  return document;
}
function pointer(root: unknown, fragment: string): unknown {
  if (!fragment || fragment === "#") return root;
  const parts = fragment.replace(/^#\/?/, "").split("/").filter(Boolean).map((part) => part.replaceAll("~1", "/").replaceAll("~0", "~"));
  let value: unknown = root;
  for (const part of parts) value = isObject(value) ? value[part] : undefined;
  return value;
}
function resolveRef(ref: string, currentRoot: JsonObject): [JsonObject, JsonObject] {
  const [file, fragment] = ref.split("#", 2);
  const root = file ? loadSchema(file) : currentRoot;
  const resolved = pointer(root, fragment ?? "");
  if (!isObject(resolved)) throw new Error(`Schema reference could not be resolved: ${ref}`);
  return [resolved, root];
}
function equal(a: unknown, b: unknown): boolean { return JSON.stringify(a) === JSON.stringify(b); }
function typeMatches(value: unknown, type: string): boolean {
  if (type === "null") return value === null;
  if (type === "array") return Array.isArray(value);
  if (type === "object") return isObject(value);
  if (type === "integer") return typeof value === "number" && Number.isInteger(value);
  if (type === "number") return typeof value === "number" && Number.isFinite(value);
  if (type === "boolean") return typeof value === "boolean";
  if (type === "string") return typeof value === "string";
  return true;
}
function pathFor(path: string, key: string | number): string { return typeof key === "number" ? `${path}[${key}]` : `${path}.${key}`.replace(/^\./, ""); }

function validateNode(value: unknown, schema: JsonObject, path: string, errors: SchemaDiagnostic[], rootSchema: JsonObject = schema): void {
  if (typeof schema.$ref === "string") {
    const [resolved, resolvedRoot] = resolveRef(schema.$ref, rootSchema);
    validateNode(value, resolved, path, errors, resolvedRoot);
  }
  if (schema.const !== undefined && !equal(value, schema.const)) errors.push({ path, message: `must equal ${JSON.stringify(schema.const)}` });
  if (Array.isArray(schema.enum) && !schema.enum.some((entry) => equal(value, entry))) errors.push({ path, message: "must be one of the allowed values" });
  const types = Array.isArray(schema.type) ? schema.type.filter((entry): entry is string => typeof entry === "string") : typeof schema.type === "string" ? [schema.type] : [];
  if (types.length && !types.some((type) => typeMatches(value, type))) {
    errors.push({ path, message: `must be ${types.join(" or ")}` });
    return;
  }
  if (typeof value === "string") {
    if (typeof schema.minLength === "number" && value.length < schema.minLength) errors.push({ path, message: `must contain at least ${schema.minLength} characters` });
    if (typeof schema.maxLength === "number" && value.length > schema.maxLength) errors.push({ path, message: `must contain at most ${schema.maxLength} characters` });
    if (typeof schema.pattern === "string" && !new RegExp(schema.pattern).test(value)) errors.push({ path, message: `must match ${schema.pattern}` });
  }
  if (typeof value === "number") {
    if (typeof schema.minimum === "number" && value < schema.minimum) errors.push({ path, message: `must be >= ${schema.minimum}` });
    if (typeof schema.maximum === "number" && value > schema.maximum) errors.push({ path, message: `must be <= ${schema.maximum}` });
    if (typeof schema.exclusiveMinimum === "number" && value <= schema.exclusiveMinimum) errors.push({ path, message: `must be > ${schema.exclusiveMinimum}` });
  }
  if (Array.isArray(value)) {
    if (typeof schema.minItems === "number" && value.length < schema.minItems) errors.push({ path, message: `must contain at least ${schema.minItems} items` });
    if (schema.uniqueItems === true && new Set(value.map((entry) => JSON.stringify(entry))).size !== value.length) errors.push({ path, message: "must contain unique items" });
    if (isObject(schema.items)) value.forEach((entry, index) => validateNode(entry, schema.items as JsonObject, pathFor(path, index), errors, rootSchema));
  }
  if (isObject(value)) {
    if (typeof schema.minProperties === "number" && Object.keys(value).length < schema.minProperties) errors.push({ path, message: `must contain at least ${schema.minProperties} properties` });
    const required = Array.isArray(schema.required) ? schema.required.filter((entry): entry is string => typeof entry === "string") : [];
    for (const key of required) if (!Object.hasOwn(value, key)) errors.push({ path: pathFor(path, key), message: "is required" });
    const properties = isObject(schema.properties) ? schema.properties : {};
    for (const [key, child] of Object.entries(properties)) if (Object.hasOwn(value, key) && isObject(child)) validateNode(value[key], child, pathFor(path, key), errors, rootSchema);
    for (const [key, child] of Object.entries(value)) {
      if (Object.hasOwn(properties, key)) continue;
      if (schema.additionalProperties === false) errors.push({ path: pathFor(path, key), message: "is not allowed" });
      else if (isObject(schema.additionalProperties)) validateNode(child, schema.additionalProperties, pathFor(path, key), errors, rootSchema);
    }
    if (isObject(schema.propertyNames)) for (const key of Object.keys(value)) validateNode(key, schema.propertyNames as JsonObject, pathFor(path, key), errors, rootSchema);
  }
  if (Array.isArray(schema.allOf)) for (const child of schema.allOf) if (isObject(child)) validateNode(value, child, path, errors, rootSchema);
  for (const keyword of ["oneOf", "anyOf"] as const) if (Array.isArray(schema[keyword])) {
    const branches = schema[keyword].filter(isObject) as JsonObject[];
    const passed = branches.filter((branch) => { const branchErrors: SchemaDiagnostic[] = []; validateNode(value, branch, path, branchErrors, rootSchema); return branchErrors.length === 0; }).length;
    if ((keyword === "oneOf" && passed !== 1) || (keyword === "anyOf" && passed === 0)) errors.push({ path, message: `${keyword} constraint failed` });
  }
  if (isObject(schema.not)) {
    const forbidden: SchemaDiagnostic[] = [];
    validateNode(value, schema.not, path, forbidden, rootSchema);
    if (forbidden.length === 0) errors.push({ path, message: "not constraint failed" });
  }
  if (isObject(schema.if)) {
    const conditionErrors: SchemaDiagnostic[] = [];
    validateNode(value, schema.if as JsonObject, path, conditionErrors, rootSchema);
    const branch = conditionErrors.length === 0 ? schema.then : schema.else;
    if (isObject(branch)) validateNode(value, branch, path, errors, rootSchema);
  }
}

export const registryKinds = ["Type", "Machine", "OperatorRuntime", "Station", "Line", "Policy", "GoldenSet", "Baseline", "Agent"] as const;
export function validate(data: unknown): SchemaDiagnostic[] {
  if (!isObject(data)) return [{ path: "", message: "document must be an object" }];
  const kind = data.kind;
  if (typeof kind !== "string" || !Object.hasOwn(kindSchema, kind)) return [{ path: "kind", message: `unknown kind ${String(kind)}` }];
  const [file, fragment = ""] = kindSchema[kind].split("#", 2);
  const root = loadSchema(file);
  const schema = pointer(root, fragment ? `#${fragment}` : "#");
  if (!isObject(schema)) return [{ path: "", message: `schema not found for ${kind}` }];
  const errors: SchemaDiagnostic[] = [];
  validateNode(data, schema, "", errors, root);
  return errors.slice(0, 20);
}

export function schemaForKind(kind: string): string | undefined { return Object.hasOwn(kindSchema, kind) ? kindSchema[kind] : undefined; }
