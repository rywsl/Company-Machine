import schemaRules from "../../../spec/mrl/kernel/schema.mrl" with { type: "text" };
import referenceRules from "../../../spec/mrl/kernel/refs.mrl" with { type: "text" };
import codeRules from "../../../spec/mrl/kernel/codes.mrl" with { type: "text" };
import typeRules from "../../../spec/mrl/kernel/types.mrl" with { type: "text" };
import stationRules from "../../../spec/mrl/kernel/stations.mrl" with { type: "text" };
import lineRules from "../../../spec/mrl/kernel/lines.mrl" with { type: "text" };
import hostRules from "../../../spec/mrl/kernel/host.mrl" with { type: "text" };
import type { MrlValue } from "../evaluator";
import { MrlRuleError, buildRuleProgram, type RuleLocation } from "../rules";
import { documentKey, documentKind, documentPosition, type HostDiagnostic, type Registry, type SpecDocument } from "./registry";
import { resolutionFunctions } from "./resolve";
import { checkExpressionSites } from "./sites";

type ReferenceFacts = Record<string, MrlValue[][]>;
type Location = { document: SpecDocument; path: string };
type Diagnostic = HostDiagnostic & { severity: "error" | "warning" | "info" };
export type FactoryResolution = Record<string, MrlValue> & { line: string; step: string; port: string };

const modules = [
  { name: "spec/mrl/kernel/schema.mrl", source: schemaRules },
  { name: "spec/mrl/kernel/refs.mrl", source: referenceRules },
  { name: "spec/mrl/kernel/codes.mrl", source: codeRules },
  { name: "spec/mrl/kernel/types.mrl", source: typeRules },
  { name: "spec/mrl/kernel/stations.mrl", source: stationRules },
  { name: "spec/mrl/kernel/lines.mrl", source: lineRules },
  { name: "spec/mrl/kernel/host.mrl", source: hostRules },
];
let program: ReturnType<typeof buildRuleProgram> | undefined;
let constitutionProgram: { key: string; program: ReturnType<typeof buildRuleProgram> } | undefined;

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function strings(value: unknown): string[] {
  return array(value).filter((item): item is string => typeof item === "string");
}

function sourceName(source: string): [string, string, string] {
  if (source === "$item") return ["item", "", ""];
  if (source.startsWith("$inputs.")) return ["input", "", source.slice("$inputs.".length)];
  const [step, port] = source.split(".", 2);
  return ["step", step, port ?? ""];
}

function parameterType(value: unknown): string {
  if (typeof value === "string") return value.includes("{{") ? "template" : "string";
  if (typeof value === "number") return Number.isInteger(value) ? "int" : "float";
  return typeof value === "boolean" ? "bool" : "unknown";
}

function typeReference(value: string) {
  const constructors: Array<"list" | "ref"> = [];
  let source = value.trim();
  while (/^(?:list|ref)</.test(source)) {
    if (!source.endsWith(">")) throw new Error("类型表达式 " + value + " 缺少结尾的 '>'");
    const bracket = source.indexOf("<");
    constructors.push(source.slice(0, bracket) as "list" | "ref");
    source = source.slice(bracket + 1, -1).trim();
  }
  if (source === "any") return { constructors, id: "any", raw: undefined };
  const match = /^([a-z][a-z0-9]*(?:[.-][a-z0-9]+)*)(?:@(\d+)(?:\.(\d+))?)?$/.exec(source);
  if (!match) throw new Error("类型表达式不合法 " + value);
  if (["any", "list", "ref"].includes(match[1])) throw new Error(match[1] + " 是保留的类型名");
  const [, name, major, minor] = match;
  if (major === undefined) return { constructors, id: name, raw: name };
  const version = String(BigInt(major));
  const id = name + "@" + version;
  return { constructors, id, raw: id + (minor === undefined ? "" : "." + String(BigInt(minor))) };
}

class ReferenceFactsBuilder {
  readonly facts: ReferenceFacts = {};
  readonly locations = new Map<string, Location>();
  readonly errors: Diagnostic[] = [];
  private readonly rejected = new Set<SpecDocument>();
  private readonly expressions = new Set<string>();
  get validDocuments(): SpecDocument[] { return this.documents.filter((doc) => !this.rejected.has(doc)); }

