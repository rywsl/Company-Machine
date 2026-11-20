import type { Database } from "bun:sqlite";
import { FactoryError } from "./errors";
import { canonicalJson, digestBytes, utcIso, type Clock, type IdSource } from "./foundation";
import type { Job } from "./jobs";
import { RunPlan } from "./runplan";

type JsonObject = Record<string, unknown>;
type PortIds = Record<string, string[]>;
type SinkMode = "execute" | "dry-run";
export type RunState = "PENDING" | "RUNNING" | "FAILING" | "CANCELLING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
export type NodeState = "WAITING" | "SUBMITTED" | "DONE" | "FAILED" | "SKIPPED";

export const runTerminalStates: ReadonlySet<RunState> = new Set(["SUCCEEDED", "FAILED", "CANCELLED"]);
const edges: Record<RunState, RunState[]> = {
  PENDING: ["RUNNING", "CANCELLING"],
  RUNNING: ["FAILING", "CANCELLING", "SUCCEEDED"],
  FAILING: ["FAILED", "RUNNING"],
  CANCELLING: ["CANCELLED"],
  SUCCEEDED: [],
  FAILED: ["RUNNING"],
  CANCELLED: [],
};

export interface Run {
  id: string;
  line: string;
  epoch: string;
  state: RunState;
  planDigest: string;
  inputs: PortIds;
  variants: Record<string, string>;
  budgetCapUsd: number;
  spentUsd: number;
  reservedUsd: number;
  outputs: PortIds;
  error: JsonObject | null;
  trigger: JsonObject;
  rebuiltFrom: string | null;
  useCache: boolean;
  sinkMode: SinkMode;
  createdAt: string;
  updatedAt: string;
}

export interface RunNode {
  runId: string;
  nodeId: string;
  state: NodeState;
  jobId: string | null;
  cacheHit: boolean;
  outputs: PortIds;
  params: JsonObject | null;
  reservedUsd: number;
  error: JsonObject | null;
  updatedAt: string;
}

export interface RunEvent {
  seq: number;
  id: string;
  runId: string;
  kind: string;
  at: string;
  data: JsonObject;
}

interface RunOptions {
  inputs: Record<string, string | readonly string[]>;
  variants?: Record<string, string>;
  budgetCapUsd?: number;
  trigger?: JsonObject;
  rebuiltFrom?: string;
  useCache?: boolean;
  sinkMode?: SinkMode;
}

type RunPatch = Partial<Pick<Run, "error" | "outputs">> & { reason?: string };
type RunRow = {
  id: string; line: string; epoch: string; state: RunState; plan_digest: string;
  inputs: string; variants: string; budget_cap_usd: number; spent_usd: number; reserved_usd: number;
  outputs: string; error: string | null; trigger: string; rebuilt_from: string | null; use_cache: number;
  sink_mode: SinkMode; created_at: string; updated_at: string;
};
type NodeRow = {
  run_id: string; node_id: string; state: NodeState; job_id: string | null; cache_hit: number;
  outputs: string; params: string | null; reserved_usd: number; error: string | null; updated_at: string;
};

function decodeRun(row: RunRow): Run {
  return {
    id: row.id, line: row.line, epoch: row.epoch, state: row.state, planDigest: row.plan_digest,
    inputs: JSON.parse(row.inputs), variants: JSON.parse(row.variants), budgetCapUsd: row.budget_cap_usd,
    spentUsd: row.spent_usd, reservedUsd: row.reserved_usd, outputs: JSON.parse(row.outputs),
    error: row.error === null ? null : JSON.parse(row.error), trigger: JSON.parse(row.trigger),
    rebuiltFrom: row.rebuilt_from, useCache: Boolean(row.use_cache), sinkMode: row.sink_mode,
    createdAt: row.created_at, updatedAt: row.updated_at,
  };
}

function decodeNode(row: NodeRow): RunNode {
  return {
    runId: row.run_id, nodeId: row.node_id, state: row.state, jobId: row.job_id, cacheHit: Boolean(row.cache_hit),
    outputs: JSON.parse(row.outputs), params: row.params === null ? null : JSON.parse(row.params), reservedUsd: row.reserved_usd,
    error: row.error === null ? null : JSON.parse(row.error), updatedAt: row.updated_at,
  };
}

