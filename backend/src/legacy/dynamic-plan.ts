import { checkRegistry } from "../../mrl/factory/check";
import type { FactoryResolution } from "../../mrl/factory/references";
import { validate } from "../../mrl/factory/schema";
import { mapCount, readMap } from "./dynamic-map";
import { restoreSubline } from "./dynamic-subline";
import { compileDynamicRunPlan } from "./dynamic-switch";
import { FactoryError } from "./errors";
import { canonicalBytes, digestBytes } from "./foundation";
import { RunPlan, type PlanNode } from "./runplan";
import type { Run } from "./runs";
import { Snapshot } from "./snapshot";

interface PlanDiagnostic { code: string; subject: string; message: string }

export interface PlanCheck {
  ok: boolean;
  diagnostics: readonly PlanDiagnostic[];
  feedback: string;
  plan: RunPlan | null;
  resolutions: readonly FactoryResolution[];
}

type PlanBounds = {
  allowed_stations?: readonly string[];
  allowed_sinks?: readonly string[];
  max_depth?: number;
  max_fanout?: number;
  max_steps?: number;
  budget_usd?: number;
};

type PlanDocument = {
  kind: "Plan";
  metadata: { name: string; generated_by?: string; run?: string };
  spec: {
    steps: Record<string, {
      station?: string; subline?: string; map?: unknown; until?: { max_iter: number };
      in?: Record<string, string | { via?: unknown }>;
    }>;
    keep?: string[];
  };
};

export function planCost(plan: RunPlan): number {
  return plan.budgetItems.reduce((sum, [, usd]) => sum + usd, 0);
}

export function exceedsPlanBudget(need: number, limit: number): boolean {
  return need - limit > Number.EPSILON * Math.max(need, Math.abs(limit)) * 8;
}

