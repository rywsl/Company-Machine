import { MrlStaticError } from "../../mrl/checker";
import { compileExpr, MrlEvalError, type EvalEnvironment } from "../../mrl/evaluator";
import { MrlSyntaxError } from "../../mrl/lexer";
import { BOOL, DYN, INT, record } from "../../mrl/types";
import { FactoryError } from "./errors";
import type { PlanNode } from "./runplan";
import type { RunNode } from "./runs";
import type { Snapshot } from "./snapshot";
import { inputsValue } from "./values";
import type { Warehouse } from "./warehouse";

export interface UntilDefinition {
  readonly cond: string;
  readonly maxIter: number;
}

export function readUntil(line: string, step: string, value: unknown): UntilDefinition {
  const settings = value as { cond?: unknown; max_iter?: unknown } | undefined;
  const cond = settings?.cond;
  const maxIter = settings?.max_iter;
  if (typeof cond !== "string" || !cond.trim() || typeof maxIter !== "number" || !Number.isSafeInteger(maxIter) || maxIter < 1 || maxIter > 10) {
    throw new FactoryError("RUN_PLAN_INVALID", "until 条件或迭代上限不合法", { line, step });
  }
  return { cond, maxIter };
}

export function untilHolds(snapshot: Snapshot, warehouse: Warehouse, station: string, step: string, definition: UntilDefinition, iteration: RunNode, attempt: number): boolean {
  const ports = snapshot.spec(station).outputs ?? {};
  const material = Object.fromEntries(Object.entries(iteration.outputs).map(([port, ids]) => {
    const beans = ids.map((id) => warehouse.get(id));
    const list = typeof ports[port]?.type === "string" && ports[port].type.startsWith("list<");
    return [port, !list && beans.length === 1 ? beans[0] : beans];
  }));
  const environment = { gate: { attempt }, out: inputsValue(material) } as EvalEnvironment;
  try {
    return compileExpr(definition.cond, { environment: { gate: record({ attempt: INT }), out: DYN }, expect: BOOL }).evaluate(environment) === true;
  } catch (error) {
    if (!(error instanceof MrlEvalError || error instanceof MrlStaticError || error instanceof MrlSyntaxError)) throw error;
    const mrl = error instanceof MrlStaticError ? error.diagnostics[0]?.code ?? "MRL-TYPE" : error.code;
    throw new FactoryError("RUN_PLAN_INVALID", "until 条件求值失败", { step, reason: "until-eval", mrl });
  }
}

export function untilIteration(node: PlanNode, attempt: number): PlanNode {
  return { ...node, id: `${node.id}@${attempt}`, kind: "station" };
}
