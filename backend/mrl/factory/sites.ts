import { checkExpression, MrlStaticError } from "../checker";
import type { MrlValue } from "../evaluator";
import type { RuleModel } from "../rules";
import { compileTemplate } from "../template";
import { BOOL, DYN, FLOAT, INT, NULL, NUM, STRING, list, lub, map, parseType, record, type MrlType, type TypeEnvironment } from "../types";
import { documentKey, documentKind, documentPosition, type HostDiagnostic, type SpecDocument } from "./registry";

type Facts = Record<string, MrlValue[][]>;
type SiteDiagnostic = HostDiagnostic & { severity: "error" | "warning" };
const payload = list(record({ path: STRING, digest: STRING, size: INT, media_type: STRING }));
const anyBean = record({ id: STRING, type: STRING, facts: map(DYN), meta: map(DYN), payload });
const run = record({ id: STRING, epoch: STRING, line: STRING, budget: record({ cap_usd: FLOAT, spent_usd: FLOAT, remaining_usd: FLOAT }) });
const request = record({ kind: STRING, summary: STRING, cost_usd: NUM });
const proposal = record({ summary: STRING, cost_estimate_usd: NUM, time_estimate_s: NUM });
const runtimeEnv = {
  brief: record({ task: STRING, factory_contract: STRING }),
  limits: record({ max_turns: INT, timeout_s: INT, budget_usd: FLOAT }),
  operator: record({ model: STRING, runtime: STRING }),
  session: record({ id: STRING }),
};

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
function literalType(value: unknown): MrlType {
  if (typeof value === "string") return STRING;
  if (typeof value === "boolean") return BOOL;
  if (typeof value === "number") return Number.isInteger(value) ? INT : FLOAT;
  return value === null ? NULL : DYN;
}
function typeList(values: unknown[]): MrlType {
  return values.length ? values.map(literalType).reduce(lub) : DYN;
}
function declared(value: unknown): MrlType {
  if (typeof value !== "string") return DYN;
  try { return parseType(value); } catch { return DYN; }
}
function schema(value: unknown): MrlType {
  const spec = object(value);
  if (Object.hasOwn(spec, "const")) return literalType(spec.const);
  if (Array.isArray(spec.enum) && spec.enum.length) return typeList(spec.enum);
  switch (spec.type ?? (spec.properties ? "object" : undefined)) {
    case "string": return STRING;
    case "integer": return INT;
    case "number": return NUM;
    case "boolean": return BOOL;
    case "null": return NULL;
    case "array": return list(schema(spec.items));
    case "object": {
      const properties = Object.entries(object(spec.properties));
      if (!spec.properties) return map(schema(spec.additionalProperties));
      const fields = Object.fromEntries(properties.map(([key, item]) => [key, schema(item)]));
      const required = array(spec.required).filter((item): item is string => typeof item === "string");
      for (const key of required) if (!Object.hasOwn(fields, key)) Object.defineProperty(fields, key, { value: DYN, enumerable: true });
      return record(fields, required, spec.additionalProperties !== false);
    }
    default: return DYN;
  }
}
function names(rows: MrlValue[][], key: string, column: number): MrlValue[][] {
  return rows.filter((row) => row[column] === key);
}

class SiteTypes {
  private readonly keys: Map<string, string>;
  private readonly parents: Map<string, string>;
  private readonly ownParents: Map<string, string>;
  private readonly documents: Map<string, SpecDocument>;
  private readonly inheritedFacts: MrlValue[][];
  private readonly resolved: MrlValue[][];
  private readonly outputs: MrlValue[][];
  private readonly beans = new Map<string, MrlType>();

  constructor(documents: SpecDocument[], private readonly facts: Facts, model: RuleModel) {
    this.keys = new Map(model.rowsOf("type_key").map(([type, key]) => [String(type), String(key)]));
    this.parents = new Map(model.rowsOf("extends").map(([type, parent]) => [String(type), String(parent)]));
    this.ownParents = new Map((facts.type_extends_decl ?? []).map(([key, parent]) => [String(key), String(parent)]));
    this.documents = new Map(documents.flatMap((doc) => {
      const key = documentKey(doc);
      return key ? [[key, doc] as const] : [];
    }));
    this.inheritedFacts = model.rowsOf("type_fact");
    this.resolved = model.rowsOf("resolved");
    this.outputs = model.rowsOf("step_out_port");
  }

  private meta(key: string, currentType?: string): MrlType {
    const seen = new Set<string>();
    let documentKey = key;
    let parent = currentType ? this.parents.get(currentType) : this.ownParents.get(key);
    while (documentKey && !seen.has(documentKey)) {
      seen.add(documentKey);
      const spec = object(object(this.documents.get(documentKey)?.data).spec);
      if (Object.hasOwn(spec, "meta_schema")) return schema(spec.meta_schema);
      documentKey = this.keys.get(parent ?? "") ?? "";
      parent = this.parents.get(parent ?? "");
    }
    return record({}, [], true);
  }