export function compilePlan(snapshot: Snapshot, document: unknown, options: {
  plannerKey: string;
  run: Pick<Run, "id" | "line" | "epoch" | "budgetCapUsd" | "spentUsd" | "reservedUsd">;
  remainingUsd?: number;
  deferRunBudget?: boolean;
}): PlanCheck {
  const { run } = options;
  if (run.epoch !== snapshot.epoch) throw new FactoryError("EPOCH_MISMATCH", "Planner 与父产线的快照不同", { run: run.id, epoch: run.epoch });
  const planner = snapshot.key("Station", options.plannerKey);
  const station = snapshot.spec(planner);
  if (station.role !== "planner") throw new FactoryError("RUN_PLAN_INVALID", "只有 planner 工位可以提交 Plan", { planner });

  const diagnostics: PlanDiagnostic[] = [];
  const add = (code: string, subject: string, message: string) => { diagnostics.push({ code, subject, message }); };
  const finish = (plan: RunPlan | null = null, resolutions: readonly FactoryResolution[] = []): PlanCheck => ({
    ok: plan !== null, plan, resolutions, diagnostics,
    feedback: diagnostics.map(({ code, subject, message }) => `- ${code}: ${subject} ${message}`).join("\n"),
  });
  if (!document || typeof document !== "object" || !("kind" in document) || document.kind !== "Plan") {
    add("PLAN_INVALID", "kind", "只允许提交 Plan 文档");
    add("E-SCHEMA", "kind", "文档不符合 Plan schema");
    return finish();
  }
  for (const issue of validate(document)) add("E-SCHEMA", issue.path, issue.message);
  if (diagnostics.length) return finish();
  const { metadata, spec } = document as PlanDocument;
  if (metadata.generated_by !== undefined && metadata.generated_by !== planner) add("PLAN_INVALID", "metadata.generated_by", "与当前 planner 工位不同");
  if (metadata.run !== undefined && metadata.run !== run.id) add("PLAN_INVALID", "metadata.run", "与当前运行不同");

  const parent = snapshot.spec(snapshot.key("Line", run.line));
  const bounds = (station.plan_bounds ?? {}) as PlanBounds;
  const name = `runtime-plan-${digestBytes(canonicalBytes(document)).slice(7, 23)}`;
  let version = 1;
  while (Object.hasOwn(snapshot.docs, `Line:${name}@${version}`)) version++;
  const line = `Line:${name}@${version}`;
  const file = `lines/${name}-${version}.yaml`;
  const lineDocument = {
    apiVersion: "mao/v1", kind: "Line", metadata: { name, version },
    spec: { ...spec, interface: { inputs: parent.interface?.inputs ?? {} } },
  };
  const schemaIssues = validate(lineDocument);
  for (const issue of schemaIssues) add("E-SCHEMA", issue.path, issue.message);
  if (schemaIssues.length) return finish();

  let minimumSteps = 0;
  for (const [step, declaration] of Object.entries(spec.steps)) {
    for (const [kind, reference] of [["Station", declaration.station], ["Line", declaration.subline]] as const) {
      if (reference && !Object.hasOwn(snapshot.docs, snapshot.key(kind, reference))) add("PLAN_INVALID", step, `当前 epoch 不包含 ${reference}`);
    }
    let count = declaration.until?.max_iter ?? 1;
    if (declaration.map) {
      try {
        const map = readMap(line, step, declaration.map);
        if ("items" in map.over) {
          count = mapCount(map, map.over.items.length) ?? map.over.items.length;
          const limit = bounds.max_fanout === undefined ? undefined : Math.min(bounds.max_fanout, map.maxItems ?? Infinity);
          if (limit !== undefined && count > limit) add("PLAN_BOUNDS", step, `Map 展开 ${count} 超过有效上限 ${limit}`);
        } else {
          count = map.maxItems ?? 1;
          if (map.maxItems === undefined && (bounds.max_fanout !== undefined || bounds.max_steps !== undefined)) {
            add("PLAN_BOUNDS", step, "受限的动态 Map 需要声明 max_items");
          }
        }
        if (!("items" in map.over) && bounds.max_fanout !== undefined && count > bounds.max_fanout) {
          add("PLAN_BOUNDS", step, `Map 展开 ${count} 超过 max_fanout ${bounds.max_fanout}`);
        }
      } catch (error) {
        if (!(error instanceof FactoryError)) throw error;
        add("PLAN_INVALID", step, error.message);
      }
    }
    minimumSteps += count;
  }
  if (bounds.max_steps !== undefined && minimumSteps > bounds.max_steps) add("PLAN_BOUNDS", "steps", `至少展开 ${minimumSteps} 步，超过 max_steps ${bounds.max_steps}`);

  const registry = snapshot.result.registry;
  const checked = checkRegistry({ ...registry, docs: [...registry.docs, { file, index: 0, line: 1, data: lineDocument, expectedKind: "Line", positions: {} }] });
  for (const issue of checked.diagnostics) {
    // Plan output is materialized even when it is absent from the optional keep list.
    if (issue.severity !== "error" || issue.code === "E-ORPHAN") continue;
    if (issue.file === file || issue.subject === line || issue.subject.startsWith(`${line}/`)) {
      add(issue.code, issue.subject.replace(line, "Plan"), issue.message);
    }
  }
  const resolutions = checked.resolutions.filter((row) => {
    if (row.line !== line) return false;
    const binding = spec.steps[row.step]?.in?.[row.port];
    return typeof binding !== "object" || binding.via === undefined;
  });
  if (diagnostics.length) return finish(null, resolutions);

  const checkedSnapshot = new Snapshot(snapshot.root, snapshot.epoch, { ...snapshot.docs, [line]: lineDocument }, checked);
  let compiled: RunPlan;
  try {
    compiled = compileDynamicRunPlan(checkedSnapshot, line);
  } catch (error) {
    if (!(error instanceof FactoryError)) throw error;
    add("PLAN_INVALID", String(error.detail.step ?? "steps"), error.message);
    return finish(null, resolutions);
  }

  const allowedStations = bounds.allowed_stations?.map((ref) => snapshot.key("Station", ref));
  const allowedSinks = bounds.allowed_sinks?.map((ref) => snapshot.key("Station", ref));
  function checkStation(node: PlanNode, step: string) {
    if (node.implicit || node.kind === "switch") return;
    if (node.role === "planner") add("PLAN_BOUNDS", step, "Plan 不能再产出 Plan");
    if (allowedStations && !allowedStations.includes(node.station)) add("PLAN_BOUNDS", step, "工位不在 allowed_stations");
    if (node.role === "sink" && allowedSinks && !allowedSinks.includes(node.station)) add("PLAN_BOUNDS", step, "Sink 不在 allowed_sinks");
  }
  function inspect(plan: RunPlan, depth: number, prefix: string): number {
    if (bounds.max_depth !== undefined && depth > bounds.max_depth) add("PLAN_BOUNDS", prefix, `深度 ${depth} 超过 max_depth ${bounds.max_depth}`);
    let stepCount = 0;
    for (const node of plan.nodes) {
      const step = prefix ? `${prefix}.${node.id}` : node.id;
      if (node.subline) {
        stepCount += 1 + inspect(restoreSubline(node.subline, plan.epoch, node.step), depth + 1, step);
        continue;
      }
      if (node.mapInstance) { stepCount++; continue; }
      checkStation(node, step);
      if (node.map) {
        const { over, maxItems, family } = node.map;
        const count = "items" in over ? mapCount(node.map, over.items.length) ?? over.items.length : maxItems ?? 1;
        const limit = bounds.max_fanout === undefined ? undefined : Math.min(bounds.max_fanout, maxItems ?? Infinity);
        if (limit !== undefined && count > limit) add("PLAN_BOUNDS", step, `Map 展开 ${count} 超过有效上限 ${limit}`);
        if (!("items" in over) && maxItems === undefined && (bounds.max_fanout !== undefined || bounds.max_steps !== undefined)) {
          add("PLAN_BOUNDS", step, "受限的动态 Map 需要声明 max_items");
        }
        if (!plan.mapInstances(node.id)) stepCount += count * family.length;
      } else stepCount += node.until?.maxIter ?? 1;
    }
    return stepCount;
  }
  const expanded = inspect(compiled, 1, "steps");
  if (bounds.max_steps !== undefined && expanded > bounds.max_steps) add("PLAN_BOUNDS", "steps", `展开 ${expanded} 步，超过 max_steps ${bounds.max_steps}`);
  const need = planCost(compiled);
  const remaining = options.remainingUsd ?? run.budgetCapUsd - run.spentUsd - run.reservedUsd;
  if (bounds.budget_usd !== undefined && exceedsPlanBudget(need, bounds.budget_usd)) add("PLAN_BUDGET", "budget", `需要 ${need} USD，超过 planner 上限 ${bounds.budget_usd} USD`);
  if (!options.deferRunBudget && exceedsPlanBudget(need, remaining)) add("PLAN_BUDGET", "budget", `需要 ${need} USD，超过 Run 剩余预算 ${remaining} USD`);
  return finish(diagnostics.length ? null : new RunPlan({ ...compiled, line: run.line }), resolutions);
}