function budgetCap(plan: RunPlan, given: number | undefined): number {
  if (given !== undefined && (!Number.isFinite(given) || given <= 0 || (plan.budgetCapUsd !== null && given > plan.budgetCapUsd))) {
    throw new FactoryError("RUN_BUDGET_INVALID", "run 预算必须大于 0 且在产线上限内", { budgetUsd: given, capUsd: plan.budgetCapUsd });
  }
  const cap = given ?? plan.budgetCapUsd;
  if (cap === null) throw new FactoryError("RUN_BUDGET_REQUIRED", "产线需要预算上限", { line: plan.line });
  if (!Number.isFinite(cap) || cap < 0) throw new FactoryError("RUN_BUDGET_INVALID", "产线预算不合法", { capUsd: cap });
  return cap;
}

function normalizeInputs(plan: RunPlan, provided: RunOptions["inputs"]): PortIds {
  const inputs = Object.fromEntries(Object.entries(provided).map(([port, ids]) => [port, typeof ids === "string" ? [ids] : ids]));
  const problems: Array<{ port: string; reason: string }> = [];
  for (const [port, type] of Object.entries(plan.inputs)) {
    if (!Object.hasOwn(inputs, port)) problems.push({ port, reason: "missing" });
    else if (!Array.isArray(inputs[port]) || !inputs[port].length || (!type.startsWith("list<") && inputs[port].length !== 1)
      || inputs[port].some((id) => typeof id !== "string" || !id)) problems.push({ port, reason: "cardinality" });
  }
  for (const port of Object.keys(inputs)) {
    if (!Object.hasOwn(plan.inputs, port)) problems.push({ port, reason: "extra" });
  }
  if (problems.length) throw new FactoryError("RUN_INPUT_INVALID", "产线输入不合法", { problems });
  return Object.fromEntries(Object.entries(inputs).map(([port, ids]) => [port, [...ids]]));
}

export class RunStore {
  constructor(readonly db: Database, readonly clock: Clock, readonly ids: IdSource) {
    db.transaction(() => {
      db.exec(`
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, line TEXT NOT NULL, epoch TEXT NOT NULL, state TEXT NOT NULL,
          plan TEXT NOT NULL, plan_digest TEXT NOT NULL, inputs TEXT NOT NULL, variants TEXT NOT NULL DEFAULT '{}',
          budget_cap_usd REAL NOT NULL, spent_usd REAL NOT NULL DEFAULT 0, reserved_usd REAL NOT NULL DEFAULT 0,
          outputs TEXT NOT NULL DEFAULT '{}', error TEXT, trigger TEXT NOT NULL, rebuilt_from TEXT REFERENCES runs(id),
          use_cache INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          sink_mode TEXT NOT NULL DEFAULT 'execute'
        );
        CREATE INDEX IF NOT EXISTS runs_state_id ON runs(state, id);
        CREATE TABLE IF NOT EXISTS run_nodes (
          run_id TEXT NOT NULL REFERENCES runs(id), node_id TEXT NOT NULL, ord INTEGER NOT NULL,
          state TEXT NOT NULL, job_id TEXT, cache_hit INTEGER NOT NULL DEFAULT 0, outputs TEXT NOT NULL DEFAULT '{}',
          params TEXT, reserved_usd REAL NOT NULL DEFAULT 0, error TEXT, updated_at TEXT NOT NULL,
          PRIMARY KEY (run_id, node_id)
        );
        CREATE INDEX IF NOT EXISTS run_nodes_order ON run_nodes(run_id, ord);
        CREATE INDEX IF NOT EXISTS run_nodes_state ON run_nodes(run_id, state);
        CREATE TABLE IF NOT EXISTS run_node_jobs (
          run_id TEXT NOT NULL, node_id TEXT NOT NULL, job_id TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY (run_id, node_id, job_id)
        );
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, at TEXT NOT NULL,
          kind TEXT NOT NULL, job_id TEXT, data TEXT NOT NULL, run_id TEXT
        );
      `);
      const hasColumn = (table: "runs" | "events", name: string) =>
        db.query<{ name: string }>("PRAGMA table_info(" + table + ")").all().some((column) => column.name === name);
      if (!hasColumn("runs", "sink_mode")) db.exec("ALTER TABLE runs ADD COLUMN sink_mode TEXT NOT NULL DEFAULT 'execute'");
      if (!hasColumn("events", "run_id")) db.exec("ALTER TABLE events ADD COLUMN run_id TEXT");
      db.exec("CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq)");
    }).immediate();
  }

