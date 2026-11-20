import { buildRuleProgram } from "../../mrl/rules";
import { FactoryError } from "./errors";
import { canonicalBytes, digestBytes } from "./foundation";
import type { Snapshot } from "./snapshot";

export interface Binding {
  readonly port: string;
  readonly source: "input" | "node";
  readonly node: string;
  readonly outPort: string;
}

export interface PlanNode {
  readonly id: string;
  readonly kind: "station" | "converter";
  readonly step: string;
  readonly station: string;
  readonly role: string;
  readonly params: Readonly<Record<string, Parameter>>;
  readonly templated: readonly string[];
  readonly inputs: readonly Binding[];
  readonly deps: readonly string[];
  readonly budgetUsd: number;
  readonly implicit: boolean;
  readonly resolution: LineResolution | null;
}

type Parameter = string | boolean | number;
type PortPair = readonly [node: string, port: string];
type BudgetItem = readonly [item: string, usd: number];
type BudgetRow = [item: string, usd: number];

interface Plan {
  readonly line: string;
  readonly epoch: string;
  readonly nodes: readonly PlanNode[];
  readonly outputs: Readonly<Record<string, PortPair>>;
  readonly keep: readonly PortPair[];
  readonly inputs: Readonly<Record<string, string>>;
  readonly inputRequires: Readonly<Record<string, readonly string[]>>;
  readonly budgetCapUsd: number | null;
  readonly budgetItems: readonly BudgetItem[];
}

export class RunPlan implements Plan {
  readonly line: string;
  readonly epoch: string;
  readonly nodes: readonly PlanNode[];
  readonly outputs: Plan["outputs"];
  readonly keep: Plan["keep"];
  readonly inputs: Plan["inputs"];
  readonly inputRequires: Plan["inputRequires"];
  readonly budgetCapUsd: number | null;
  readonly budgetItems: Plan["budgetItems"];

  private readonly nodesById: ReadonlyMap<string, PlanNode>;

  constructor(plan: Plan) {
    const value = structuredClone(plan);
    this.line = value.line;
    this.epoch = value.epoch;
    this.nodes = value.nodes;
    this.outputs = value.outputs;
    this.keep = value.keep;
    this.inputs = value.inputs;
    this.inputRequires = value.inputRequires;
    this.budgetCapUsd = value.budgetCapUsd;
    this.budgetItems = value.budgetItems;
    this.nodesById = new Map(this.nodes.map((node) => [node.id, node]));
  }

  static fromJSON(value: RunPlanJSON): RunPlan {
    return new RunPlan({
      line: value.line,
      epoch: value.epoch,
      inputs: value.inputs,
      inputRequires: value.input_requires,
      budgetCapUsd: value.budget_cap_usd,
      budgetItems: value.budget_items,
      outputs: value.outputs,
      keep: value.keep,
      nodes: value.nodes.map(({ inputs, budget_usd, ...node }) => ({
        ...node,
        budgetUsd: budget_usd,
        inputs: inputs.map(({ out_port, ...input }) => ({ ...input, outPort: out_port })),
      })),
    });
  }

  node(id: string): PlanNode {
    const node = this.nodesById.get(id);
    if (!node) throw new FactoryError("NODE_NOT_FOUND", "运行计划没有该节点", { node: id });
    return node;
  }

  toJSON() {
    return structuredClone({
      line: this.line,
      epoch: this.epoch,
      inputs: this.inputs,
      input_requires: this.inputRequires,
      budget_cap_usd: this.budgetCapUsd,
      budget_items: this.budgetItems,
      nodes: this.nodes.map((node) => ({
        id: node.id,
        kind: node.kind,
        step: node.step,
        station: node.station,
        role: node.role,
        params: node.params,
        templated: node.templated,
        deps: node.deps,
        inputs: node.inputs.map(({ port, source, node, outPort }) => ({ port, source, node, out_port: outPort })),
        budget_usd: node.budgetUsd,
        implicit: node.implicit,
        resolution: node.resolution,
      })),
      outputs: this.outputs,
      keep: this.keep,
    });
  }

  digest(): string {
    return digestBytes(canonicalBytes(this.toJSON()));
  }
}

export type RunPlanJSON = ReturnType<RunPlan["toJSON"]>;

interface Port {
  type: string;
  requires?: string[];
  guarantees?: string[];
  optional?: boolean;
}

