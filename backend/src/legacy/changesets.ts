import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { validate } from "../../mrl/factory/schema";
import { governanceLimits, object } from "./change-facts";
import type { Context } from "./context";
import { FactoryError } from "./errors";
import { canonicalBytes, canonicalJson, digestBytes, parseDuration, utcIso } from "./foundation";
import type { EpochStore } from "./registry-epochs";
import type { Snapshot } from "./snapshot";

type Data = Record<string, unknown>;
interface ChangeSetRow {
  id: string;
  base_epoch: string;
  source_commit: string;
  commit: string;
  state: string;
  risk_tier: string | null;
  reviews_required: number;
  proposer: string;
  proposer_runtime: string | null;
  document: string;
  report: string;
  stage_started_at: string | null;
  activated_at: string | null;
  created_at: string;
  updated_at: string;
}

export type ChangeSet = Omit<ChangeSetRow, "document" | "report"> & { document: Data; report: Data };
const gitObjectId = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;
const allowedSlo = new Set(["success_rate", "gate_reject_rate", "cost_per_run_usd"]);

function decodeRow(row: ChangeSetRow): ChangeSet {
  return { ...row, document: JSON.parse(row.document), report: JSON.parse(row.report || "{}") };
}

function checkDocument(document: unknown): { document: Data; base: string; intent: string } {
  const doc = object(document);
  const problems = doc.kind === "ChangeSet" ? validate(doc).map((error) => error.path || error.message) : ["kind"];
  const specification = object(doc.spec);
  const rollout = object(specification.rollout);
  const budget = object(rollout.shadow).budget_usd;
  if (rollout.shadow !== undefined && (typeof budget !== "number" || !Number.isFinite(budget) || budget < 0)) problems.push("shadow.budget_usd");
  if (rollout.observe !== undefined) {
    try { parseDuration(String(rollout.observe)); } catch { problems.push("observe"); }
  }
  if (rollout.canary !== undefined) {
    const canary = object(rollout.canary);
    const { percent, min_runs: minRuns } = canary;
    if (typeof percent !== "number" || !Number.isInteger(percent) || percent < 1 || percent > 100) problems.push("canary.percent");
    if (typeof minRuns !== "number" || !Number.isInteger(minRuns) || minRuns < 1) problems.push("canary.min_runs");
  }
  if (rollout.slo !== undefined) {
    if (rollout.slo === null || typeof rollout.slo !== "object" || Array.isArray(rollout.slo)) problems.push("slo");
    else for (const [name, value] of Object.entries(object(rollout.slo))) {
      if (!allowedSlo.has(name) || typeof value !== "string" || !/^(>=|<=|>|<)\s*[0-9]+(\.[0-9]+)?$/.test(value)) problems.push(`slo.${name}`);
    }
  }
  if (problems.length) throw new FactoryError("CHANGESET_INVALID", "变更文档不合法", { problems });
  const { risk_tier: _claimedRisk, status: _claimedStatus, ...spec } = specification;
  return { document: { ...doc, spec }, base: String(spec.base_epoch), intent: String(spec.intent) };
}

type GitChild = { exited: Promise<number>; stdout: ReadableStream<Uint8Array>; stderr: ReadableStream<Uint8Array>; kill(signal: number): void };
const gitRuntime = Bun as unknown as { spawn(argv: string[], options: Record<string, unknown>): GitChild };

