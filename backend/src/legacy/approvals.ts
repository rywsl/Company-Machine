import { compileExpr, evaluate, type EvalEnvironment } from "../../mrl/evaluator";
import { isMrlNumber, nativeNumber } from "../../mrl/numbers";
import { parseRuleModule } from "../../mrl/rules";
import { BOOL, DYN, NUM, STRING, record } from "../../mrl/types";
import type { Context } from "./context";
import { FactoryError } from "./errors";
import { proposalPayload, type ProposalPayload } from "./factoryctl";
import { canonicalJson, parseDuration, utcIso } from "./foundation";
import { JobStore } from "./jobs";
import type { Snapshot } from "./snapshot";
import { approvalEnv } from "./values";
import type { Bean } from "./warehouse";

export { readProposal, writeProposal, type ProposalPayload } from "./factoryctl";

export type ApprovalStatus = "pending" | "approved" | "denied";
export type ApprovalDecision = "approve" | "deny";
export type ApprovalRoute = "auto" | "foreman" | "human" | "review" | "missing";
export type ApprovalPayload = { kind: string; summary: string; cost_usd: number };
export interface Approval {
  id: string;
  jobId: string;
  attempt: number;
  requestId: string;
  phase: "in_run" | "propose";
  status: ApprovalStatus;
  route: ApprovalRoute;
  payload: ApprovalPayload | ProposalPayload;
  deadline: string | null;
  decision: ApprovalDecision | null;
  decidedBy: string | null;
  note: string;
  createdAt: string;
  decidedAt: string | null;
}

interface ApprovalContext {
  jobId: string;
  attempt: number;
  stationKey?: string;
  inputs?: Record<string, Bean | Bean[]>;
  params?: Record<string, unknown>;
}
interface OpenRequest extends ApprovalContext {
  requestId?: string;
  payload: { kind: string; summary: string; cost_usd?: number };
}
interface OpenProposal extends ApprovalContext { payload: ProposalPayload; }

export interface ApprovalVote {
  approvalId: string;
  runtime: string;
  by: string;
  decision: ApprovalDecision;
  note: string;
  at: string;
}

type ApprovalFilter = { jobId?: string; attempt?: number; status?: ApprovalStatus };
type JsonObject = Record<string, unknown>;
type Row = {
  id: string; job_id: string; attempt: number; request_id: string; mailbox_request_id: string | null; phase: Approval["phase"];
  kind: string; summary: string; cost_usd: number; status: ApprovalStatus | "timed_out"; route: ApprovalRoute;
  payload: string; deadline: string | null; decision: ApprovalDecision | null; decided_by: string | null; by: string | null;
  note: string; created_at: string; decided_at: string | null;
};

const DEFAULT_TIMEOUT_SECONDS = 600;
const MRL_ENVIRONMENT = {
  in_run: { request: record({ kind: STRING, summary: STRING, cost_usd: NUM }), inputs: DYN, params: DYN },
  propose: { proposal: record({ summary: STRING, cost_estimate_usd: NUM, time_estimate_s: NUM }), inputs: DYN, params: DYN },
};
const SECRET = /\b(api[_-]?key|secret|token|password|authorization|cookie)(\s*[:=]\s*)(?:bearer\s+)?(?:"[^"]*"|\S+)/gi;
const redact = (text: string) => text.replace(SECRET, "$1$2[REDACTED]");

function redactPlan(value: unknown): unknown {
  if (typeof value === "string") return redact(value);
  if (Array.isArray(value)) return value.map(redactPlan);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [
      key, /(?:api[_-]?key|secret|token|password|authorization|cookie)/i.test(key) ? "[REDACTED]" : redactPlan(item),
    ]));
  }
  return value;
}

