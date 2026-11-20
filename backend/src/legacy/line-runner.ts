import { compileExpr, type EvalEnvironment } from "../../mrl/evaluator";
import { compileTemplate } from "../../mrl/template";
import { BOOL, DYN } from "../../mrl/types";
import { JobCache, cacheableRoles } from "./cache";
import { compileDynamicRunPlan, selectSwitchTarget, switchTargets } from "./dynamic-switch";
import { restoreSubline } from "./dynamic-subline";
import { untilHolds, untilIteration } from "./dynamic-until";
import { FactoryError } from "./errors";
import { terminalStates } from "./jobs";
import type { PlanNode, RunPlan } from "./runplan";
import { RunStore, runTerminalStates, type Run, type RunNode } from "./runs";
import { inputsValue, propertyEnv } from "./values";
import type { Runner } from "./runner";

export class LineRunner {
  readonly runs: RunStore;
  readonly cache: JobCache;

  constructor(readonly runner: Runner) {
    const { db, clock, ids } = runner.context;
    this.runs = new RunStore(db, clock, ids);
    this.cache = new JobCache(runner.context, runner.jobs);
  }

  start(line: string, options: Parameters<RunStore["create"]>[1]): Run {
    return this.startPlan(compileDynamicRunPlan(this.runner.snapshot, line), options);
  }

  private startPlan(plan: RunPlan, options: Parameters<RunStore["create"]>[1]): Run {
    const problems: Array<{ port: string; bean: string; reason: string }> = [];
    for (const [port, type] of Object.entries(plan.inputs)) {
      const ids = options.inputs[port];
      if (ids === undefined) continue; // RunStore checks interface shape.
      const expected = type.replace(/^list<(.+)>$/, "$1");
      for (const id of typeof ids === "string" ? [ids] : ids) {
        try {
          const bean = this.runner.context.warehouse.get(id);
          const concrete = bean.type.replace(/(@\d+)\.\d+$/, "$1");
          if (!bean.sealed || bean.expiredAt || (expected !== "any" && !this.runner.snapshot.isSubtype(concrete, expected))) {
            problems.push({ port, bean: id, reason: "material" });
            continue;
          }
          for (const name of plan.inputRequires[port] ?? []) {
            const expression = this.runner.snapshot.properties(concrete)[name];
            const ok = typeof expression === "string" && compileExpr(expression, { environment: { bean: DYN }, expect: BOOL })
              .evaluate(propertyEnv(bean) as EvalEnvironment) === true;
            if (!ok) problems.push({ port, bean: id, reason: name });
          }
        } catch (error) {
          problems.push({ port, bean: id, reason: error instanceof FactoryError ? error.code : "property" });
        }
      }
    }
    for (const [step, variant] of Object.entries(options.variants ?? {})) {
      if (!variant || !plan.nodes.some((node) => (node.kind === "station" || node.kind === "until") && node.step === step)) {
        problems.push({ port: step, bean: "", reason: "variant" });
      }
    }
    if (problems.length) throw new FactoryError("RUN_INPUT_INVALID", "产线来料检验未通过", { problems });
    return this.runs.create(plan, options);
  }