async function git(registry: string, args: string[], timeoutMs: number, environment: Record<string, string> = {}, allowed = [0]) {
  if (timeoutMs <= 0) throw new FactoryError("GIT_FAILED", "Git 命令超时", { command: args[0] });
  const ambient = Object.fromEntries(Object.entries(process.env).filter((entry): entry is [string, string] =>
    typeof entry[1] === "string" && !/^GIT_/i.test(entry[0])));
  const noHooks = process.platform === "win32" ? "NUL" : "/dev/null";
  const argv = ["git", "-C", registry, "-c", `core.hooksPath=${noHooks}`, "-c", "commit.gpgsign=false", ...args];
  let child: GitChild;
  try {
    child = gitRuntime.spawn(argv, { stdin: "ignore", stdout: "pipe", stderr: "pipe", env: {
      ...ambient, GIT_TERMINAL_PROMPT: "0", GIT_NO_REPLACE_OBJECTS: "1", GIT_NO_LAZY_FETCH: "1", ...environment,
    } });
  } catch {
    throw new FactoryError("GIT_FAILED", "无法启动 Git", { command: args[0] });
  }
  let timeout = false;
  const timer = setTimeout(() => { timeout = true; child.kill(9); }, timeoutMs);
  try {
    const [code, stdout, stderr] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()]);
    if (timeout || !allowed.includes(code)) {
      throw new FactoryError("GIT_FAILED", timeout ? "Git 命令超时" : "Git 命令失败", { command: args[0], stderr: stderr.trim().slice(0, 4_000) });
    }
    return { code, stdout };
  } finally { clearTimeout(timer); }
}

export class ChangeSetStore {
  constructor(readonly context: Context, readonly epochs: EpochStore) {
    context.db.exec(`
      CREATE TABLE IF NOT EXISTS changesets (
        id TEXT PRIMARY KEY, base_epoch TEXT NOT NULL, source_commit TEXT NOT NULL, "commit" TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL, risk_tier TEXT, reviews_required INTEGER NOT NULL DEFAULT 0,
        proposer TEXT NOT NULL, proposer_runtime TEXT, document TEXT NOT NULL, report TEXT NOT NULL DEFAULT '{}',
        stage_started_at TEXT, activated_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS changesets_state ON changesets(state);
      CREATE INDEX IF NOT EXISTS changesets_activated ON changesets(activated_at);
      CREATE TABLE IF NOT EXISTS reviews (
        id TEXT PRIMARY KEY, changeset_id TEXT NOT NULL REFERENCES changesets(id),
        runtime TEXT NOT NULL, runtime_digest TEXT NOT NULL, verdict TEXT NOT NULL,
        note TEXT NOT NULL, by TEXT NOT NULL, at TEXT NOT NULL,
        UNIQUE(changeset_id, runtime)
      );
    `);
  }

  get(id: string): ChangeSet {
    const row = this.context.db.query<ChangeSetRow>("SELECT * FROM changesets WHERE id = ?").get(id);
    if (!row) throw new FactoryError("CHANGESET_NOT_FOUND", "找不到变更", { id });
    return decodeRow(row);
  }

  list(state?: string): ChangeSet[] {
    const { db } = this.context;
    const rows = state === undefined ? db.query<ChangeSetRow>("SELECT * FROM changesets ORDER BY id").all()
      : db.query<ChangeSetRow>("SELECT * FROM changesets WHERE state = ? ORDER BY id").all(state);
    return rows.map(decodeRow);
  }

  async addReview(id: string, review: { runtime: string; verdict: string; note: string; by: string }): Promise<string> {
    const reviewable = (change: ChangeSet) => {
      if (!["verified", "shadow", "canary"].includes(change.state)) {
        throw new FactoryError("CHANGESET_STATE_INVALID", "当前状态不能复核", { id, state: change.state });
      }
    };
    const change = this.get(id);
    reviewable(change);
    if (!["approve", "deny"].includes(review.verdict) || Array.from(review.note).length > 2000) {
      throw new FactoryError("REVIEW_INVALID", "复核不合法", { id, runtime: review.runtime });
    }
    const snapshot = await this.epochs.materialize(change.commit);
    const runtime = snapshot.key("OperatorRuntime", review.runtime);
    if (!runtime.startsWith("OperatorRuntime:") || !Object.hasOwn(snapshot.docs, runtime)) {
      throw new FactoryError("REVIEW_INVALID", "复核运行时不存在", { id, runtime: review.runtime });
    }
    const digest = digestBytes(canonicalBytes(snapshot.doc(runtime)));
    const { db, ids, clock } = this.context;
    return db.transaction(() => {
      const current = this.get(id);
      reviewable(current);
      if (runtime === current.proposer_runtime) {
        throw new FactoryError("REVIEW_SAME_RUNTIME", "提出者不能复核自己", { id, runtime });
      }
      const previous = db.query<{ id: string }>("SELECT id FROM reviews WHERE changeset_id=? AND runtime=?").get(id, runtime);
      const reviewId = previous?.id ?? ids.new("rv");
      const at = utcIso(clock.now());
      db.query(`INSERT INTO reviews (id, changeset_id, runtime, runtime_digest, verdict, note, by, at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(changeset_id, runtime) DO UPDATE SET
        runtime_digest=excluded.runtime_digest, verdict=excluded.verdict, note=excluded.note, by=excluded.by, at=excluded.at`)
        .run(reviewId, id, runtime, digest, review.verdict, review.note, review.by, at);
      db.query("INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'changeset.review', NULL, ?)")
        .run(ids.new("ev"), at, canonicalJson({ id, review: reviewId, runtime, verdict: review.verdict }));
      return reviewId;
    }).immediate();
  }