  create(plan: RunPlan, options: RunOptions): Run {
    const cap = budgetCap(plan, options.budgetCapUsd);
    const inputs = normalizeInputs(plan, options.inputs);
    const variants = options.variants ?? {};
    const steps = new Set(plan.nodes.filter((node) => !node.implicit).map((node) => node.step));
    for (const [step, variant] of Object.entries(variants)) {
      if (!steps.has(step) || typeof variant !== "string" || !variant) {
        throw new FactoryError("RUN_INPUT_INVALID", "产线 variant 不合法", { step });
      }
    }
    const sinkMode = options.sinkMode ?? "execute";
    if (sinkMode !== "execute" && sinkMode !== "dry-run") throw new FactoryError("RUN_INPUT_INVALID", "sinkMode 不合法", { sinkMode });
    const trigger = options.trigger ?? { on: "manual", by: "" };
    const wire = canonicalJson(plan.toJSON());
    const digest = digestBytes(new TextEncoder().encode(wire));
    return this.db.transaction(() => {
      const id = this.ids.new("r");
      const now = this.now();
      if (options.rebuiltFrom) this.get(options.rebuiltFrom);
      this.db.query(`
        INSERT INTO runs (id, line, epoch, state, plan, plan_digest, inputs, variants,
                          budget_cap_usd, trigger, rebuilt_from, use_cache, created_at, updated_at, sink_mode)
        VALUES (?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `).run(id, plan.line, plan.epoch, wire, digest, canonicalJson(inputs), canonicalJson(variants),
        cap, canonicalJson(trigger), options.rebuiltFrom ?? null, Number(options.useCache ?? true), now, now, sinkMode);
      const insert = this.db.query(`
        INSERT INTO run_nodes (run_id, node_id, ord, state, updated_at)
        VALUES (?, ?, ?, 'WAITING', ?)
      `);
      plan.nodes.forEach((node, ordinal) => insert.run(id, node.id, ordinal, now));
      this.event(id, "run.created", {
        line: plan.line, epoch: plan.epoch, plan_digest: digest, inputs, budget_cap_usd: cap, trigger,
      }, now);
      return this.get(id);
    }).immediate();
  }

  get(id: string): Run {
    const row = this.db.query<RunRow>("SELECT * FROM runs WHERE id = ?").get(id);
    if (!row) throw new FactoryError("RUN_NOT_FOUND", "找不到 run " + id, { runId: id });
    return decodeRun(row);
  }

  list(state?: RunState): Run[] {
    const rows = state === undefined
      ? this.db.query<RunRow>("SELECT * FROM runs ORDER BY id").all()
      : this.db.query<RunRow>("SELECT * FROM runs WHERE state = ? ORDER BY id").all(state);
    return rows.map(decodeRun);
  }

  plan(id: string): RunPlan {
    const row = this.db.query<{ line: string; epoch: string; plan: string; plan_digest: string }>(
      "SELECT line, epoch, plan, plan_digest FROM runs WHERE id = ?",
    ).get(id);
    if (!row) throw new FactoryError("RUN_NOT_FOUND", "找不到 run " + id, { runId: id });
    const digest = digestBytes(new TextEncoder().encode(row.plan));
    try {
      const plan = RunPlan.fromJSON(JSON.parse(row.plan));
      if (digest !== row.plan_digest || plan.line !== row.line || plan.epoch !== row.epoch) throw new Error("计划摘要或标识不匹配");
      return plan;
    } catch (error) {
      throw new FactoryError("RUN_PLAN_INVALID", "无法恢复 run 的持久化计划", { runId: id, reason: String(error) });
    }
  }