  tick(id: string, dispatch?: (jobIds: readonly string[]) => void): string[] {
    return this.runner.context.db.transaction(() => {
      let run = this.runs.get(id);
      if (runTerminalStates.has(run.state)) return [];
      if (run.epoch !== this.runner.snapshot.epoch) {
        throw new FactoryError("EPOCH_MISMATCH", "运行计划与当前快照不符", { run: id, run_epoch: run.epoch, snapshot_epoch: this.runner.snapshot.epoch });
      }
      const plan = this.runs.plan(id);
      if (run.state === "PENDING") run = this.runs.transition(id, "RUNNING");
      const nodes = new Map(this.runs.nodes(id).map((node) => [node.nodeId, node]));
      for (const node of nodes.values()) {
        if (node.state !== "SUBMITTED" || !node.jobId) continue;
        const job = this.runner.jobs.get(node.jobId);
        if (terminalStates.has(job.state)) nodes.set(node.nodeId, this.runs.settleNode(id, node.nodeId, job));
      }
      const submitted: string[] = [];
      const descendantJobs: string[] = [];
      let activeChild = false;
      for (const planned of plan.nodes) {
        if (planned.kind !== "subline") continue;
        const node = nodes.get(planned.id);
        if (node?.state === "WAITING" && typeof node.params?.child_run === "string") {
          activeChild = this.advanceChild(run, node, nodes, submitted, descendantJobs) || activeChild;
        }
      }
      const failed = [...nodes.values()].find((node) => node.state === "FAILED");
      if (run.state === "RUNNING" && failed) {
        run = this.runs.transition(id, "FAILING", { error: { code: "NODE_FAILED", node: failed.nodeId } });
      }

      if (run.state === "RUNNING") {
        const dynamic = plan.nodes.some((node) => node.kind === "switch");
        for (const planned of plan.nodes) {
          if (nodes.get(planned.id)?.state !== "WAITING") continue;
          if (planned.kind === "subline" && nodes.get(planned.id)?.params?.child_run) continue;
          try {
            if (dynamic) {
              const readiness = this.dynamicReadiness(plan, planned, nodes);
              if (readiness === "waiting") continue;
              if (readiness === "skipped") {
                nodes.set(planned.id, this.runs.skipNode(id, planned.id));
                continue;
              }
            } else if (!planned.deps.every((dep) => nodes.get(dep)?.state === "DONE")) continue;
            if (planned.kind === "switch") {
              if (!planned.switch) throw new FactoryError("RUN_PLAN_INVALID", "运行计划缺少 switch 定义", { step: planned.step });
              const on = planned.switch.on;
              const ids = on.source === "input" ? run.inputs[on.port] : nodes.get(on.node)?.outputs[on.port];
              const selected = selectSwitchTarget(planned, (ids ?? []).map((bean) => this.runner.context.warehouse.get(bean)));
              if (!plan.nodes.some((target) => target.id === selected)) {
                throw new FactoryError("RUN_PLAN_INVALID", "switch 选中的步骤不在计划里", { step: planned.step, selected });
              }
              nodes.set(planned.id, this.runs.completeSwitch(id, planned.id, selected));
              continue;
            }
            if (planned.kind === "subline") {
              if (!planned.subline) throw new FactoryError("RUN_PLAN_INVALID", "运行计划缺少子产线定义", { step: planned.step });
              const childPlan = restoreSubline(planned.subline, run.epoch, planned.step);
              const childInputs = this.nodeInputs(run, planned, nodes, Object.fromEntries(Object.entries(childPlan.inputs).map(([port, type]) => [port, { type }])));
              const link = this.runs.attachSubline(id, planned.id, childPlan.budgetCapUsd ?? 0, () => this.startPlan(childPlan, {
                inputs: childInputs, useCache: run.useCache, sinkMode: run.sinkMode,
                trigger: { on: "subline", parent: id, step: planned.id },
              }));
              nodes.set(planned.id, link);
              activeChild = this.advanceChild(run, link, nodes, submitted, descendantJobs) || activeChild;
              if (nodes.get(planned.id)?.state === "FAILED") {
                run = this.runs.transition(id, "FAILING", { error: { code: "NODE_FAILED", node: planned.id } });
                break;
              }
              continue;
            }
            const node = planned.kind === "until" ? this.nextIteration(id, planned, nodes) : planned;
            if (!node) continue;
            const inputs = this.nodeInputs(run, node, nodes);
            const params = this.nodeParams(run, node, plan.inputs);
            const material = planned.kind !== "until" && cacheableRoles.has(node.role)
              ? this.cache.material(this.runner.snapshot, node.station, inputs, params, { variant: run.variants[node.step] })
              : undefined;
            if (material && run.useCache) {
              const cached = this.cache.lookup(material.key());
              if (cached) {
                nodes.set(node.id, this.runs.completeCachedNode(id, node.id, { params }, cached));
                continue;
              }
            }
            const row = this.runs.submitNode(id, node.id, { params, reservedUsd: node.budgetUsd, reserveFrom: planned.until ? planned.id : undefined }, () => {
              const job = this.runner.submit(node.station, inputs, params, runValue(run), {
                proposalOnly: node.role === "sink" && run.sinkMode === "dry-run",
              });
              if (material) this.cache.bind(job.id, material);
              return job;
            });
            nodes.set(node.id, row);
            if (row.jobId) submitted.push(row.jobId);
          } catch (error) {
            if (!(error instanceof FactoryError)) throw error;
            const problem = { code: error.code, message: error.message, detail: error.detail };
            if (error.code !== "RUN_BUDGET_EXHAUSTED") nodes.set(planned.id, this.runs.failNode(id, planned.id, problem));
            run = this.runs.transition(id, "FAILING", {
              error: error.code === "RUN_BUDGET_EXHAUSTED" ? problem : { code: "NODE_FAILED", node: planned.id, cause: problem },
            });
            break;
          }
        }
      }

      const active = activeChild || [...nodes.values()].some((node) => node.state === "SUBMITTED");
      if (!active && (run.state === "FAILING" || run.state === "CANCELLING")) {
        this.runs.transition(id, run.state === "FAILING" ? "FAILED" : "CANCELLED");
      } else if (run.state === "RUNNING" && [...nodes.values()].every((node) => node.state === "DONE" || node.state === "SKIPPED")) {
        const outputs = Object.fromEntries(Object.entries(plan.outputs).map(([name, [node, port]]) => [name, nodes.get(node)?.outputs[port] ?? []]));
        this.runs.transition(id, "SUCCEEDED", { outputs });
      }
      if (dispatch) {
        const pending = [...descendantJobs, ...[...nodes.values()].flatMap((node) => node.state === "SUBMITTED" && node.jobId ? [node.jobId] : [])].sort();
        if (pending.length) dispatch(pending);
      }
      return submitted.sort();
    }).immediate();
  }