  private beanShape(rows: MrlValue[][], meta: MrlType): MrlType {
    const facts = Object.fromEntries(rows.map(([, fact, type]) => [String(fact), declared(type)]));
    return record({ id: STRING, type: STRING, facts: record(facts), meta, payload });
  }

  specificBean(key: string): MrlType {
    return this.beanShape([
      ...names(this.inheritedFacts, this.ownParents.get(key) ?? "", 0),
      ...names(this.facts.type_fact_decl ?? [], key, 0),
    ], this.meta(key));
  }

  bean(expression: unknown): MrlType {
    if (typeof expression !== "string") return anyBean;
    const previous = this.beans.get(expression);
    if (previous) return previous;
    let type = anyBean;
    if (expression.startsWith("list<")) type = list(this.bean(expression.slice(5, -1)));
    else if (expression.startsWith("ref<")) {
      const inner = this.bean(expression.slice(4, -1));
      type = inner.kind === "record" ? { ...inner, fields: { ...inner.fields, uri: { type: STRING, required: true } } } : inner;
    } else {
      const key = this.keys.get(expression);
      if (key) type = this.beanShape(names(this.inheritedFacts, expression, 0), this.meta(key, expression));
    }
    this.beans.set(expression, type);
    return type;
  }

  ports(rows: MrlValue[][]): MrlType {
    const fields: Record<string, MrlType> = Object.create(null);
    const required = [];
    for (const [name, expression, optional] of rows) {
      fields[String(name)] = this.bean(expression);
      if (!optional) required.push(String(name));
    }
    return record(fields, required);
  }

  portsOf(key: string, direction: "in_port" | "out_port"): MrlType {
    return this.ports(names(this.facts[direction] ?? [], key, 0).map(([, name, type, optional]) => [name, type, optional]));
  }

  source(line: string, source: unknown): MrlValue | undefined {
    if (typeof source !== "string") return undefined;
    if (source.startsWith("$inputs.")) return names(this.facts.line_input ?? [], line, 0).find((row) => row[1] === source.slice(8))?.[2];
    const [step, port] = source.split(".", 2);
    return names(this.outputs, line, 0).find((row) => row[1] === step && row[2] === port)?.[3];
  }

  resolveStation(reference: unknown): string {
    return String(this.resolved.find(([kind, ref]) => kind === "Station" && ref === reference)?.[2] ?? "");
  }

  hasJudge(station: string): boolean {
    return Boolean(object(object(object(this.documents.get(station)?.data).spec).gate).judge);
  }

  lineInputs(line: string): MrlType {
    return this.ports(names(this.facts.line_input ?? [], line, 0).map(([, name, type]) => [name, type, false]));
  }
}