  nodes(id: string): RunNode[] {
    this.get(id);
    return this.db.query<NodeRow>("SELECT * FROM run_nodes WHERE run_id = ? ORDER BY ord").all(id).map(decodeNode);
  }

  node(runId: string, nodeId: string): RunNode {
    const row = this.db.query<NodeRow>("SELECT * FROM run_nodes WHERE run_id = ? AND node_id = ?").get(runId, nodeId);
    if (!row) throw new FactoryError("NODE_NOT_FOUND", "找不到 run 节点", { runId, nodeId });
    return decodeNode(row);
  }

  events(id: string, afterSeq = 0): RunEvent[] {
    this.get(id);
    const rows = this.db.query<{ seq: number; id: string; kind: string; at: string; data: string }>(
      "SELECT seq, id, kind, at, data FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
    ).all(id, afterSeq);
    return rows.map(({ data, ...row }) => ({ ...row, runId: id, data: JSON.parse(data) }));
  }

  jobHistory(runId: string, nodeId: string): string[] {
    this.node(runId, nodeId);
    return this.db.query<{ job_id: string }>(
      "SELECT job_id FROM run_node_jobs WHERE run_id = ? AND node_id = ? ORDER BY rowid",
    ).all(runId, nodeId).map((row) => row.job_id);
  }

  transition(id: string, state: RunState, patch: RunPatch = {}): Run {
    return this.db.transaction(() => {
      const run = this.get(id);
      if (run.state === "FAILED" || (run.state === "FAILING" && state === "RUNNING")) {
        this.invalidState(run, "transition");
      }
      return this.setRun(run, state, patch, this.now());
    }).immediate();
  }

  submitNode(
    runId: string, nodeId: string, options: { params: JsonObject; reservedUsd: number }, createJob: () => { id: string },
  ): RunNode {
    return this.db.transaction(() => {
      const run = this.get(runId);
      const node = this.node(runId, nodeId);
      const need = options.reservedUsd;
      if (run.state !== "RUNNING" || node.state !== "WAITING") this.invalidState(run, "submit", node);
      if (!Number.isFinite(need) || need < 0) throw new FactoryError("RUN_BUDGET_INVALID", "节点预算不合法", { runId, nodeId, need });
      const projected = run.spentUsd + run.reservedUsd + need;
      const rounding = Number.EPSILON * Math.max(Math.abs(projected), Math.abs(run.budgetCapUsd)) * 4;
      if (!Number.isFinite(projected) || projected - run.budgetCapUsd > rounding) {
        throw new FactoryError("RUN_BUDGET_EXHAUSTED", "预留会超出 run 预算", {
          runId, node: nodeId, spent: run.spentUsd, reserved: run.reservedUsd, need, cap: run.budgetCapUsd,
        });
      }
      const params = canonicalJson(options.params);
      const { id: jobId } = createJob();
      const now = this.now();
      this.db.query(`
        UPDATE run_nodes SET state = 'SUBMITTED', job_id = ?, params = ?, reserved_usd = ?, updated_at = ?
        WHERE run_id = ? AND node_id = ?
      `).run(jobId, params, need, now, runId, nodeId);
      this.db.query("INSERT INTO run_node_jobs (run_id, node_id, job_id, created_at) VALUES (?, ?, ?, ?)").run(runId, nodeId, jobId, now);
      this.db.query("UPDATE runs SET reserved_usd = reserved_usd + ?, updated_at = ? WHERE id = ?").run(need, now, runId);
      this.nodeEvent(node, "SUBMITTED", jobId, now);
      this.budgetEvent(run, nodeId, need, 0, now);
      return this.node(runId, nodeId);
    }).immediate();
  }