function decode(row: Row): Approval {
  const { status } = row;
  const legacyTimeout = status === "timed_out";
  const stored = JSON.parse(row.payload);
  const payload = row.phase === "propose"
    ? { summary: row.summary, cost_estimate_usd: row.cost_usd, time_estimate_s: 0, ...stored } as ProposalPayload
    : { kind: row.kind, summary: row.summary, cost_usd: row.cost_usd, ...stored } as ApprovalPayload;
  return {
    id: row.id, jobId: row.job_id, attempt: row.attempt, requestId: row.mailbox_request_id ?? row.request_id,
    phase: row.phase, status: legacyTimeout ? "denied" : status, route: row.route,
    payload,
    deadline: row.deadline, decision: legacyTimeout ? "deny" : row.decision,
    decidedBy: legacyTimeout ? "timeout" : row.decided_by ?? row.by, note: row.note,
    createdAt: row.created_at, decidedAt: row.decided_at,
  };
}

function approvalPayload(value: OpenRequest["payload"]): ApprovalPayload {
  if (!value || typeof value.kind !== "string" || !value.kind.trim() || typeof value.summary !== "string" || !value.summary.trim()) {
    throw new FactoryError("APPROVAL_PAYLOAD_INVALID", "审批请求需要 kind 和 summary");
  }
  const cost = value.cost_usd === undefined ? 0 : value.cost_usd;
  if (!Number.isFinite(cost) || cost < 0) throw new FactoryError("APPROVAL_PAYLOAD_INVALID", "审批费用必须为有限非负数");
  return { kind: value.kind, summary: value.summary, cost_usd: cost };
}

export class Approvals {
  private readonly jobs: JobStore;
  constructor(readonly context: Pick<Context, "db" | "clock" | "ids">, private readonly registry?: Snapshot, jobs?: JobStore) {
    const { db, clock, ids } = context;
    this.jobs = jobs ?? new JobStore(db, clock, ids);
    db.transaction(() => {
      db.exec(`
        CREATE TABLE IF NOT EXISTS approvals (
          id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt INTEGER NOT NULL, request_id TEXT NOT NULL,
          phase TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL, cost_usd REAL NOT NULL, status TEXT NOT NULL,
          by TEXT, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, decided_at TEXT,
          route TEXT NOT NULL DEFAULT 'foreman', payload TEXT NOT NULL DEFAULT '{}', deadline TEXT,
          decision TEXT, decided_by TEXT, mailbox_request_id TEXT
        );
        CREATE INDEX IF NOT EXISTS approvals_job ON approvals(job_id);
        CREATE UNIQUE INDEX IF NOT EXISTS approvals_request ON approvals(request_id);
      `);
      const columns = new Set(db.query<{ name: string }>("PRAGMA table_info(approvals)").all().map((column) => column.name));
      for (const [column, definition] of Object.entries({
        route: "TEXT NOT NULL DEFAULT 'foreman'", payload: "TEXT NOT NULL DEFAULT '{}'", deadline: "TEXT",
        decision: "TEXT", decided_by: "TEXT", mailbox_request_id: "TEXT",
      })) {
        if (!columns.has(column)) db.exec("ALTER TABLE approvals ADD COLUMN " + column + " " + definition);
      }
      db.exec(`
        CREATE UNIQUE INDEX IF NOT EXISTS approvals_mailbox ON approvals(job_id, attempt, mailbox_request_id);
        CREATE INDEX IF NOT EXISTS approvals_deadline ON approvals(status, deadline);
        CREATE INDEX IF NOT EXISTS approvals_proposal ON approvals(job_id, attempt, phase);
        CREATE TABLE IF NOT EXISTS approval_votes (
          approval_id TEXT NOT NULL, runtime TEXT NOT NULL, by TEXT NOT NULL, decision TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '', at TEXT NOT NULL, PRIMARY KEY (approval_id, runtime)
        );
      `);
    }).immediate();
  }

  get snapshot(): Snapshot {
    if (!this.registry) throw new FactoryError("EPOCH_NOT_FOUND", "审批决定需要绑定的 Registry 快照");
    return this.registry;
  }