export function checkExpressionSites(documents: SpecDocument[], facts: Facts, model: RuleModel): SiteDiagnostic[] {
  const types = new SiteTypes(documents, facts, model);
  const diagnostics: SiteDiagnostic[] = [];
  const seen = new Set<string>();
  for (const doc of documents) {
    const key = documentKey(doc);
    if (!key) continue;
    const spec = object(object(doc.data).spec);
    const emit = (code: string, path: string, message: string, subject = path) => {
      const identity = JSON.stringify([code, key, subject, message]);
      if (seen.has(identity)) return;
      seen.add(identity);
      const [line, col] = documentPosition(doc, "spec." + path);
      diagnostics.push({ code, subject: key + "/" + subject, message, file: doc.file, doc: doc.index, path: "spec." + path, line, col, severity: code.startsWith("W-") ? "warning" : "error" });
    };
    const check = (value: unknown, path: string, environment: TypeEnvironment, template = false, metaWarning?: "property" | "admit", subject = path) => {
      if (typeof value !== "string") return;
      try {
        const compiled = template ? compileTemplate(value, { environment }) : checkExpression(value, { environment, expect: BOOL });
        for (const warning of compiled.warnings) emit("W-EXPR", path, warning.code + "：" + warning.message, subject);
        if (!("paths" in compiled)) return;
        if (metaWarning === "property" && compiled.paths.some((value) => /^bean\.meta(?:\.|$)/.test(value))) {
          emit("W-PROPERTY-META", path, "命名属性引用了生产者自报的 bean.meta，应当基于实测 facts");
        }
        if (metaWarning === "admit" && compiled.paths.some((value) => /^inputs\.[A-Za-z_][A-Za-z0-9_]*\.meta(?:\.|$)/.test(value))) {
          emit("W-ADMIT-META", path, "admit 引用了输入 Bean 自报的 meta，准入应当基于实测 facts");
        }
      } catch (error) {
        if (error instanceof MrlStaticError) {
          for (const item of error.diagnostics) {
            if (item.severity !== "error") continue;
            const message = !template && item.code === "MRL-TYPE" && item.message.endsWith(", expected bool")
              ? "这里需要 bool，" + item.message : item.message;
            emit("E-EXPR", path, item.code + "：" + message, subject);
          }
          return;
        }
        if (error instanceof Error && "code" in error) {
          emit("E-EXPR", path, String(error.code) + "：" + error.message, subject);
          return;
        }
        throw error;
      }
    };
    const templates = (value: unknown, path: string, environment: TypeEnvironment): void => {
      if (typeof value === "string") {
        if (value.includes("{{")) check(value, path, environment, true);
      } else if (Array.isArray(value)) {
        value.forEach((item, index) => templates(item, path + "[" + index + "]", environment));
      } else {
        for (const [name, item] of Object.entries(object(value))) templates(item, path + "." + name, environment);
      }
    };
    switch (documentKind(doc)) {
      case "Type": {
        const bean = types.specificBean(key);
        for (const [name, value] of Object.entries(object(spec.properties))) check(value, "properties." + name, { bean }, false, "property");
        break;
      }
      case "Station": {
        const inputs = types.portsOf(key, "in_port");
        const outputs = types.portsOf(key, "out_port");
        const params = record(Object.fromEntries(names(facts.station_param ?? [], key, 0).map(([, name, type]) => [String(name), declared(type)])));
        const base = { inputs, params };
        array(spec.admit).forEach((item, index) => check(item, "admit[" + index + "]", base, false, "admit"));
        for (const [name, definition] of Object.entries(object(spec.outputs))) {
          const out = outputs.kind === "record" ? outputs.fields[name]?.type ?? anyBean : anyBean;
          array(object(definition).accept).forEach((item, index) => check(item, "outputs." + name + ".accept[" + index + "]", { ...base, out }));
        }
        const conversion = names(facts.converts ?? [], key, 0)[0];
        if (conversion) array(spec.equivalence).forEach((item, index) =>
          check(item, "equivalence[" + index + "]", { params, src: types.bean(conversion[1]), dst: types.bean(conversion[2]) }));
        for (const [phase, type] of [["propose", proposal], ["in_run", request]] as const) {
          check(object(object(spec.approval)[phase]).auto_if, "approval." + phase, { ...base, [phase === "propose" ? "proposal" : "request"]: type });
        }
        for (const [name, monitor] of Object.entries(object(spec.monitors))) check(object(monitor).when, "monitors." + name, { ...base, request });
        check(spec.idempotency_key, "idempotency_key", { ...base, run }, true);
        break;
      }
      case "Machine":
        for (const [recipe, value] of Object.entries(object(spec.recipes))) {
          const definition = object(value);
          const inputs = types.ports(names(facts.recipe_in ?? [], key, 0).filter((row) => row[1] === recipe)
            .map(([, , name, type, optional]) => [name, type, optional]));
          const params = record(Object.fromEntries(names(facts.recipe_param ?? [], key, 0).filter((row) => row[1] === recipe)
            .map(([, , name, type]) => [String(name), declared(type)])));
          const environment = { inputs, params };
          const path = "recipes." + recipe + ".";
          for (const field of ["task_template", "call", "run", "receipt"]) templates(definition[field], path + field, environment);
          for (const [port, output] of Object.entries(object(definition.outputs))) templates(object(output).harvest, path + "outputs." + port + ".harvest", environment);
        }
        break;
      case "OperatorRuntime": {
        const invocation = object(spec.invoke);
        templates(invocation.argv, "invoke.argv", runtimeEnv);
        templates(invocation.resume, "invoke.resume", runtimeEnv);
        templates(spec.env, "env", runtimeEnv);
        break;
      }
      case "Line":
        for (const [name, configuration] of Object.entries(object(spec.steps))) {
          const step = object(configuration);
          const prefix = "steps." + name;
          const context: TypeEnvironment = { inputs: types.lineInputs(key), run };
          const { over, as } = object(step.map);
          const itemType = Array.isArray(over) ? typeList(over) : types.bean(types.source(key, over));
          const collision = typeof as === "string" && Object.hasOwn(context, as);
          if (typeof as === "string" && !collision) {
            const type = Array.isArray(over) ? itemType : itemType.kind === "list" ? itemType.element : DYN;
            Object.defineProperty(context, as, { value: type, enumerable: true });
          }
          for (const [name, value] of Object.entries(object(step.params))) {
            const path = prefix + ".params." + name;
            if (collision && typeof value === "string" && value.includes("{{")) emit("E-EXPR", path, "map 的 as 名字与模板环境中的名字冲突");
            else templates(value, path, context);
          }
          const choice = object(step.switch);
          const bean = types.bean(types.source(key, choice.on));
          array(choice.cases).forEach((value, index) => {
            const path = prefix + ".switch.cases[" + index + "]";
            check(object(value).when, path + ".when", { bean }, false, undefined, path);
          });
          if (step.until) {
            const station = types.resolveStation(step.station);
            const gate = record({ attempt: INT, judge_score: FLOAT }, types.hasJudge(station) ? ["attempt", "judge_score"] : ["attempt"]);
            check(object(step.until).cond, prefix + ".until", { gate, out: station ? types.portsOf(station, "out_port") : map(anyBean) });
          }
        }
        break;
    }
  }
  return diagnostics;
}