  approvals(id: string, candidate: Snapshot): string[] {
    const change = this.get(id);
    if (candidate.epoch !== change.commit) throw new FactoryError("EPOCH_MISMATCH", "复核需要候选快照", { id, epoch: candidate.epoch });
    return this.context.db.query<{ runtime: string; runtime_digest: string }>(
      "SELECT runtime, runtime_digest FROM reviews WHERE changeset_id=? AND verdict='approve' ORDER BY runtime",
    ).all(id).filter(({ runtime, runtime_digest }) => runtime !== change.proposer_runtime
      && runtime.startsWith("OperatorRuntime:") && Object.hasOwn(candidate.docs, runtime)
      && digestBytes(canonicalBytes(candidate.doc(runtime))) === runtime_digest
    ).map(({ runtime }) => runtime);
  }

  withdraw(id: string, by: string): ChangeSet {
    const { db, ids, clock } = this.context;
    return db.transaction(() => {
      const current = this.get(id);
      if (!["draft", "verified"].includes(current.state)) {
        throw new FactoryError("CHANGESET_STATE_INVALID", "当前状态不能撤回", { id, state: current.state });
      }
      const at = utcIso(clock.now());
      db.query("UPDATE changesets SET state='withdrawn', updated_at=? WHERE id=?").run(at, id);
      db.query("INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'changeset.state', NULL, ?)")
        .run(ids.new("ev"), at, canonicalJson({ id, from: current.state, to: "withdrawn", code: "WITHDRAWN", by }));
      return this.get(id);
    }).immediate();
  }