  openRequest(options: OpenRequest): Approval {
    const { db, ids, clock } = this.context;
    const payload = approvalPayload(options.payload);
    const stored = { ...payload, kind: redact(payload.kind), summary: redact(payload.summary) };
    return db.transaction(() => {
      const job = this.jobs.get(options.jobId);
      if (options.stationKey !== undefined && job.station !== this.snapshot.key("Station", options.stationKey)) {
        throw new FactoryError("APPROVAL_STATE_INVALID", "审批工位与 Job 不一致", { jobId: job.id });
      }
      if (options.requestId !== undefined) {
        if (!options.requestId) throw new FactoryError("APPROVAL_PAYLOAD_INVALID", "requestId 不能为空");
        const existing = this.byRequest(options.jobId, options.attempt, options.requestId);
        if (existing) {
          if (existing.phase !== "in_run" || canonicalJson(existing.payload) !== canonicalJson(stored)) {
            throw new FactoryError("APPROVAL_PAYLOAD_INVALID", "审批请求标识已用于其他载荷", { approvalId: existing.id });
          }
          return existing;
        }
      }
      if (job.attempt !== options.attempt || job.epoch !== this.snapshot.epoch ||
          (job.state !== "RUNNING" && job.state !== "AWAITING_APPROVAL")) {
        throw new FactoryError("APPROVAL_STATE_INVALID", "当前 Job 尝试无法开启审批", { jobId: job.id, attempt: options.attempt });
      }
      const settings = this.snapshot.spec(job.station).approval;
      const { route, note } = this.resolveRoute("in_run", settings?.in_run, payload, options);
      const id = ids.new("a");
      const requestId = options.requestId ?? id;
      const internalRequestId = "mailbox:" + canonicalJson([job.id, options.attempt, requestId]);
      const at = clock.now();
      const created = utcIso(at);
      const timeout = settings?.timeout === undefined ? DEFAULT_TIMEOUT_SECONDS : parseDuration(settings.timeout);
      const deadline = utcIso(new Date(at.getTime() + timeout * 1000));
      db.query(`
        INSERT INTO approvals (id, job_id, attempt, request_id, mailbox_request_id, phase, kind, summary, cost_usd,
                               status, route, payload, deadline, note, created_at)
        VALUES (?, ?, ?, ?, ?, 'in_run', ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
      `).run(id, job.id, options.attempt, internalRequestId, requestId, stored.kind, stored.summary, stored.cost_usd,
        route, canonicalJson(stored), deadline, note, created);
      this.event(job.id, "approval.requested", { id, job: job.id, phase: "in_run", route, deadline }, created);
      const approval = this.get(id);
      if (route === "auto") return this.settle(approval, "approve", "auto", note, created);
      if (route === "missing") return this.settle(approval, "deny", "auto", "APPROVAL_DENIED", created);
      return approval;
    }).immediate();
  }

