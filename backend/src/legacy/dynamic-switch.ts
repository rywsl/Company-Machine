import { MrlStaticError } from "../../mrl/checker";
import { compileExpr, MrlEvalError, type EvalEnvironment } from "../../mrl/evaluator";
import { MrlSyntaxError } from "../../mrl/lexer";
import { BOOL, DYN } from "../../mrl/types";
import { readUntil } from "./dynamic-until";
import { FactoryError } from "./errors";
import { compileRunPlan, RunPlan, topologicalOrder, type PlanNode } from "./runplan";
import { Snapshot } from "./snapshot";
import { beanValue } from "./values";
import type { Bean } from "./warehouse";

export interface SwitchDefinition {
  readonly on: { source: "input" | "node"; node: string; port: string };
  readonly cases: ReadonlyArray<{ when: string; goto: string }>;
  readonly default: string;
}

function invalid(line: string, step: string, reason: string): never {
  throw new FactoryError("RUN_PLAN_INVALID", "switch 的来源或分支定义不合法", { line, step, reason });
}

function choice(line: string, step: string, value: unknown): SwitchDefinition {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid(line, step, "switch");
  const { on, cases, default: fallback } = value as Record<string, unknown>;
  const match = typeof on === "string" && /^(\$inputs|[a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$/.exec(on);
  if (!match) return invalid(line, step, "switch-source");
  if (typeof fallback !== "string" || !Array.isArray(cases)) return invalid(line, step, "switch-cases");
  const branches = cases.map((entry): { when: string; goto: string } => {
    if (typeof entry?.when !== "string" || typeof entry?.goto !== "string") return invalid(line, step, "switch-case");
    return { when: entry.when, goto: entry.goto };
  });
  return { on: { source: match[1] === "$inputs" ? "input" : "node", node: match[1], port: match[2] }, cases: branches, default: fallback };
}

export function switchTargets(definition: SwitchDefinition): string[] {
  return [...new Set([...definition.cases.map((entry) => entry.goto), definition.default])];
}

export function compileDynamicRunPlan(snapshot: Snapshot, lineRef: string): RunPlan {
  const line = snapshot.key("Line", lineRef);
  const document = snapshot.doc(line);
  const declarations = Object.entries(document.spec.steps as Record<string, Record<string, unknown>>);
  const switches = declarations.filter(([, declaration]) => Object.hasOwn(declaration, "switch"));
  const loops = new Map(declarations.filter(([, declaration]) => Object.hasOwn(declaration, "until"))
    .map(([step, declaration]) => [step, readUntil(line, step, declaration.until)]));
  if (!switches.length && !loops.size) return compileRunPlan(snapshot, line);
  const stepNames = new Set(declarations.map(([name]) => name));
  const stations = declarations.filter(([, declaration]) => !Object.hasOwn(declaration, "switch"))
    .map(([step, { until: _until, ...station }]) => [step, station]);
  const stationOnly = new Snapshot(snapshot.root, snapshot.epoch, {
    ...snapshot.docs, [line]: { ...document, spec: { ...document.spec, steps: Object.fromEntries(stations) } },
  }, snapshot.result);
  const base = compileRunPlan(stationOnly, line);
  const nodes = new Map(base.nodes.map((node) => [node.id, node]));
  const controllers = new Map<string, string[]>();

  for (const [step, declaration] of switches) {
    const definition = choice(line, step, declaration.switch);
    const { on } = definition;
    if (on.source === "input") {
      if (!Object.hasOwn(base.inputs, on.port)) return invalid(line, step, "switch-source");
    } else {
      const producer = nodes.get(on.node);
      if (!producer || !Object.hasOwn(snapshot.spec(producer.station).outputs ?? {}, on.port)) {
        return invalid(line, step, "switch-source");
      }
    }
    for (const target of switchTargets(definition)) {
      if (!stepNames.has(target)) return invalid(line, step, "switch-target");
      controllers.set(target, [...(controllers.get(target) ?? []), step]);
    }
    nodes.set(step, {
      id: step, step, kind: "switch", station: "", role: "control", params: {}, templated: [],
      inputs: [], deps: on.source === "node" ? [on.node] : [], budgetUsd: 0, implicit: false, resolution: null, switch: definition,
    });
  }
  for (const [id, node] of nodes) {
    const incoming = controllers.get(node.step);
    const until = loops.get(id);
    if (until) nodes.set(id, { ...node, kind: "until", until, deps: [...new Set([...node.deps, ...(incoming ?? [])])].sort() });
    else if (incoming) nodes.set(id, { ...node, deps: [...new Set([...node.deps, ...incoming])].sort() });
  }
  const budgetItems = base.budgetItems.map(([id, amount]) => [id, amount * (loops.get(id)?.maxIter ?? 1)] as const);
  return new RunPlan({ ...base, nodes: topologicalOrder(line, nodes), budgetItems });
}

export function selectSwitchTarget(node: PlanNode, materials: readonly Bean[]): string {
  const definition = node.switch;
  if (!definition) throw new FactoryError("RUN_PLAN_INVALID", "运行计划缺少 switch 定义", { step: node.step });
  const values = materials.map(beanValue);
  const env = { bean: values.length === 1 ? values[0] : values } as EvalEnvironment;
  for (const [index, branch] of definition.cases.entries()) {
    try {
      if (compileExpr(branch.when, { environment: { bean: DYN }, expect: BOOL }).evaluate(env) === true) return branch.goto;
    } catch (error) {
      if (!(error instanceof MrlEvalError || error instanceof MrlStaticError || error instanceof MrlSyntaxError)) throw error;
      const mrl = error instanceof MrlStaticError ? error.diagnostics[0]?.code ?? "MRL-TYPE" : error.code;
      throw new FactoryError("SWITCH_EVAL", "switch 条件求值失败", { step: node.step, index, mrl });
    }
  }
  return definition.default;
}