  constructor(private readonly documents: SpecDocument[]) {
    for (const document of documents) {
      const prior = this.errors.length;
      this.extractReferences(document);
      if (this.errors.length > prior) this.rejected.add(document);
    }
    if (this.rejected.size) {
      for (const key of Object.keys(this.facts)) delete this.facts[key];
      this.locations.clear();
      this.expressions.clear();
      const diagnostics = this.errors.splice(0);
      for (const document of documents) if (!this.rejected.has(document)) this.extractReferences(document);
      this.errors.push(...diagnostics);
    }
    this.registerExpression("any", "any", "");
  }

  private add(relation: string, ...row: MrlValue[]): void {
    (this.facts[relation] ??= []).push(row);
  }

  private registerExpression(id: string, form: string, argument: string): string {
    if (!this.expressions.has(id)) {
      this.expressions.add(id);
      this.add("texpr", id, form, argument);
    }
    return id;
  }

  private locate(subject: string, document: SpecDocument, path: string): void {
    if (!this.locations.has(subject)) this.locations.set(subject, { document, path });
  }

  private extractReferences(document: SpecDocument): void {
    const key = documentKey(document);
    if (!key) return;
    const kind = documentKind(document);
    const { metadata, spec: raw } = record(document.data);
    const { name, version, lifecycle = "active" } = record(metadata);
    const spec = record(raw);
    this.add("spec", key, kind, String(name), String(version), String(lifecycle));
    this.add("spec_doc", key, document.file, document.index);
    this.locate(key, document, "");
    const locate = (path: string, subject = path) => this.locate(key + "/" + subject, document, "spec." + path);
    const reference = (target: unknown, targetKind: string, path: string, subject = path) => {
      if (typeof target !== "string") return;
      this.add("ref", key + "/" + subject, targetKind, target);
      locate(path, subject);
    };
    const type = (target: unknown, path: string, subject = path) => {
      if (typeof target !== "string") return undefined;
      try {
        const { constructors, id, raw } = typeReference(target);
        reference(raw, "Type", path, subject);
        const root = this.registerExpression(id, raw ? "named" : "any", raw ? id : "");
        return constructors.reduceRight((argument, form) => this.registerExpression(form + "<" + argument + ">", form, argument), root);
      } catch (error) {
        this.errors.push(this.diagnostic("E-SCHEMA", "error", key + "/" + subject, String((error as Error).message), { document, path: "spec." + path }));
      }
    };
    const ports = (item: Record<string, unknown>, path = "", recipe?: string) => {
      for (const direction of ["inputs", "outputs"]) {
        for (const [port, value] of Object.entries(record(item[direction]))) {
          const portPath = path + direction + "." + port;
          const definition = record(value);
          const expression = type(definition.type, portPath + ".type", portPath);
          if (!expression) continue;
          const input = direction === "inputs";
          if (recipe) this.add(input ? "recipe_in" : "recipe_out", key, recipe, port, expression, definition.optional === true);
          if (kind === "Station") {
            const properties = strings(input ? definition.requires : definition.guarantees);
            this.add(input ? "in_port" : "out_port", key, port, expression, definition.optional === true, properties);
            for (const property of properties) this.add(input ? "in_require" : "out_guarantee", key, port, property);
            if (!input) this.registerExpression("list<" + expression + ">", "list", expression);
          }
        }
      }
    };
    switch (kind) {
      case "Type": {
        const [major, minor] = String(version).split(".");
        this.add("type_version", key, String(name) + "@" + major, Number(minor), String(lifecycle));
        this.add("type_kind", key, String(spec.kind ?? "atomic"));
        const parent = type(spec.extends, "extends");
        if (parent && !parent.includes("<") && parent !== "any") this.add("type_extends_decl", key, parent);
        for (const field of ["item", "target"]) {
          const expression = type(spec[field], field);
          if (expression) this.add("type_" + field, key, expression);
        }
        for (const [field, target] of Object.entries(record(spec.fields))) {
          const path = "fields." + field;
          const expression = type(target, path);
          if (expression) this.add("type_field", key, field, expression);
        }
        for (const [fact, declared] of Object.entries(record(spec.facts))) {
          if (typeof declared !== "string") continue;
          this.add("type_fact_decl", key, fact, declared);
          locate("facts." + fact);
        }
        for (const property of Object.keys(record(spec.properties))) {
          this.add("type_property", key, property);
          locate("properties." + property);
        }
        for (const property of strings(spec.always)) this.add("type_always", key, property);
        locate("always");
        for (const [index, rawValidator] of array(spec.validators).entries()) {
          const validator = record(rawValidator);
          const path = "validators[" + index + "]";
          if (typeof validator.name === "string" && typeof validator.image === "string") {
            this.add("type_validator", key, validator.name, validator.image);
            locate(path, "validators." + validator.name);
            for (const fact of strings(validator.emits)) this.add("validator_emits", key, validator.name, fact);
            for (const polarity of ["accept", "reject"]) {
              for (const fixture of strings(record(validator.fixtures)[polarity])) this.add("validator_fixture", key, validator.name, polarity, fixture);
            }
          }
        }
        break;
      }
      case "Machine": {
        const image = record(spec.runtime).image;
        if (typeof image === "string") {
          this.add("machine_image", key, image);
          this.locations.set(key, { document, path: "spec.runtime.image" });
        }
        const { repo, commit } = record(spec.source);
        if (typeof repo === "string" && typeof commit === "string") {
          this.add("machine_source", key, repo, commit);
          locate("source");
        }
        for (const [recipe, value] of Object.entries(record(spec.recipes))) {
          const definition = record(value);
          this.add("recipe", key, recipe);
          ports(definition, "recipes." + recipe + ".", recipe);
          if (Object.hasOwn(definition, "run")) this.add("recipe_run", key, recipe);
          for (const [parameter, value] of Object.entries(record(definition.params))) {
            const type = record(value).type;
            if (typeof type === "string") this.add("recipe_param", key, recipe, parameter, type);
          }
        }
        break;
      }
      case "OperatorRuntime": {
        const volume = record(spec.inject).volume;
        if (typeof volume === "string" && volume) this.add("runtime_volume", key, volume);
        if (typeof spec.kind === "string") this.add("runtime", key, spec.kind);
        for (const model of strings(spec.models_allowed)) this.add("runtime_model", key, model);
        break;
      }
      case "Station": {
        reference(spec.machine, "Machine", "machine");
        reference(record(spec.operator).runtime, "OperatorRuntime", "operator");
        reference(spec.policy, "Policy", "policy");
        reference(record(record(spec.gate).judge).runtime, "OperatorRuntime", "gate.judge.runtime", "gate.judge");
        const operator = record(spec.operator);
        if (typeof spec.role === "string") this.add("station", key, spec.role);
        if (typeof spec.machine === "string" && typeof spec.recipe === "string") this.add("station_machine", key, spec.machine, spec.recipe);
        if (typeof operator.runtime === "string") this.add("station_operator", key, operator.runtime, String(operator.model ?? ""));
        if (typeof spec.policy === "string") this.add("station_policy", key, spec.policy);
        const budget = record(spec.limits).budget_usd;
        if (typeof budget === "number") this.add("station_budget", key, budget);
        const phases = strings(spec.phases);
        this.add("station_phases", key, phases.length ? phases : ["execute"]);
        if (Array.isArray(spec.admit)) this.add("station_admit", key, spec.admit.length);
        if (typeof spec.idempotency_key === "string") this.add("sink_idempotency", key, spec.idempotency_key);
        if (typeof spec.verify_receipt === "string") this.add("sink_verify", key, spec.verify_receipt);
        for (const [parameter, value] of Object.entries(record(spec.params))) {
          const definition = record(value);
          if (typeof definition.type === "string") this.add("station_param", key, parameter, definition.type, Object.hasOwn(definition, "default"));
          locate("params." + parameter);
        }
        for (const phase of ["propose", "in_run"]) {
          const rule = record(spec.approval)[phase];
          if (rule !== undefined) this.add("approval", key, phase, typeof rule === "string" ? rule : "conditional");
          locate("approval." + phase);
        }
        for (const field of ["recipe", "inputs", "outputs", "limits", "phases", "admit", "verify_receipt", "fixtures", "equivalence"]) locate(field);
        ports(spec);
        const converter = record(spec.converts);
        const from = type(converter.from, "converts.from", "converts");
        const to = type(converter.to, "converts.to", "converts");
        if (from && to) {
          const preserves = strings(converter.preserves);
          this.add("converts", key, from, to, converter.lossless === true, preserves);
          for (const property of preserves) this.add("conv_preserve", key, property);
          const { usd_est = 0, p50_s = 0 } = record(spec.cost);
          this.add("conv_cost", key, Number(usd_est), Number(p50_s));
        }
        this.add("equivalence_count", key, array(spec.equivalence).length);
        for (const polarity of ["accept", "reject"]) {
          for (const fixture of strings(record(spec.fixtures)[polarity])) this.add("station_fixture", key, polarity, fixture);
        }
        for (const field of ["allowed_stations", "allowed_sinks"]) {
          for (const [index, target] of array(record(spec.plan_bounds)[field]).entries()) {
            reference(target, "Station", "plan_bounds." + field + "[" + index + "]");
          }
        }
        break;
      }
      case "Line": {
        this.add("line", key);
        const budget = record(spec.budget).cap_usd;
        if (typeof budget === "number") this.add("line_budget", key, budget);
        locate("budget");
        const io = record(spec.interface);
        for (const [port, definition] of Object.entries(record(io.inputs))) {
          const path = "interface.inputs." + port;
          const input = record(definition);
          const expression = type(input.type, path + ".type", path);
          if (!expression) continue;
          const requires = strings(input.requires);
          this.add("line_input", key, port, expression, requires);
          for (const property of requires) this.add("line_input_require", key, port, property);
        }
        for (const [port, definition] of Object.entries(record(io.outputs))) {
          const path = "interface.outputs." + port;
          const output = record(definition);
          if (typeof output.from !== "string") continue;
          locate(path);
          const [, step, sourcePort] = sourceName(output.from);
          this.add("line_output", key, port, step, sourcePort);
          const expression = type(output.type, path + ".type", path);
          if (expression) this.add("line_output_type", key, port, expression);
        }
        for (const [index, trigger] of array(spec.trigger).entries()) {
          type(record(trigger).type, "trigger[" + index + "].type", "trigger[" + index + "]");
        }
        for (const [id, rawStep] of Object.entries(record(spec.steps))) {
          const step = record(rawStep);
          const subject = "steps." + id;
          reference(step.station, "Station", subject + ".station", subject);
          reference(step.subline, "Line", subject + ".subline", subject);
          locate(subject);
          locate(subject + ".in");
          locate(subject + ".params");
          if (typeof step.station === "string") this.add("step", key, id, "station", step.station);
          else if (typeof step.subline === "string") this.add("step", key, id, "subline", step.subline);
          else this.add("step", key, id, "switch", "");
          for (const [port, binding] of Object.entries(record(step.in))) {
            const path = subject + ".in." + port;
            const { from, via } = record(binding);
            const source = typeof binding === "string" ? binding : from;
            if (typeof source !== "string") continue;
            locate(path);
            this.add("bind", key, id, port, ...sourceName(source));
            const chain = strings(via);
            this.add("bind_via", key, id, port, via === "none" ? "none" : Array.isArray(via) ? "explicit" : "auto", chain);
            for (const [index, station] of chain.entries()) {
              reference(station, "Station", path + ".via[" + index + "]");
            }
          }
          for (const [name, value] of Object.entries(record(step.params))) {
            this.add("step_param", key, id, name, parameterType(value));
            locate(subject + ".params." + name);
          }
          if (step.map) {
            const { over, max_items = -1, max_parallel = 1 } = record(step.map);
            if (typeof over === "string") this.add("map_over", key, id, ...sourceName(over));
            this.add("step_map", key, id, Array.isArray(over) ? over.length : Number(max_items), Number(max_parallel));
            locate(subject + ".map");
          }
          if (step.until) {
            this.add("step_until", key, id, Number(record(step.until).max_iter));
            locate(subject + ".until");
          }
          if (step.switch) {
            const { on, cases, default: fallback } = record(step.switch);
            if (typeof on === "string") this.add("switch_on", key, id, ...sourceName(on));
            for (const entry of array(cases)) {
              const { goto } = record(entry);
              if (typeof goto === "string") this.add("switch_target", key, id, goto);
            }
            if (typeof fallback === "string") this.add("switch_target", key, id, fallback);
            locate(subject + ".switch");
          }
        }
        for (const kept of strings(spec.keep)) {
          const [, step, port] = sourceName(kept);
          this.add("keep", key, step, port);
        }
        locate("keep");
        break;
      }
      case "Policy":
        this.add("policy", key);
        for (const role of strings(spec.applies_to)) this.add("policy_applies", key, role);
        for (const tier of Object.keys(record(spec.egress))) this.add("policy_egress", key, tier);
        for (const secret of strings(spec.secrets)) this.add("policy_secret", key, secret);
        break;
      case "GoldenSet":
      case "Baseline":
        reference(spec.station, "Station", "station");
        break;
      case "Agent":
        reference(spec.runtime, "OperatorRuntime", "runtime");
        reference(spec.policy, "Policy", "policy");
        break;
    }
  }