  openProposal(options: OpenProposal): Approval {
    const { db, clock, ids } = this.context;
    const proposed = proposalPayload(options.payload);
    const { plan, ...decisionPayload } = proposed;
    const rulePayload = { ...decisionPayload, summary: redact(decisionPayload.summary) };
    const payload: ProposalPayload = { ...rulePayload };
    if (plan !== undefined) payload.plan = redactPlan(plan) as JsonObject;
    return db.transaction(() => {
      const job = this.jobs.get(options.jobId);
      if (options.stationKey !== undefined && job.station !== this.snapshot.key("Station", options.stationKey)) {
        throw new FactoryError("APPROVAL_STATE_INVALID", "审批工位与 Job 不一致", { jobId: job.id });
      }
      const existing = this.proposal(job.id, options.attempt);
      if (existing) {
        if (canonicalJson(existing.payload) !== canonicalJson(payload)) {
          throw new FactoryError("PROPOSAL_INVALID", "本次尝试已有不同的持久化方案", { approvalId: existing.id });
        }
        return existing;
      }
      if (job.attempt !== options.attempt || job.epoch !== this.snapshot.epoch ||
          (job.state !== "RUNNING" && job.state !== "AWAITING_APPROVAL")) {
        throw new FactoryError("APPROVAL_STATE_INVALID", "当前 Job 尝试无法提交方案", { jobId: job.id, attempt: options.attempt });
      }
      const settings = this.snapshot.spec(job.station).approval;
      if (!settings?.propose) throw new FactoryError("APPROVAL_RULE_MISSING", "工位缺少方案审批规则", { station: job.station });
      const { route, note } = this.resolveRoute("propose", settings.propose, rulePayload, options);
      if (route === "review" && this.reviewQuorum() === undefined) {
        throw new FactoryError("REVIEW_QUORUM_UNSET", "复核法定人数没有设置", { station: job.station });
      }
      const at = clock.now();
      const created = utcIso(at);
      const deadline = settings.timeout == null ? null : utcIso(new Date(at.getTime() + parseDuration(settings.timeout) * 1000));
      const id = ids.new("a");
      db.query(`
        INSERT INTO approvals (id, job_id, attempt, request_id, phase, kind, summary, cost_usd,
                               status, route, payload, deadline, note, created_at)
        VALUES (?, ?, ?, ?, 'propose', 'propose', ?, ?, 'pending', ?, ?, ?, ?, ?)
      `).run(id, job.id, options.attempt, id, payload.summary, payload.cost_estimate_usd, route, canonicalJson(payload), deadline, note, created);
      this.event(job.id, "approval.requested", { id, job: job.id, phase: "propose", route, deadline }, created);
      const approval = this.get(id);
      return route === "auto" ? this.settle(approval, "approve", "auto", note, created) : approval;
    }).immediate();
  }

  proposal(jobId: string, attempt: number): Approval | null {
    const row = this.context.db.query<Row>(`
      SELECT * FROM approvals WHERE job_id = ? AND attempt = ? AND phase = 'propose' ORDER BY rowid LIMIT 1
    `).get(jobId, attempt);
    return row ? decode(row) : null;
  }

  votes(approvalId: string): ApprovalVote[] {
    return this.context.db.query<ApprovalVote>(`
      SELECT approval_id AS approvalId, runtime, by, decision, note, at
      FROM approval_votes WHERE approval_id = ? ORDER BY runtime
    `).all(approvalId);
  }

  forProvenance(jobId: string): JsonObject[] {
    return this.list({ jobId }).map(({ id, phase, decision, decidedBy, route, payload }) => ({
      id, phase, decision, decided_by: decidedBy, route,
      cost_usd: "cost_estimate_usd" in payload ? payload.cost_estimate_usd : payload.cost_usd,
    }));
  }

  get(id: string): Approval {
    const row = this.context.db.query<Row>("SELECT * FROM approvals WHERE id = ?").get(id);
    if (!row) throw new FactoryError("APPROVAL_NOT_FOUND", "找不到审批 " + id, { approvalId: id });
    return decode(row);
  }

  byRequest(jobId: string, attempt: number, requestId: string): Approval | null {
    const row = this.context.db.query<Row>(`
      SELECT * FROM approvals WHERE job_id = ? AND attempt = ? AND mailbox_request_id = ?
      UNION ALL
      SELECT * FROM approvals WHERE job_id = ? AND attempt = ? AND mailbox_request_id IS NULL AND request_id = ?
      LIMIT 1
    `).get(jobId, attempt, requestId, jobId, attempt, requestId);
    return row ? decode(row) : null;
  }

  list(filter: ApprovalFilter = {}): Approval[] {
    const conditions: string[] = [];
    const params: Array<string | number> = [];
    if (filter.jobId !== undefined) { conditions.push("job_id = ?"); params.push(filter.jobId); }
    if (filter.attempt !== undefined) { conditions.push("attempt = ?"); params.push(filter.attempt); }
    if (filter.status === "denied") conditions.push("status IN ('denied', 'timed_out')");
    else if (filter.status !== undefined) { conditions.push("status = ?"); params.push(filter.status); }
    const where = conditions.length ? " WHERE " + conditions.join(" AND ") : "";
    return this.context.db.query<Row>("SELECT * FROM approvals" + where + " ORDER BY created_at, id").all(...params).map(decode);
  }