  completeCachedNode(
    runId: string, nodeId: string, options: { params: JsonObject }, job: Pick<Job, "id" | "state" | "outputs">,
  ): RunNode {
    return this.db.transaction(() => {
      const run = this.get(runId);
      const node = this.node(runId, nodeId);
      if (job.state === "SEALED" && node.state === "DONE" && node.cacheHit && node.jobId === job.id) return node;
      if (run.state !== "RUNNING" || node.state !== "WAITING" || job.state !== "SEALED") {
        this.invalidState(run, "complete_cached_node", node);
      }
      const now = this.now();
      this.db.query(`
        UPDATE run_nodes SET state = 'DONE', job_id = ?, cache_hit = 1, params = ?, outputs = ?,
                             reserved_usd = 0, error = NULL, updated_at = ?
        WHERE run_id = ? AND node_id = ?
      `).run(job.id, canonicalJson(options.params), canonicalJson(job.outputs), now, runId, nodeId);
      this.db.query(`
        INSERT INTO run_node_jobs (run_id, node_id, job_id, created_at) VALUES (?, ?, ?, ?)
        ON CONFLICT (run_id, node_id, job_id) DO NOTHING
      `).run(runId, nodeId, job.id, now);
      this.db.query("UPDATE runs SET updated_at = ? WHERE id = ?").run(now, runId);
      this.nodeEvent(node, "DONE", job.id, now, true);
      return this.node(runId, nodeId);
    }).immediate();
  }

  settleNode(runId: string, nodeId: string, job: Pick<Job, "id" | "state" | "costUsd" | "outputs" | "error">): RunNode {
    return this.db.transaction(() => {
      const run = this.get(runId);
      const node = this.node(runId, nodeId);
      const target = job.state === "SEALED" ? "DONE" : job.state === "DEAD" ? "FAILED" : null;
      if (node.jobId === job.id && node.state === target) return node;
      if (!target || node.state !== "SUBMITTED" || node.jobId !== job.id) this.invalidState(run, "settle", node);
      if (!Number.isFinite(job.costUsd) || job.costUsd < 0 || !Number.isFinite(run.spentUsd + job.costUsd)) {
        throw new FactoryError("RUN_BUDGET_INVALID", "节点结算金额不合法", { runId, nodeId, costUsd: job.costUsd });
      }
      const now = this.now();
      const outputs = target === "DONE" ? job.outputs : {};
      const error = target === "FAILED" ? job.error ?? { code: "JOB_FAILED" } : null;
      this.setNode(node, target, outputs, error, now);
      this.db.query(`
        UPDATE runs SET reserved_usd = (SELECT COALESCE(SUM(reserved_usd), 0) FROM run_nodes WHERE run_id = ? AND state = 'SUBMITTED'),
                        spent_usd = spent_usd + ?, updated_at = ? WHERE id = ?
      `).run(runId, job.costUsd, now, runId);
      this.budgetEvent(run, nodeId, 0, job.costUsd, now);
      return this.node(runId, nodeId);
    }).immediate();
  }

  failNode(runId: string, nodeId: string, error: JsonObject): RunNode {
    return this.changeWaitingNode(runId, nodeId, "FAILED", error);
  }

  skipNode(runId: string, nodeId: string): RunNode {
    return this.changeWaitingNode(runId, nodeId, "SKIPPED", null);
  }

  completeSwitch(runId: string, nodeId: string, selected: string): RunNode {
    return this.changeWaitingNode(runId, nodeId, "DONE", null, { selected });
  }

  cancel(id: string, by: string): Run {
    return this.transition(id, "CANCELLING", {
      reason: "cancel by=" + by, error: { code: "RUN_CANCELLED", message: "run 已取消", by },
    });
  }

  retryNode(id: string, nodeId: string, by: string): Run {
    return this.db.transaction(() => {
      const run = this.get(id);
      const node = this.node(id, nodeId);
      if ((run.state !== "FAILED" && run.state !== "FAILING") || node.state !== "FAILED") this.invalidState(run, "retry_node", node);
      const now = this.now();
      this.db.query(`
        UPDATE run_nodes SET state = 'WAITING', job_id = NULL, cache_hit = 0, outputs = '{}',
                             params = NULL, reserved_usd = 0, error = NULL, updated_at = ?
        WHERE run_id = ? AND (node_id = ? OR state = 'SKIPPED')
      `).run(now, id, nodeId);
      this.nodeEvent(node, "WAITING", null, now);
      return this.setRun(run, "RUNNING", { reason: "retry_node by=" + by, error: null }, now);
    }).immediate();
  }