  diagnostic(code: string, severity: Diagnostic["severity"], subject: string, message: string, location = this.locations.get(subject)): Diagnostic {
    if (!location) return { code, severity, subject, message };
    const { document, path } = location;
    const [line, col] = documentPosition(document, path);
    return { code, severity, subject, message, file: document.file, doc: document.index, path, line, col };
  }
}

export function checkFactoryKernel(
  documents: SpecDocument[],
  constitution: Registry["constitution"],
  host: Diagnostic[],
  onDiagnosticRule?: (location: RuleLocation) => void,
): { diagnostics: Diagnostic[]; resolutions: FactoryResolution[] } {
  const input = new ReferenceFactsBuilder(documents);
  program ??= buildRuleProgram(modules, resolutionFunctions);
  const errors = [...input.errors];
  input.facts.host_diag = [...host, ...input.errors].map((item) => [item.code, item.subject, item.message]);
  let executable = program;
  if (!constitution.length) {
    errors.push({ code: "E-CONSTITUTION", subject: "Registry", message: "constitution directory has no MRL source", severity: "error" });
  } else {
    try {
      const key = JSON.stringify(constitution);
      if (constitutionProgram?.key !== key) {
        constitutionProgram = {
          key,
          program: buildRuleProgram([...modules, ...constitution.map(({ file, source }) => ({ name: file, source }))], resolutionFunctions),
        };
      }
      executable = constitutionProgram.program;
    } catch (error) {
      errors.push(constitutionError(error));
    }
  }
  const triggered: RuleLocation[] = [];
  const trace = onDiagnosticRule ? (location: RuleLocation) => triggered.push(location) : undefined;
  const run = () => {
    triggered.length = 0;
    try {
      return executable.evaluate(input.facts, 1_000_000, trace);
    } catch (error) {
      if (executable === program) throw error;
      // An invalid constitution cannot prevent the host kernel from reporting its diagnostics.
      errors.push(constitutionError(error));
      executable = program!;
      triggered.length = 0;
      return executable.evaluate(input.facts, 1_000_000, trace);
    }
  };
  let result = run();
  const sites = checkExpressionSites(input.validDocuments, input.facts, result);
  if (sites.length) {
    input.facts.host_diag.push(...sites.map((item) => [item.code, item.subject, item.message]));
    result = run();
  }
  if (onDiagnosticRule) for (const location of triggered) onDiagnosticRule(location);
  const levels = new Map(result.rowsOf("code").map(([code, level]) => [code, level]));
  const provided = new Map([...host, ...input.errors, ...sites].map((item) => [JSON.stringify([item.code, item.subject, item.message]), item]));
  const diagnostics = result.rowsOf("diag").map(([code, subject, message]) => {
    const origin = provided.get(JSON.stringify([code, subject, message]));
    if (origin) return origin;
    const severity = levels.get(code) as Diagnostic["severity"] | undefined;
    if (!severity) return input.diagnostic("E-CONSTITUTION", "error", String(subject), "未登记的诊断码 " + String(code) + "：" + String(message));
    return input.diagnostic(String(code), severity, String(subject), String(message));
  });
  const edges = new Map(result.rowsOf("edge").map(([line, step, port, from, guarantees, to, requires]) =>
    [JSON.stringify([line, step, port]), { from, guarantees, to, requires }]));
  const resolutions = result.rowsOf("resolution").map(([line, step, port, value]) => ({
    ...edges.get(JSON.stringify([line, step, port])),
    ...value as Record<string, MrlValue>,
    line: String(line), step: String(step), port: String(port),
  }));
  return { diagnostics: [...errors.filter((item) => !input.errors.includes(item)), ...diagnostics], resolutions };
}

function constitutionError(error: unknown): Diagnostic {
  const location = error instanceof MrlRuleError ? error.location : undefined;
  const message = error instanceof Error ? error.message : String(error);
  return {
    code: "E-CONSTITUTION", severity: "error",
    subject: location?.file ? "File:" + location.file : "Registry",
    file: location?.file, line: location?.line,
    message: error instanceof MrlRuleError ? error.code + "：" + message : message,
  };
}