interface Station {
  role: string;
  inputs?: Record<string, Port>;
  outputs?: Record<string, Port>;
  params?: Record<string, { default?: unknown }>;
  limits?: { budget_usd?: number };
  converts?: { from: string; to: string; lossless: boolean; preserves?: string[] };
  cost?: { usd_est?: number; p50_s?: number };
}

type Via = "none" | string[];
type LineBinding = string | { from: string; via?: Via };
type LineInput = { type: string; requires?: string[] };
type LineOutput = { from: string; type?: string };

interface LineStep {
  station?: string;
  in?: Record<string, LineBinding>;
  params?: Record<string, Parameter>;
  map?: unknown;
  switch?: unknown;
  until?: unknown;
  subline?: string;
}

interface LineSpec {
  steps: Record<string, LineStep>;
  interface?: { inputs?: Record<string, LineInput>; outputs?: Record<string, LineOutput> };
  keep?: string[];
  budget?: { cap_usd: number };
}

interface Step {
  declaration: LineStep;
  stationKey: string;
  station: Station;
}

interface Edge {
  line: string;
  step: string;
  port: string;
  from: string;
  guarantees: string[];
  to: string;
  requires: string[];
}

type ResolutionStatus = "direct" | "convert" | "contract" | "no-converter" | "via-invalid";

export interface LineResolution extends Edge {
  status: ResolutionStatus;
  chain: string[];
  type: string;
  props: string[];
  missing: string[];
  lossless: boolean;
  detail: string;
}

interface Converter {
  id: string;
  key: string;
  station: Station;
  active: boolean;
  from: string;
  to: string;
  lossless: boolean;
  requires: string[];
  guarantees: string[];
  preserves: ReadonlySet<string>;
  cost: number;
  p50: number;
}

interface Path {
  chain: string[];
  type: string;
  props: ReadonlySet<string>;
  lossless: boolean;
  cost: number;
  p50: number;
}

interface PlannedEdge {
  input: Binding;
  resolution: LineResolution;
}