  private advanceChild(parent: Run, node: RunNode, nodes: Map<string, RunNode>, submitted: string[], pending: string[]): boolean {
    const childId = String(node.params?.child_run);
    let child = this.runs.get(childId);
    if (child.epoch !== parent.epoch || child.trigger.parent !== parent.id || child.trigger.step !== node.nodeId) {
      throw new FactoryError("RUN_PLAN_INVALID", "子产线的持久化身份不匹配", { run: parent.id, node: node.nodeId, child: childId });
    }
    if (!runTerminalStates.has(child.state)) {
      if (parent.state === "CANCELLING" && (child.state === "PENDING" || child.state === "RUNNING")) this.runs.cancel(childId, "parent:" + parent.id);
      submitted.push(...this.tick(childId, (jobs) => pending.push(...jobs)));
      child = this.runs.get(childId);
    }
    if (!runTerminalStates.has(child.state)) return true;
    nodes.set(node.nodeId, this.runs.settleSubline(parent.id, node.nodeId, childId));
    return false;
  }

  private nextIteration(id: string, node: PlanNode, nodes: Map<string, RunNode>): PlanNode | undefined {
    const definition = node.until;
    if (!definition) throw new FactoryError("RUN_PLAN_INVALID", "运行计划缺少 until 定义", { step: node.step });
    let attempt = 1;
    while (attempt < definition.maxIter && nodes.has(untilIteration(node, attempt + 1).id)) attempt++;
    let iteration = untilIteration(node, attempt);
    const stored = nodes.get(iteration.id);
    if (stored?.state === "DONE") {
      const { context, snapshot } = this.runner;
      if (untilHolds(snapshot, context.warehouse, node.station, node.step, definition, stored, attempt)) {
        nodes.set(node.id, this.runs.completeUntil(id, node.id, stored.outputs));
        return;
      }
      if (attempt === definition.maxIter) {
        throw new FactoryError("UNTIL_EXHAUSTED", "达到最大次数时条件仍不成立", { step: node.step, max_iter: definition.maxIter });
      }
      attempt++;
      iteration = untilIteration(node, attempt);
    } else if (stored && stored.state !== "WAITING") return;
    this.runs.prepareIteration(id, node.id, iteration.id, (definition.maxIter - attempt + 1) * node.budgetUsd);
    nodes.set(iteration.id, this.runs.node(id, iteration.id));
    return iteration;
  }