  async propose(document: unknown, reference: string, options: { by: string; timeoutSeconds?: number }): Promise<ChangeSet> {
    const checked = checkDocument(document);
    const { db, clock, ids } = this.context;
    const config = this.config();
    const limit = governanceLimits().active_per_24h;
    this.checkWindows(config, limit);
    const configuredTimeout = config.git_timeout_s;
    const seconds = typeof configuredTimeout === "number" && Number.isFinite(configuredTimeout) && configuredTimeout > 0 ? configuredTimeout : 60;
    const deadline = clock.monotonic() + Math.min(options.timeoutSeconds ?? seconds, seconds);
    const run = (args: string[], env?: Record<string, string>, allowed?: number[]) => git(this.epochs.registry, args, (deadline - clock.monotonic()) * 1000, env, allowed);
    const active = this.epochs.current();
    const stale = () => new FactoryError("CHANGESET_STALE", "基线不是当前 active 或提交不是其后代", { base_epoch: checked.base, active: this.epochs.current() });
    const resolved = await run(["rev-parse", "--verify", "--end-of-options", `${reference}^{commit}`], undefined, [0, 1, 128]);
    const source = resolved.stdout.trim();
    if (resolved.code !== 0 || !gitObjectId.test(source)) throw new FactoryError("COMMIT_NOT_FOUND", "找不到提交", { commit: reference });
    if (!active.startsWith(checked.base)) throw stale();
    if ((await run(["merge-base", "--is-ancestor", active, source], undefined, [0, 1])).code !== 0) throw stale();
    const changedPaths = (await run(["diff", "--name-only", "--no-renames", "-z", active, source])).stdout.split("\0").filter(Boolean);
    const constitution = changedPaths.find((path) => path.startsWith("constitution/"));
    if (constitution) throw new FactoryError("CONSTITUTION_FORBIDDEN", "不能修改宪法", { path: constitution });
    if (!changedPaths.length) throw new FactoryError("CHANGESET_INVALID", "变更是空的", { reason: "empty" });
    const tree = (await run(["rev-parse", "--verify", `${source}^{tree}`])).stdout.trim();
    const time = clock.now();
    const date = `@${Math.floor(time.getTime() / 1_000)} +0000`;
    const now = utcIso(time);
    const identity = {
      GIT_AUTHOR_NAME: "mao-governance", GIT_COMMITTER_NAME: "mao-governance", GIT_AUTHOR_EMAIL: "governance@factory.invalid",
      GIT_COMMITTER_EMAIL: "governance@factory.invalid", GIT_AUTHOR_DATE: date, GIT_COMMITTER_DATE: date,
    };
    const id = ids.new("cs");
    const commit = (await run(["commit-tree", tree, "-p", active, "-m", `changeset: ${id}\n\n${checked.intent}`], identity)).stdout.trim();
    if (!gitObjectId.test(commit)) throw new FactoryError("GIT_FAILED", "Git 未返回候选提交 ID");
    const ref = `refs/changesets/${id}`;
    await run(["update-ref", ref, commit, "0".repeat(commit.length)]);
    try {
      const runtime = options.by.startsWith("human:") ? null : options.by.startsWith("OperatorRuntime:") ? options.by : `OperatorRuntime:${options.by}`;
      db.transaction(() => {
        if (this.epochs.current() !== active) throw stale();
        this.checkWindows(this.config(), limit);
        db.query(`INSERT INTO changesets
          (id, base_epoch, source_commit, "commit", state, proposer, proposer_runtime, document, created_at, updated_at)
          VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)`)
          .run(id, active, source, commit, options.by, runtime, canonicalJson(checked.document), now, now);
        db.query("INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'changeset.proposed', NULL, ?)")
          .run(ids.new("ev"), now, canonicalJson({ id, base_epoch: active, source_commit: source, commit, by: options.by }));
      }).immediate();
    } catch (error) {
      await git(this.epochs.registry, ["update-ref", "-d", ref, commit], Math.min(seconds * 1_000, 5_000)).catch(() => {});
      throw error;
    }
    return this.get(id);
  }

  private config(): Data {
    const path = join(this.context.root, "factory.json");
    if (!existsSync(path)) return {};
    try { return object(JSON.parse(readFileSync(path, "utf8")).governance); }
    catch { throw new FactoryError("REGISTRY_NOT_CONFIGURED", "factory.json 无法解析", { path }); }
  }

  freezeUntil(config = this.config()): string | undefined {
    const now = this.context.clock.now().getTime();
    if (Array.isArray(config.freeze)) for (const window of config.freeze) {
      const { start, until } = object(window);
      if (Date.parse(String(start)) <= now && now < Date.parse(String(until))) {
        return String(until);
      }
    }
  }

  private checkWindows(config: Data, limit: number): void {
    const until = this.freezeUntil(config);
    if (until) throw new FactoryError("FREEZE_WINDOW", "封版期内不能受理", { until });
    const now = this.context.clock.now().getTime();
    const count = this.context.db.query<{ count: number }>(`SELECT count(*) AS count FROM changesets WHERE activated_at >= ? AND activated_at <= ?`)
      .get(utcIso(new Date(now - 86_400_000)), utcIso(new Date(now)))?.count ?? 0;
    if (!Number.isFinite(limit) || count >= limit) throw new FactoryError("RATE_LIMITED", "推广过于频繁", { count });
  }
}
