import { compileExpr, type EvalEnvironment } from "../../mrl/evaluator";
import { compileTemplate } from "../../mrl/template";
import { BOOL, DYN } from "../../mrl/types";
import { JobCache, cacheableRoles } from "./cache";
import { FactoryError } from "./errors";
import { terminalStates } from "./jobs";
import { compileRunPlan, type PlanNode } from "./runplan";
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
    const plan = compileRunPlan(this.runner.snapshot, line);
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
      if (!variant || !plan.nodes.some((node) => !node.implicit && node.step === step)) {
        problems.push({ port: step, bean: "", reason: "variant" });
      }
    }
    if (problems.length) throw new FactoryError("RUN_INPUT_INVALID", "产线来料检验未通过", { problems });
    return this.runs.create(plan, options);
  }

  tick(id: string): string[] {
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
      const failed = [...nodes.values()].find((node) => node.state === "FAILED");
      if (run.state === "RUNNING" && failed) {
        run = this.runs.transition(id, "FAILING", { error: { code: "NODE_FAILED", node: failed.nodeId } });
      }

      const submitted: string[] = [];
      if (run.state === "RUNNING") {
        for (const node of plan.nodes) {
          if (nodes.get(node.id)?.state !== "WAITING" || !node.deps.every((dep) => nodes.get(dep)?.state === "DONE")) continue;
          try {
            const inputs = this.nodeInputs(run, node, nodes);
            const params = this.nodeParams(run, node, plan.inputs);
            const material = cacheableRoles.has(node.role)
              ? this.cache.material(this.runner.snapshot, node.station, inputs, params, { variant: run.variants[node.step] })
              : undefined;
            if (material && run.useCache) {
              const cached = this.cache.lookup(material.key());
              if (cached) {
                this.runs.completeCachedNode(id, node.id, { params }, cached);
                continue;
              }
            }
            const row = this.runs.submitNode(id, node.id, { params, reservedUsd: node.budgetUsd }, () => {
              if (node.role === "sink" && run.sinkMode === "dry-run") {
                throw new FactoryError("SINK_DRY_RUN_UNSUPPORTED", "还不能执行发布工位的试运行", { run: id, node: node.id });
              }
              const job = this.runner.submit(node.station, inputs, params, runValue(run));
              if (material) this.cache.bind(job.id, material);
              return job;
            });
            nodes.set(node.id, row);
            if (row.jobId) submitted.push(row.jobId);
          } catch (error) {
            if (!(error instanceof FactoryError)) throw error;
            const problem = { code: error.code, message: error.message, detail: error.detail };
            if (error.code !== "RUN_BUDGET_EXHAUSTED") nodes.set(node.id, this.runs.failNode(id, node.id, problem));
            run = this.runs.transition(id, "FAILING", {
              error: error.code === "RUN_BUDGET_EXHAUSTED" ? problem : { code: "NODE_FAILED", node: node.id, cause: problem },
            });
            break;
          }
        }
      }

      const active = [...nodes.values()].some((node) => node.state === "SUBMITTED");
      if (!active && (run.state === "FAILING" || run.state === "CANCELLING")) {
        this.runs.transition(id, run.state === "FAILING" ? "FAILED" : "CANCELLED");
      } else if (run.state === "RUNNING" && [...nodes.values()].every((node) => node.state === "DONE")) {
        const outputs = Object.fromEntries(Object.entries(plan.outputs).map(([name, [node, port]]) => [name, nodes.get(node)?.outputs[port] ?? []]));
        this.runs.transition(id, "SUCCEEDED", { outputs });
      }
      return submitted.sort();
    }).immediate();
  }

  async run(id: string): Promise<Run> {
    const maxTicks = this.runs.plan(id).nodes.length * 2 + 2;
    for (let tick = 0; tick < maxTicks; tick += 1) {
      this.tick(id);
      const run = this.runs.get(id);
      if (runTerminalStates.has(run.state)) return run;
      const jobs = this.runs.nodes(id).filter((node) => node.state === "SUBMITTED" && node.jobId).map((node) => node.jobId!).sort();
      let waiting = 0;
      for (const job of jobs) if ((await this.runner.run(job)).state === "AWAITING_APPROVAL") waiting += 1;
      if (waiting > 0 && waiting === jobs.length) return this.runs.get(id);
    }
    throw new FactoryError("RUN_STUCK", "产线未能在计划步数内完成", { run: id, state: this.runs.get(id).state });
  }

  private nodeInputs(run: Run, node: PlanNode, nodes: Map<string, RunNode>): Record<string, string | string[]> {
    const station = this.runner.snapshot.spec(node.station);
    const inputs: Record<string, string | string[]> = Object.create(null);
    for (const binding of node.inputs) {
      const values = binding.source === "input" ? run.inputs[binding.outPort] : nodes.get(binding.node)?.outputs[binding.outPort];
      const declaration = station.inputs[binding.port];
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