  applyDecision(id: string, decision: ApprovalDecision, options: { by: string; note?: string }): Approval {
    const by = options.by?.trim();
    if (!by || ["auto", "timeout"].includes(by.toLowerCase())) {
      throw new FactoryError("APPROVAL_BY_INVALID", "审批需要有效的决定者", { approvalId: id });
    }
    const { db, clock } = this.context;
    const outcome = db.transaction(() => {
      const approval = this.get(id);
      if (approval.status !== "pending" || (decision !== "approve" && decision !== "deny")) {
        throw new FactoryError("APPROVAL_STATE_INVALID", "审批不在可决定状态", { approvalId: id, status: approval.status });
      }
      const job = this.jobs.get(approval.jobId);
      if (job.epoch !== this.snapshot.epoch || job.attempt !== approval.attempt ||
          (job.state !== "RUNNING" && job.state !== "AWAITING_APPROVAL")) {
        throw new FactoryError("APPROVAL_STATE_INVALID", "审批所属 Job 尝试已结束", { approvalId: id, jobId: job.id });
      }
      const now = utcIso(clock.now());
      if (approval.deadline !== null && Date.parse(approval.deadline) <= Date.parse(now)) {
        return { expired: true, approval: this.settle(approval, "deny", "timeout", "APPROVAL_TIMEOUT", now) };
      }
      const note = redact(options.note ?? "");
      const decided = approval.phase === "propose" && approval.route === "review"
        ? this.vote(approval, decision, by, note, now)
        : this.settle(approval, decision, by, note, now);
      return { expired: false, approval: decided };
    }).immediate();
    if (outcome.expired) throw new FactoryError("APPROVAL_TIMEOUT", "审批已超时", { approvalId: id });
    return outcome.approval;
  }

  expireDue(filter: Pick<ApprovalFilter, "jobId" | "attempt"> = {}): string[] {
    const { db, clock } = this.context;
    return db.transaction(() => {
      const at = utcIso(clock.now());
      const clauses = ["status = 'pending'", "deadline <= ?"];
      const params: Array<string | number> = [at];
      if (filter.jobId !== undefined) { clauses.push("job_id = ?"); params.push(filter.jobId); }
      if (filter.attempt !== undefined) { clauses.push("attempt = ?"); params.push(filter.attempt); }
      const due = db.query<Row>("SELECT * FROM approvals WHERE " + clauses.join(" AND ") + " ORDER BY id").all(...params);
      return due.map((row) => this.settle(decode(row), "deny", "timeout", "APPROVAL_TIMEOUT", at).id);
    }).immediate();
  }

  closePending(filter: { jobId: string; attempt: number }, note: string): string[] {
    const { db, clock } = this.context;
    const closedNote = redact(note);
    return db.transaction(() => {
      const at = utcIso(clock.now());
      const rows = db.query<Row>(`
        SELECT * FROM approvals WHERE job_id = ? AND attempt = ? AND phase = 'in_run' AND status = 'pending' ORDER BY id
      `).all(filter.jobId, filter.attempt);
      return rows.map((row) => {
        const approval = decode(row);
        if (approval.deadline && Date.parse(approval.deadline) <= Date.parse(at)) {
          return this.settle(approval, "deny", "timeout", "APPROVAL_TIMEOUT", at).id;
        }
        return this.settle(approval, "deny", "runner", closedNote, at).id;
      });
    }).immediate();
  }

  ctlResult(approval: Approval): { decision: ApprovalDecision | "timeout"; note: string; code: string } | null {
    if (approval.status === "pending") return null;
    if (approval.decidedBy === "timeout") return { decision: "timeout", note: approval.note, code: "APPROVAL_TIMEOUT" };
    if (approval.status === "approved") return { decision: "approve", note: approval.note, code: "" };
    const code = approval.phase === "propose" && approval.route === "review" ? "APPROVAL_REVIEW_SPLIT" : "APPROVAL_DENIED";
    return { decision: "deny", note: approval.note, code };
  }