  private dynamicReadiness(plan: RunPlan, node: PlanNode, nodes: Map<string, RunNode>): "ready" | "waiting" | "skipped" {
    const controls = node.deps.filter((dependency) => {
      const definition = plan.node(dependency).switch;
      return definition && switchTargets(definition).includes(node.step);
    });
    if (controls.some((id) => !["DONE", "SKIPPED"].includes(nodes.get(id)?.state ?? ""))) return "waiting";
    if (controls.length && !controls.some((id) => nodes.get(id)?.params?.selected === node.step)) return "skipped";
    const data = node.deps.filter((id) => !controls.includes(id));
    if (data.some((id) => !["DONE", "SKIPPED"].includes(nodes.get(id)?.state ?? ""))) return "waiting";
    if (data.length && data.every((id) => nodes.get(id)?.state === "SKIPPED") && !node.inputs.some(({ source }) => source === "input")) return "skipped";
    return "ready";
  }

  async run(id: string): Promise<Run> {
    const maxTicks = this.tickLimit(this.runs.plan(id));
    for (let tick = 0; tick < maxTicks; tick += 1) {
      const jobs: string[] = [];
      this.tick(id, (pending) => jobs.push(...pending));
      const run = this.runs.get(id);
      if (runTerminalStates.has(run.state)) return run;
      let waiting = 0;
      for (const job of jobs) if ((await this.runner.run(job)).state === "AWAITING_APPROVAL") waiting += 1;
      if (waiting > 0 && waiting === jobs.length) return this.runs.get(id);
    }
    throw new FactoryError("RUN_STUCK", "产线未能在计划步数内完成", { run: id, state: this.runs.get(id).state });
  }

  private tickLimit(plan: RunPlan): number {
    return plan.nodes.reduce((total, node) => total + 2 * (node.until?.maxIter ?? 1)
      + (node.subline ? this.tickLimit(restoreSubline(node.subline, plan.epoch, node.step)) : 0), 2);
  }

  private nodeInputs(run: Run, node: PlanNode, nodes: Map<string, RunNode>, declarations = this.runner.snapshot.spec(node.station).inputs): Record<string, string | string[]> {
    const inputs: Record<string, string | string[]> = Object.create(null);
    for (const binding of node.inputs) {
      const values = binding.source === "input" ? run.inputs[binding.outPort] : nodes.get(binding.node)?.outputs[binding.outPort];
      const declaration = declarations[binding.port];
      const list = declaration.type.startsWith("list<");
      if (!values?.length && declaration.optional) continue;
      if (!values?.length || (!list && values.length !== 1)) {
        throw new FactoryError("INPUT_MISSING", "产线节点没有可用输入", { node: node.id, port: binding.port });
      }
      inputs[binding.port] = list ? values : values[0];
    }
    return inputs;
  }

  private nodeParams(run: Run, node: PlanNode, ports: Readonly<Record<string, string>>): Record<string, unknown> {
    if (!node.templated.length) return { ...node.params };
    const materials = Object.fromEntries(Object.entries(run.inputs).map(([name, ids]) => {
      const beans = ids.map((id) => this.runner.context.warehouse.get(id));
      return [name, ports[name].startsWith("list<") ? beans : beans[0]];
    }));
    const values = {
      run: runValue(run),
      inputs: inputsValue(materials),
    };
    const params = { ...node.params };
    for (const name of node.templated) {
      try {
        params[name] = compileTemplate(String(params[name]), { environment: { run: DYN, inputs: DYN } }).render(values as EvalEnvironment);
      } catch (error) {
        throw new FactoryError("PARAM_INVALID", "产线节点模板渲染失败", { node: node.id, param: name, reason: String(error) });
      }
    }
    return params;
  }
}

function runValue(run: Run) {
  return { id: run.id, line: run.line, epoch: run.epoch, budget: {
    cap_usd: run.budgetCapUsd, spent_usd: run.spentUsd, remaining_usd: run.budgetCapUsd - run.spentUsd,
  } };
}
