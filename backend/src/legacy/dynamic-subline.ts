import { FactoryError } from "./errors";
import { RunPlan, type RunPlanJSON } from "./runplan";
import type { Snapshot } from "./snapshot";

export interface SublineDefinition {
  readonly line: string;
  readonly digest: string;
  readonly plan: unknown;
}

interface SublinePort {
  type: string;
  requires?: string[];
  guarantees?: string[];
  optional?: boolean;
}

export interface SublineShape {
  role: "subline";
  inputs: Record<string, SublinePort>;
  outputs: Record<string, SublinePort>;
  limits: { budget_usd: number };
}

export function freezeSubline(plan: RunPlan): SublineDefinition {
  return { line: plan.line, digest: plan.digest(), plan: plan.toJSON() };
}

export function restoreSubline(definition: SublineDefinition, epoch: string, step: string): RunPlan {
  try {
    const plan = RunPlan.fromJSON(definition.plan as RunPlanJSON);
    if (plan.epoch === epoch && plan.line === definition.line && plan.digest() === definition.digest) return plan;
  } catch {
    // A corrupt nested plan is reported in terms of the parent control node.
  }
  throw new FactoryError("RUN_PLAN_INVALID", "子产线计划摘要或 epoch 不匹配", { step, line: definition.line, epoch });
}

function sublineOutput(snapshot: Snapshot, plan: RunPlan, port: string): SublinePort {
  const [nodeId, outPort] = plan.outputs[port];
  const node = plan.node(nodeId);
  const followed = node.subline ? restoreSubline(node.subline, plan.epoch, node.step) : undefined;
  const source = followed
    ? sublineOutput(snapshot, followed, outPort)
    : snapshot.spec(node.station).outputs?.[outPort] as SublinePort | undefined;
  if (!source) throw new FactoryError("RUN_PLAN_INVALID", "子产线输出缺少来源端口", { line: plan.line, node: nodeId, outPort });
  const type = snapshot.spec(plan.line).interface?.outputs?.[port]?.type ?? (node.kind === "map" ? `list<${source.type}>` : source.type);
  return { ...source, type };
}

export function sublineShape(snapshot: Snapshot, plan: RunPlan): SublineShape {
  const specification = snapshot.spec(plan.line);
  if (!specification.interface || plan.budgetCapUsd === null) {
    throw new FactoryError("RUN_PLAN_INVALID", "子产线需要声明接口和预算上限", { line: plan.line });
  }
  return {
    role: "subline",
    inputs: Object.fromEntries(Object.entries(plan.inputs).map(([port, type]) => [port, {
      type, requires: [...(plan.inputRequires[port] ?? [])],
    }])),
    outputs: Object.fromEntries(Object.keys(plan.outputs).map((port) => [port, sublineOutput(snapshot, plan, port)])),
    limits: { budget_usd: plan.budgetCapUsd },
  };
}