  private resolveRoute(
    phase: Approval["phase"], rule: ApprovalRoute | { auto_if: string; else: ApprovalRoute } | undefined,
    payload: ApprovalPayload | Omit<ProposalPayload, "plan">, options: ApprovalContext,
  ): { route: ApprovalRoute; note: string } {
    if (!rule || typeof rule === "string") return { route: rule ?? "missing", note: "" };
    try {
      const env = approvalEnv(phase, { ...payload }, options.inputs ?? {}, options.params ?? {});
      const predicate = compileExpr(rule.auto_if, { environment: MRL_ENVIRONMENT[phase], expect: BOOL });
      return { route: predicate.evaluate(env as EvalEnvironment) === true ? "auto" : rule.else, note: "" };
    } catch {
      return { route: rule.else === "auto" ? "foreman" : rule.else, note: "APPROVAL_EVAL_ERROR" };
    }
  }

  private reviewQuorum(): number | undefined {
    const values = new Set<unknown>();
    for (const module of this.snapshot.result.registry.constitution) {
      for (const rule of parseRuleModule(module.source, module.file).rules) {
        if (rule.relation !== "limit" || rule.body.length || rule.head.length !== 2) continue;
        const [name, value] = rule.head;
        if (name.kind === "expression" && name.ast.kind === "literal" && name.ast.value === "approval_review_quorum" && value.kind === "expression") {
          const quorum = evaluate(value.ast);
          values.add(isMrlNumber(quorum) ? nativeNumber(quorum) : quorum);
        }
      }
    }
    const [value] = values;
    return values.size === 1 && typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : undefined;
  }

  private vote(approval: Approval, decision: ApprovalDecision, by: string, note: string, at: string): Approval {
    const runtime = /^runtime:([a-z][a-z0-9-]*)@[0-9]+$/.exec(by)?.[1];
    if (!runtime) throw new FactoryError("APPROVAL_BY_INVALID", "复核决定者必须包含 Runtime 名称和版本", { approvalId: approval.id });
    const quorum = this.reviewQuorum();
    if (quorum === undefined) throw new FactoryError("REVIEW_QUORUM_UNSET", "复核法定人数没有设置", { approvalId: approval.id });
    if (this.votes(approval.id).some((item) => item.runtime === runtime)) {
      throw new FactoryError("APPROVAL_STATE_INVALID", "同一 Runtime 不能重复复核", { approvalId: approval.id, runtime });
    }
    this.context.db.query("INSERT INTO approval_votes (approval_id, runtime, by, decision, note, at) VALUES (?, ?, ?, ?, ?, ?)")
      .run(approval.id, runtime, by, decision, note, at);
    this.event(approval.jobId, "approval.vote", { id: approval.id, runtime, decision }, at);
    if (decision === "deny") return this.settle(approval, "deny", by, note, at);
    const votes = this.votes(approval.id);
    if (votes.length < quorum) return approval;
    return this.settle(approval, "approve", votes.map((item) => item.by).join(","), note, at);
  }

  private settle(approval: Approval, decision: ApprovalDecision, by: string, note: string, at: string): Approval {
    const status = decision === "approve" ? "approved" : "denied";
    this.context.db.query(`
      UPDATE approvals SET status = ?, decision = ?, by = ?, decided_by = ?, note = ?, decided_at = ? WHERE id = ?
    `).run(status, decision, by, by, note, at, approval.id);
    if (by === "timeout") this.event(approval.jobId, "approval.expired", { id: approval.id, deadline: approval.deadline }, at);
    this.event(approval.jobId, "job.approval", { approval: approval.id, decision, by }, at);
    return { ...approval, status, decision, decidedBy: by, note, decidedAt: at };
  }

  private event(jobId: string, kind: string, data: JsonObject, at: string): void {
    this.context.db.query("INSERT INTO job_events (id,job_id,kind,data,at) VALUES (?,?,?,?,?)")
      .run(this.context.ids.new("event"), jobId, kind, canonicalJson(data), at);
  }
}