  private setRun(run: Run, state: RunState, patch: RunPatch, now: string): Run {
    if (!edges[run.state]?.includes(state)) this.invalidState(run, "transition");
    if (runTerminalStates.has(state)) {
      const active = this.db.query<NodeRow>(
        "SELECT * FROM run_nodes WHERE run_id = ? AND state IN ('SUBMITTED', 'WAITING', 'FAILED') ORDER BY ord",
      ).all(run.id);
      if (active.some((node) => node.state === "SUBMITTED") || (state === "SUCCEEDED" && active.length)) {
        this.invalidState(run, "finish");
      }
      for (const node of active.filter((node) => node.state === "WAITING").map(decodeNode)) {
        this.setNode(node, "SKIPPED", {}, null, now);
      }
    }
    const error = patch.error === undefined ? run.error : patch.error;
    const outputs = patch.outputs ?? run.outputs;
    this.db.query("UPDATE runs SET state = ?, error = ?, outputs = ?, updated_at = ? WHERE id = ?")
      .run(state, error === null ? null : canonicalJson(error), canonicalJson(outputs), now, run.id);
    this.event(run.id, "run.state", { from: run.state, to: state, reason: patch.reason ?? "", error }, now);
    if (state === "SUCCEEDED") this.event(run.id, "run.done", { outputs }, now);
    return this.get(run.id);
  }

  private changeWaitingNode(runId: string, nodeId: string, state: "FAILED" | "SKIPPED" | "DONE", error: JsonObject | null, params?: JsonObject): RunNode {
    return this.db.transaction(() => {
      const run = this.get(runId);
      const node = this.node(runId, nodeId);
      if (node.state !== "WAITING" || run.state !== "RUNNING") this.invalidState(run, "change_node", node);
      const now = this.now();
      this.setNode(node, state, {}, error, now);
      if (params) this.db.query("UPDATE run_nodes SET params = ? WHERE run_id = ? AND node_id = ?").run(canonicalJson(params), runId, nodeId);
      this.db.query("UPDATE runs SET updated_at = ? WHERE id = ?").run(now, runId);
      return this.node(runId, nodeId);
    }).immediate();
  }

  private setNode(node: RunNode, state: NodeState, outputs: PortIds, error: JsonObject | null, now: string): void {
    this.db.query(`
      UPDATE run_nodes SET state = ?, outputs = ?, error = ?, reserved_usd = 0, updated_at = ?
      WHERE run_id = ? AND node_id = ?
    `).run(state, canonicalJson(outputs), error === null ? null : canonicalJson(error), now, node.runId, node.nodeId);
    this.nodeEvent(node, state, node.jobId, now);
  }

  private invalidState(run: Run, op: string, node?: RunNode): never {
    throw new FactoryError("RUN_STATE_INVALID", "当前运行状态不允许此操作", {
      runId: run.id, state: run.state, op, ...(node && { node: node.nodeId, nodeState: node.state }),
    });
  }

  private now(): string { return utcIso(this.clock.now()); }

  private event(runId: string, kind: string, data: JsonObject, at: string): void {
    this.db.query("INSERT INTO events (id, at, kind, data, run_id) VALUES (?, ?, ?, ?, ?)")
      .run(this.ids.new("ev"), at, kind, canonicalJson(data), runId);
  }

  private nodeEvent(node: RunNode, state: NodeState, jobId: string | null, at: string, cacheHit = node.cacheHit): void {
    this.event(node.runId, "run.node", { node: node.nodeId, from: node.state, to: state, job: jobId, cache_hit: cacheHit }, at);
  }

  private budgetEvent(run: Run, nodeId: string, reservedUsd: number, spentUsd: number, at: string): void {
    this.event(run.id, "run.budget", {
      node: nodeId, reserved_usd: reservedUsd, spent_usd: spentUsd, run_spent_usd: run.spentUsd + spentUsd,
    }, at);
  }
}