export function attachedPlanNodes(owner: string, plan: RunPlan): PlanNode[] {
  const qualify = (id: string) => `${owner}/${id}`;
  const rename = (node: PlanNode): PlanNode => {
    const selection = node.switch;
    const map = node.map;
    return {
      ...node,
      id: qualify(node.id),
      step: qualify(node.step),
      deps: node.deps.map(qualify),
      inputs: node.inputs.map((binding) => binding.source === "node" ? { ...binding, node: qualify(binding.node) } : binding),
      ...(node.mapInstance ? { mapInstance: { ...node.mapInstance, owner: qualify(node.mapInstance.owner) } } : {}),
      ...(selection ? { switch: {
        on: selection.on.source === "node" ? { ...selection.on, node: qualify(selection.on.node) } : selection.on,
        default: qualify(selection.default),
        cases: selection.cases.map((branch) => ({ ...branch, goto: qualify(branch.goto) })),
      } } : {}),
      ...(map ? { map: {
        ...map,
        over: !("items" in map.over) && map.over.source === "node" ? { ...map.over, node: qualify(map.over.node) } : map.over,
        family: map.family.map(rename),
      } } : {}),
    };
  };
  return plan.nodes.map(rename).map((node) => node.deps.length ? node : { ...node, deps: [owner] });
}
