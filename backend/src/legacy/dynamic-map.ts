import { FactoryError } from "./errors";
import type { PlanNode } from "./runplan";

export type MapValue = string | number | boolean;
export type MapSource = { source: "input" | "node"; node: string; port: string };
export interface MapDefinition {
  readonly over: { items: readonly MapValue[] } | MapSource;
  readonly as?: string;
  readonly maxItems?: number;
  readonly maxParallel: number;
  readonly overflow: "reject" | "truncate";
}

export type MapPlan = MapDefinition & { readonly family: readonly PlanNode[] };

function invalid(line: string, step: string, reason: string): never {
  throw new FactoryError("RUN_PLAN_INVALID", "map 展开设置不合法", { line, step, reason });
}

export function readMap(line: string, step: string, value: unknown): MapDefinition {
  const settings = value as { over?: unknown; as?: unknown; max_items?: unknown; max_parallel?: unknown; overflow?: unknown } | undefined;
  const over = settings?.over;
  const alias = settings?.as;
  const limit = settings?.max_items;
  const parallel = settings?.max_parallel ?? 1;
  const overflow = settings?.overflow ?? "reject";
  if (typeof parallel !== "number" || !Number.isSafeInteger(parallel) || parallel < 1) return invalid(line, step, "max-parallel");
  if (limit !== undefined && (typeof limit !== "number" || !Number.isSafeInteger(limit) || limit < 1)) return invalid(line, step, "max-items");
  if (overflow !== "reject" && overflow !== "truncate") return invalid(line, step, "overflow");
  if (alias !== undefined && (typeof alias !== "string" || !/^[a-z][a-z0-9_]*$/.test(alias) || ["inputs", "run"].includes(alias))) return invalid(line, step, "as");
  const options: Omit<MapDefinition, "over"> = {
    ...(alias !== undefined ? { as: alias } : {}), ...(limit !== undefined ? { maxItems: limit } : {}), maxParallel: parallel, overflow,
  };
  if (Array.isArray(over)) {
    if (!alias || !over.length || !over.every((item) => typeof item === "string" || typeof item === "boolean" || (typeof item === "number" && Number.isFinite(item)))) {
      return invalid(line, step, "literal-source");
    }
    return { ...options, over: { items: [...over] } };
  }
  const match = typeof over === "string" && /^(\$inputs|[a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$/.exec(over);
  if (!match) return invalid(line, step, "source");
  return { ...options, over: { source: match[1] === "$inputs" ? "input" : "node", node: match[1], port: match[2] } };
}

export function mapCount(definition: MapDefinition, sourceLength: number, line: string, step: string): number {
  const limit = definition.maxItems;
  if (limit === undefined || sourceLength <= limit) return sourceLength;
  if (definition.overflow === "truncate") return limit;
  throw new FactoryError("DYNAMIC_OVERFLOW", "map 列表长度超过展开上限", { line, step, length: sourceLength, max_items: limit });
}

export function mapFamily(family: readonly PlanNode[], index: number): PlanNode[] {
  const suffix = `#${index}`;
  const ids = new Map(family.map((node) => [node.id, node.id + suffix]));
  return family.map((node) => ({
    ...node, id: ids.get(node.id)!,
    deps: node.deps.map((dependency) => ids.get(dependency) ?? dependency),
    inputs: node.inputs.map((binding) => binding.source === "node" && ids.has(binding.node) ? { ...binding, node: ids.get(binding.node)! } : binding),
  }));
}