const UNSUPPORTED = ["subline", "switch", "map", "until"] as const;
const MAX_CHAIN = 4;
const MAX_PATHS = 100_000;
const TYPE_NAME = /^([a-z][a-z0-9]*(?:[.-][a-z0-9]+)*)@([0-9]+)(?:\.([0-9]+))?$/;
const TYPE_CONTAINER = /^(list|ref)<(.+)>$/;
const PORT_REF = /^([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$/;
const LINE_INPUT = /^\$inputs\.([a-z][a-z0-9_]*)$/;

function compare(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function invalid(line: string, message: string, detail: Record<string, unknown> = {}): never {
  throw new FactoryError("RUN_PLAN_INVALID", message, { line, ...detail });
}

class LineTypes {
  private readonly expressions = new Map<string, string>();
  private readonly types = new Map<string, { parent?: string; properties: string[] }>();
  private readonly alwaysByType = new Map<string, ReadonlySet<string>>();

  constructor(private readonly snapshot: Snapshot, private readonly line: string) {}

  normalize(text: string): string {
    const found = this.expressions.get(text);
    if (found !== undefined) return found;
    if (text === "any") return text;
    const composite = TYPE_CONTAINER.exec(text);
    if (composite) {
      const value = composite[1] + "<" + this.normalize(composite[2]) + ">";
      this.expressions.set(text, value);
      return value;
    }
    const named = TYPE_NAME.exec(text);
    if (!named || ["any", "list", "ref"].includes(named[1])) {
      return invalid(this.line, "产线引用了无效类型", { type: text });
    }
    const major = named[1] + "@" + Number(named[2]);
    try {
      if (named[3] !== undefined && this.snapshot.doc("Type:" + major + "." + Number(named[3])).metadata?.lifecycle === "retired") {
        return invalid(this.line, "产线引用了已退役的类型", { type: text });
      }
      this.type(major);
    } catch (error) {
      if (error instanceof FactoryError && ["SPEC_NOT_FOUND", "TYPE_NOT_FOUND"].includes(error.code)) {
        return invalid(this.line, "产线引用的类型不存在", { type: text });
      }
      throw error;
    }
    this.expressions.set(text, major);
    return major;
  }

  isSubtype(source: string, target: string): boolean {
    if (source === target || target === "any") return true;
    const left = TYPE_CONTAINER.exec(source);
    const right = TYPE_CONTAINER.exec(target);
    if (left && right && left[1] === right[1]) return this.isSubtype(left[2], right[2]);
    if (left || right || source === "any") return false;
    return this.ancestry(source).includes(target);
  }

  always(type: string): ReadonlySet<string> {
    const cached = this.alwaysByType.get(type);
    if (cached) return cached;
    const composite = TYPE_CONTAINER.exec(type);
    const properties = composite
      ? this.always(composite[2])
      : new Set(type === "any" ? [] : this.ancestry(type).flatMap((name) => this.type(name).properties));
    this.alwaysByType.set(type, properties);
    return properties;
  }

  private type(name: string): { parent?: string; properties: string[] } {
    const found = this.types.get(name);
    if (found) return found;
    const spec: { extends?: string; always?: string[] } = this.snapshot.spec(this.snapshot.typeKey(name));
    const value = { parent: spec.extends, properties: spec.always ?? [] };
    this.types.set(name, value);
    return value;
  }

  private ancestry(type: string): string[] {
    const visited = new Set<string>();
    let current: string | undefined = type;
    while (current) {
      if (visited.has(current)) return invalid(this.line, "类型继承有环", { type: current });
      visited.add(current);
      const parent: string | undefined = this.type(current).parent;
      current = parent === undefined ? undefined : this.normalize(parent);
      if (current && !TYPE_NAME.test(current)) return invalid(this.line, "继承目标须为命名类型", { type: current });
    }
    return [...visited];
  }
}

function stationSteps(snapshot: Snapshot, line: string, spec: LineSpec): Map<string, Step> {
  const steps = new Map<string, Step>();
  const declarations = Object.entries(spec.steps).sort(([a], [b]) => compare(a, b));
  for (const [step, declaration] of declarations) {
    const construct = UNSUPPORTED.find((key) => Object.hasOwn(declaration, key));
    if (construct) throw new FactoryError("LINE_UNSUPPORTED", "静态产线不支持该构造", { line, step, construct });
  }
  for (const [step, declaration] of declarations) {
    if (!declaration.station) return invalid(line, "步骤缺少工位", { step });
    const stationKey = snapshot.key("Station", declaration.station);
    const document = snapshot.doc(stationKey);
    if (document.metadata?.lifecycle === "retired") return invalid(line, "步骤引用了已退役的工位", { step, station: stationKey });
    steps.set(step, { declaration, stationKey, station: document.spec as Station });
  }
  return steps;
}

function converterLibrary(snapshot: Snapshot, types: LineTypes, line: string): Map<string, Converter> {
  const converters = new Map<string, Converter>();
  for (const key of snapshot.keys("Station")) {
    const document = snapshot.doc(key);
    const station = document.spec as Station;
    if (station.role !== "converter" || document.metadata?.lifecycle === "retired") continue;
    const input = station.inputs?.in;
    const output = station.outputs?.out;
    const converts = station.converts;
    if (!converts || !input || !output || output.optional
      || Object.keys(station.inputs ?? {}).length !== 1 || Object.keys(station.outputs ?? {}).length !== 1) {
      return invalid(line, "转换工位须定义唯一的 in 和 out 端口与转换契约", { station: key });
    }
    const from = types.normalize(converts.from);
    const to = types.normalize(converts.to);
    if (from !== types.normalize(input.type) || to !== types.normalize(output.type)) {
      return invalid(line, "转换工位端口与转换契约的类型不一致", { station: key });
    }
    const id = key.slice("Station:".length);
    converters.set(id, {
      id, key, station, from, to, active: (document.metadata?.lifecycle ?? "active") === "active",
      lossless: converts.lossless,
      requires: input.requires ?? [],
      guarantees: output.guarantees ?? [],
      preserves: new Set(converts.preserves),
      cost: station.cost?.usd_est ?? 0,
      p50: station.cost?.p50_s ?? 0,
    });
  }
  return converters;
}

function hasAll(props: ReadonlySet<string>, required: readonly string[]): boolean {
  return required.every((name) => props.has(name));
}

function convert(types: LineTypes, path: Path, converter: Converter): Path {
  return {
    chain: [...path.chain, converter.id],
    type: converter.to,
    props: new Set([
      ...types.always(converter.to),
      ...converter.guarantees,
      ...[...path.props].filter((property) => converter.preserves.has(property)),
    ]),
    lossless: path.lossless && converter.lossless,
    cost: path.cost + converter.cost,
    p50: path.p50 + converter.p50,
  };
}

function resolution(edge: Edge, path: Path, status: ResolutionStatus, detail = "", chain = path.chain): LineResolution {
  return {
    ...edge,
    status,
    chain,
    type: path.type,
    props: [...path.props].sort(compare),
    missing: [...new Set(edge.requires)].filter((property) => !path.props.has(property)).sort(compare),
    lossless: (status === "direct" || status === "convert") && path.lossless,
    detail,
  };
}

function comparePaths(left: Path, right: Path): number {
  const ranking = Number(right.lossless) - Number(left.lossless)
    || left.chain.length - right.chain.length
    || left.cost - right.cost
    || left.p50 - right.p50;
  if (ranking) return ranking;
  for (let index = 0; index < left.chain.length; index += 1) {
    const sequence = compare(left.chain[index], right.chain[index]);
    if (sequence) return sequence;
  }
  return 0;
}

function resolveVia(
  types: LineTypes,
  converters: Map<string, Converter>,
  edge: Edge,
  initial: Path,
  chain: string[],
): LineResolution {
  let path = initial;
  if (new Set(chain).size !== chain.length) return invalid(edge.line, "转换链包含重复工位", { step: edge.step, port: edge.port, chain });
  for (const id of chain) {
    const converter = converters.get(id);
    if (!converter) return resolution(edge, path, "via-invalid", "找不到可用的转换工位 " + id, chain);
    if (!types.isSubtype(path.type, converter.from)) {
      return resolution(edge, path, "via-invalid", "转换工位无法接收 " + path.type, chain);
    }
    if (!hasAll(path.props, converter.requires)) {
      return resolution(edge, path, "via-invalid", "可用属性未满足转换工位要求 " + id, chain);
    }
    path = convert(types, path, converter);
  }
  if (!types.isSubtype(path.type, edge.to)) {
    return resolution(edge, path, "via-invalid", "转换链终点与下游类型不兼容", chain);
  }
  return resolution(edge, path, hasAll(path.props, edge.requires) ? "convert" : "contract");
}

function resolveEdge(types: LineTypes, converters: Map<string, Converter>, edge: Edge, via?: Via): LineResolution {
  const initial = {
    chain: [], type: edge.from, props: new Set([...types.always(edge.from), ...edge.guarantees]),
    lossless: true, cost: 0, p50: 0,
  };
  if (Array.isArray(via)) return resolveVia(types, converters, edge, initial, via);
  const direct = types.isSubtype(edge.from, edge.to);
  if (direct && hasAll(initial.props, edge.requires)) return resolution(edge, initial, "direct");

  const candidates = via === "none" ? [] : [...converters.values()].filter((converter) => converter.active);
  let best: Path | undefined;
  let explored = 0;
  let frontier: Path[] = [initial];
  for (let depth = 0; depth < MAX_CHAIN && frontier.length; depth += 1) {
    const next: Path[] = [];
    for (const path of frontier) {
      for (const converter of candidates) {
        if (path.chain.includes(converter.id) || !types.isSubtype(path.type, converter.from) || !hasAll(path.props, converter.requires)) continue;
        if (++explored > MAX_PATHS) {
          throw new FactoryError("MRL-COST", "转换解析超过路径预算", { line: edge.line, step: edge.step, port: edge.port, limit: MAX_PATHS });
        }
        const converted = convert(types, path, converter);
        next.push(converted);
        if (types.isSubtype(converted.type, edge.to) && hasAll(converted.props, edge.requires)
          && (!best || comparePaths(converted, best) < 0)) best = converted;
      }
    }
    frontier = next;
  }
  return best ? resolution(edge, best, "convert") : resolution(edge, initial, direct ? "contract" : "no-converter");
}

function outputRef(line: string, source: string): PortPair {
  const match = PORT_REF.exec(source);
  if (!match) return invalid(line, "无法解析节点输出引用", { source });
  return [match[1], match[2]];
}

function outputPort(line: string, steps: Map<string, Step>, [node, port]: PortPair): Port {
  const output = steps.get(node)?.station.outputs;
  if (!output || !Object.hasOwn(output, port)) return invalid(line, "找不到来源节点的输出端口", { node, port });
  return output[port];
}

function resolveWiring(
  line: string,
  steps: Map<string, Step>,
  inputs: Record<string, LineInput>,
  types: LineTypes,
  converters: Map<string, Converter>,
): Map<string, PlannedEdge[]> {
  const wiring = new Map<string, PlannedEdge[]>();
  for (const [step, { declaration, station }] of steps) {
    const edges: PlannedEdge[] = [];
    const supplied = declaration.in ?? {};
    for (const [port, definition] of Object.entries(station.inputs ?? {})) {
      if (!definition.optional && !Object.hasOwn(supplied, port)) return invalid(line, "必填输入没有连接", { step, port });
    }
    for (const [port, binding] of Object.entries(supplied).sort(([a], [b]) => compare(a, b))) {
      const target = station.inputs;
      if (!target || !Object.hasOwn(target, port)) return invalid(line, "工位没有该输入端口", { step, port });
      const ref = typeof binding === "string" ? binding : binding.from;
      const via = typeof binding === "string" ? undefined : binding.via;
      const lineInput = LINE_INPUT.exec(ref)?.[1];
      let source: Port;
      let input: Binding;
      if (lineInput) {
        if (!Object.hasOwn(inputs, lineInput)) return invalid(line, "找不到产线接口输入", { step, port, source: lineInput });
        const entry = inputs[lineInput];
        source = { type: entry.type, guarantees: entry.requires };
        input = { port, source: "input", node: "", outPort: lineInput };
      } else {
        const pair = outputRef(line, ref);
        source = outputPort(line, steps, pair);
        input = { port, source: "node", node: pair[0], outPort: pair[1] };
      }
      const resolved = resolveEdge(types, converters, {
        line, step, port,
        from: types.normalize(source.type), guarantees: source.guarantees ?? [],
        to: types.normalize(target[port].type), requires: target[port].requires ?? [],
      }, via);
      if (resolved.status !== "direct" && resolved.status !== "convert") {
        return invalid(line, "产线输入边不能执行", { step, port, status: resolved.status, resolution: resolved });
      }
      edges.push({ input, resolution: resolved });
    }
    wiring.set(step, edges);
  }
  return wiring;
}

function makeNode(
  id: string,
  step: string,
  station: string,
  spec: Station,
  params: Record<string, Parameter>,
  inputs: Binding[],
  implicitResolution: LineResolution | null,
): PlanNode {
  return {
    id, step, station, params, inputs, resolution: implicitResolution,
    kind: implicitResolution ? "converter" : "station",
    role: spec.role,
    templated: Object.keys(params).filter((name) => typeof params[name] === "string" && params[name].includes("{{")).sort(compare),
    deps: [...new Set(inputs.filter((item) => item.source === "node").map((item) => item.node))].sort(compare),
    budgetUsd: spec.limits?.budget_usd ?? 0,
    implicit: implicitResolution !== null,
  };
}

function converterNodeId(step: string, port: string, converter: string): string {
  return step + ".in." + port + "~" + converter;
}

function expandNodes(
  line: string,
  steps: Map<string, Step>,
  wiring: Map<string, PlannedEdge[]>,
  converters: Map<string, Converter>,
): Map<string, PlanNode> {
  const nodes = new Map<string, PlanNode>();
  for (const [name, { declaration, stationKey, station }] of steps) {
    const inputs: Binding[] = [];
    for (const edge of wiring.get(name) ?? []) {
      let incoming = edge.input;
      for (const id of edge.resolution.chain) {
        const converter = converters.get(id)!;
        for (const [param, declaration] of Object.entries(converter.station.params ?? {})) {
          if (!Object.hasOwn(declaration, "default")) return invalid(line, "隐式转换工位缺少参数默认值", { station: converter.key, param });
        }
        const nodeId = converterNodeId(name, edge.input.port, id);
        nodes.set(nodeId, makeNode(nodeId, name, converter.key, converter.station, {}, [{ ...incoming, port: "in" }], edge.resolution));
        incoming = { port: edge.input.port, source: "node", node: nodeId, outPort: "out" };
      }
      inputs.push(incoming);
    }
    nodes.set(name, makeNode(name, name, stationKey, station, declaration.params ?? {}, inputs, null));
  }
  return nodes;
}

function topologicalOrder(line: string, nodes: Map<string, PlanNode>): PlanNode[] {
  const indegree = new Map<string, number>();
  const children = new Map([...nodes.keys()].map((id): [string, string[]] => [id, []]));
  for (const [id, node] of nodes) {
    indegree.set(id, node.deps.length);
    for (const dependency of node.deps) {
      const found = children.get(dependency);
      if (!found) return invalid(line, "运行计划节点依赖了不存在的节点", { node: id, dependency });
      found.push(id);
    }
  }
  const ordered: PlanNode[] = [];
  let ready = [...indegree].filter(([, size]) => size === 0).map(([id]) => id);
  while (ready.length) {
    const layer = ready.sort(compare);
    ready = [];
    for (const id of layer) {
      ordered.push(nodes.get(id)!);
      for (const child of children.get(id)!) {
        const remaining = indegree.get(child)! - 1;
        indegree.set(child, remaining);
        if (remaining === 0) ready.push(child);
      }
    }
  }
  if (ordered.length !== nodes.size) return invalid(line, "运行计划有环");
  return ordered;
}

// Static lines have a multiplicity of one. Project cost_item from the validated
// wiring and station declarations before comparing it with the expanded plan.
const budgetProgram = buildRuleProgram([{
  name: "static_line_budget",
  source: [
    "module static_line_budget.",
    "@input rel step_cost(step: string, usd: float).",
    "@input rel converter_cost(item: string, usd: float).",
    "rel cost_item(item: string, usd: float).",
    "cost_item(Item, Usd) :- step_cost(Item, Usd).",
    "cost_item(Item, Usd) :- converter_cost(Item, Usd).",
  ].join("\n"),
}]);

function reconcileBudget(
  line: string,
  nodes: PlanNode[],
  steps: Map<string, Step>,
  wiring: Map<string, PlannedEdge[]>,
  converters: Map<string, Converter>,
): BudgetRow[] {
  const stepCost: BudgetRow[] = [];
  const converterCost: BudgetRow[] = [];
  const budgetedStations = new Set<string>();
  for (const [step, { station, stationKey }] of steps) {
    const usd = station.limits?.budget_usd;
    if (usd !== undefined) {
      stepCost.push([step, usd]);
      budgetedStations.add(stationKey);
    }
    for (const edge of wiring.get(step) ?? []) {
      for (const id of edge.resolution.chain) {
        const converter = converters.get(id)!;
        const usd = converter.station.limits?.budget_usd;
        if (usd === undefined) continue;
        converterCost.push([converterNodeId(step, edge.input.port, id), usd]);
        budgetedStations.add(converter.key);
      }
    }
  }
  const items = nodes.filter((node) => budgetedStations.has(node.station))
    .map((node): BudgetRow => [node.id, node.budgetUsd]).sort(([left], [right]) => compare(left, right));
  const kernel = budgetProgram.evaluate({ step_cost: stepCost, converter_cost: converterCost }, 2 * (stepCost.length + converterCost.length))
    .rowsOf("cost_item").map(([name, usd]): BudgetRow => [String(name), Number(usd)])
    .sort(([left], [right]) => compare(left, right));
  if (items.length !== kernel.length || items.some(([id, usd], index) => id !== kernel[index][0] || usd !== kernel[index][1])) {
    return invalid(line, "预算组成与 cost_item 不一致", { got: items, kernel });
  }
  return items;
}

export function compileRunPlan(snapshot: Snapshot, lineRef: string): RunPlan {
  const line = snapshot.key("Line", lineRef);
  const document = snapshot.doc(line);
  if (document.metadata?.lifecycle === "retired") return invalid(line, "产线已退役");
  const spec = document.spec as LineSpec;
  const steps = stationSteps(snapshot, line, spec);
  const types = new LineTypes(snapshot, line);
  const converters = converterLibrary(snapshot, types, line);
  const inputs = spec.interface?.inputs ?? {};
  for (const input of Object.values(inputs)) types.normalize(input.type);
  const wiring = resolveWiring(line, steps, inputs, types, converters);
  const nodes = topologicalOrder(line, expandNodes(line, steps, wiring, converters));

  const outputs = Object.fromEntries(Object.entries(spec.interface?.outputs ?? {}).map(([name, output]) => {
    const reference = outputRef(line, output.from);
    const source = outputPort(line, steps, reference);
    if (output.type && !types.isSubtype(types.normalize(source.type), types.normalize(output.type))) {
      return invalid(line, "接口输出的声明类型无法接收来源类型", { output: name, type: output.type });
    }
    return [name, reference];
  }));
  const keep = (spec.keep ?? []).map((source) => {
    const pair = outputRef(line, source);
    outputPort(line, steps, pair);
    return pair;
  }).sort(([aNode, aPort], [bNode, bPort]) => compare(aNode, bNode) || compare(aPort, bPort));

  return new RunPlan({
    line, epoch: snapshot.epoch, nodes, outputs, keep,
    inputs: Object.fromEntries(Object.entries(inputs).map(([name, input]) => [name, input.type])),
    inputRequires: Object.fromEntries(Object.entries(inputs).map(([name, input]) => [name, input.requires ?? []])),
    budgetCapUsd: spec.budget?.cap_usd ?? null,
    budgetItems: reconcileBudget(line, nodes, steps, wiring, converters),
  });
}
