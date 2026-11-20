import type { ContainerProvider } from "../../runtime/podman";
import { selectedContainerProvider } from "../../runtime/selected";
import { governanceLimits, object } from "./change-facts";
import { runMetrics, sloViolation } from "./change-rollout";
import { runShadowWith, type ShadowExecution } from "./change-shadow";
import { verificationReject, verifyChangeSet, type VerificationContracts, type VerificationOutcome } from "./change-verification";
import type { ChangeSet, ChangeSetStore } from "./changesets";
import { ContractRunner } from "./contract";
import { FactoryError } from "./errors";
import { canonicalJson, parseDuration, utcIso } from "./foundation";
import { Runner } from "./runner";
import type { Snapshot } from "./snapshot";

type Data = Record<string, unknown>;
type Options = {
  provider?: () => ContainerProvider | Promise<ContainerProvider>;
  contracts?: (snapshot: Snapshot) => VerificationContracts | Promise<VerificationContracts>;
  shadow?: ShadowExecution;
  timeoutSeconds?: number;
};

export class ChangeSetStages {
  constructor(readonly store: ChangeSetStore, readonly options: Options = {}) {
    store.context.db.exec(`CREATE TABLE IF NOT EXISTS changeset_stage_locks (id TEXT PRIMARY KEY, pid INTEGER NOT NULL)`);
  }

  async advance(id: string): Promise<ChangeSet> {
    this.lock(id);
    try {
      let change = this.store.get(id);
      if (change.state === "draft") {
        change = this.transition(change, "verifying", { stage: "verifying", ok: null, code: null, detail: {} });
      }
      if (change.state === "verifying") return await this.verify(change);
      if (change.state === "verified" || (change.state === "shadow" && change.report.ok !== true)) return await this.shadow(change);
      if (change.state === "shadow") return await this.afterShadow(change);
      if (change.state === "canary") return await this.canary(change);
      if (change.state === "active") return await this.observe(change);
      if (change.state === "rolled-back" || change.state === "rejected") await this.store.epochs.syncRefs();
      throw new FactoryError("CHANGESET_STATE_INVALID", "当前状态不能推进", { id, state: change.state });
    } finally {
      this.store.context.db.query("DELETE FROM changeset_stage_locks WHERE id = ? AND pid = ?").run(id, process.pid);
    }
  }

  private async verify(change: ChangeSet): Promise<ChangeSet> {
    const { epochs, context } = this.store;
    const base = await epochs.materialize(change.base_epoch);
    let candidate: Snapshot;
    try { candidate = await epochs.materialize(change.commit); }
    catch (error) {
      if (error instanceof FactoryError && error.code === "REGISTRY_INVALID") {
        const rejected = verificationReject(error.code, { diags: error.detail.diags ?? [] });
        return this.transition(change, rejected.state, rejected.report);
      }
      throw error;
    }
    const outcome = await verifyChangeSet(context, change, base, candidate, () => this.contracts(candidate), this.options.timeoutSeconds ?? 60);
    return this.transition(change, outcome.state, outcome.report, outcome);
  }

  private async shadow(change: ChangeSet): Promise<ChangeSet> {
    const { epochs, context } = this.store;
    const candidateRef = epochs.pointers().candidate;
    if (change.base_epoch !== epochs.current()) {
      const rejected = context.db.transaction(() => this.rejectStale(change)).immediate();
      await epochs.syncRefs();
      return rejected;
    }
    if (candidateRef && candidateRef !== change.commit) return this.wait(change, "queued");
    const active = await epochs.materialize(change.base_epoch);
    const candidate = await epochs.materialize(change.commit);
    const rollout = object(object(change.document.spec).rollout);
    const shadow = object(rollout.shadow);
    const goldenOnly = change.risk_tier === "T1" && shadow.runs === undefined;
    const claimed = context.db.transaction(() => {
      if (epochs.current() !== change.base_epoch) return { change: this.rejectStale(change), run: false };
      if (change.reviews_required > 0 && this.store.approvals(change.id, candidate).length < change.reviews_required) {
        return { change: this.wait(change, "reviews"), run: false };
      }
      const count = context.db.query<{ count: number }>(`SELECT count(*) AS count FROM changesets
        WHERE id <> ? AND (state IN ('shadow','canary') OR
          (state='active' AND NOT (json_extract(report,'$.stage') IS 'observe' AND json_extract(report,'$.ok') IS 1)))`).get(change.id)?.count ?? 0;
      const pointer = epochs.pointers().candidate;
      if (count >= governanceLimits().concurrent_rollouts || (pointer && pointer !== change.commit)) return { change: this.wait(change, "queued"), run: false };
      if (!epochs.updateRolloutPointers(change.base_epoch, change.commit, "stage")) return { change: this.rejectStale(change), run: false };
      return { change: change.state === "shadow" ? change : this.transition(change, "shadow", { ...change.report, stage: "shadow", ok: null, code: null }), run: true };
    }).immediate();
    if (!claimed.run) {
      if (claimed.change.state === "rejected") await epochs.syncRefs();
      return claimed.change;
    }
    change = claimed.change;
    await epochs.syncRefs();
    const run = this.options.shadow ?? runShadowWith(this.runner, (snapshot) => this.contracts(snapshot));
    let detail: Data;
    let ok = false;
    try {
      const result = await run(change, candidate, active, goldenOnly);
      detail = result;
      ok = result.ok && Number.isFinite(result.spent_usd) && result.spent_usd >= 0 &&
        result.spent_usd <= Number(shadow.budget_usd ?? 0) && result.regressions.length === 0;
    } catch (error) {
      if (!(error instanceof FactoryError)) throw error;
      detail = { cause: error.code };
    }
    const result = context.db.transaction(() => {
      if (change.base_epoch !== epochs.current()) return this.rejectStale(change);
      if (!ok) epochs.updateRolloutPointers(change.base_epoch, change.commit, "discard");
      return this.transition(change, ok ? "shadow" : "rejected", { ...change.report, stage: "shadow", ok, code: ok ? null : "SHADOW_FAILED", detail });
    }).immediate();
    if (result.state === "rejected") await epochs.syncRefs();
    return result;
  }

  private wait(change: ChangeSet, reason: string): ChangeSet {
    if (change.report.reason === reason) return change;
    const { db, clock } = this.store.context;
    return db.transaction(() => {
      if (this.store.get(change.id).state !== change.state) throw new FactoryError("CHANGESET_STATE_INVALID", "变更已被其他进程推进", { id: change.id });
      db.query("UPDATE changesets SET report=?, updated_at=? WHERE id=?")
        .run(canonicalJson({ ...change.report, reason }), utcIso(clock.now()), change.id);
      return this.store.get(change.id);
    }).immediate();
  }

  private async afterShadow(change: ChangeSet): Promise<ChangeSet> {
    if (this.store.freezeUntil()) return this.wait(change, "freeze");
    if (!Array.isArray(change.report.stages) || !change.report.stages.includes("canary")) return this.completeRollout(change, "promote");
    const { db } = this.store.context;
    const canary = db.transaction(() => {
      const pointers = this.store.epochs.pointers();
      if (pointers.active !== change.base_epoch || pointers.candidate !== change.commit) {
        return this.rejectStale(change);
      }
      return this.transition(change, "canary", { ...change.report, stage: "canary", ok: null, code: null, detail: {} }, undefined, "stage");
    }).immediate();
    await this.store.epochs.syncRefs();
    return canary;
  }

  private async canary(change: ChangeSet): Promise<ChangeSet> {
    const rollout = object(object(change.document.spec).rollout);
    const minimum = Number(object(rollout.canary).min_runs ?? 1) * Number(change.report.multiplier ?? 1);
    const metrics = runMetrics(this.store.context, change.commit, change.stage_started_at ?? change.updated_at);
    if (metrics.runs < minimum) return this.wait(change, "samples");
    const violation = sloViolation(metrics, object(rollout.slo));
    if (violation) return this.completeRollout(change, "discard", violation);
    if (this.store.freezeUntil()) return this.wait(change, "freeze");
    return this.completeRollout(change, "promote");
  }

  private async observe(change: ChangeSet): Promise<ChangeSet> {
    // Retry publishing durable SQLite pointers after an interrupted ref update.
    await this.store.epochs.syncRefs();
    if (change.report.stage === "observe" && change.report.ok === true) return change;
    const rollout = object(object(change.document.spec).rollout);
    const multiplier = Number(change.report.multiplier ?? 1);
    const minimum = rollout.canary === undefined ? 1 : Number(object(rollout.canary).min_runs ?? 1) * multiplier;
    const started = change.activated_at ?? change.updated_at;
    const elapsedMs = this.store.context.clock.now().getTime() - Date.parse(started);
    const finished = elapsedMs >= parseDuration(String(rollout.observe ?? "1h")) * multiplier * 1_000;
    const metrics = runMetrics(this.store.context, change.commit, started);
    const violation = sloViolation(metrics, object(rollout.slo));
    if (violation && (finished || metrics.runs >= minimum)) return this.completeRollout(change, "rollback", violation);
    if (!finished) return change;
    return this.transition(change, "active", { ...change.report, stage: "observe", ok: true, code: null, detail: { metrics } });
  }

  private rejectStale(change: ChangeSet): ChangeSet {
    this.store.epochs.updateRolloutPointers(change.base_epoch, change.commit, "discard");
    return this.transition(change, "rejected", { ...change.report, ok: false, code: "CHANGESET_STALE",
      detail: { base_epoch: change.base_epoch, active: this.store.epochs.current() } });
  }

  private async completeRollout(change: ChangeSet, outcome: "promote" | "discard" | "rollback", violation?: Data): Promise<ChangeSet> {
    const { epochs, context } = this.store;
    const completed = context.db.transaction(() => {
      if (!epochs.updateRolloutPointers(change.base_epoch, change.commit, outcome)) return this.rejectStale(change);
      if (outcome === "promote") {
        return this.transition(change, "active", { ...change.report, stage: "active", ok: true, code: null, detail: {} }, undefined, "active");
      }
      return this.transition(change, "rolled-back", { ...change.report, ok: false, code: "SLO_ROLLBACK", detail: violation ?? {} });
    }).immediate();
    await epochs.syncRefs();
    return completed;
  }

  private runner = async (snapshot: Snapshot): Promise<Runner> => {
    const provider = this.options.provider ? await this.options.provider() : selectedContainerProvider();
    return new Runner(this.store.context, snapshot, provider);
  };

  private async contracts(snapshot: Snapshot): Promise<VerificationContracts> {
    return this.options.contracts ? this.options.contracts(snapshot) : new ContractRunner(await this.runner(snapshot));
  }

  private transition(current: ChangeSet, to: string, report: Data, outcome?: VerificationOutcome, stamp?: "stage" | "active"): ChangeSet {
    const { db, clock, ids } = this.store.context;
    return db.transaction(() => {
      const at = utcIso(clock.now());
      if (this.store.get(current.id).state !== current.state) throw new FactoryError("CHANGESET_STATE_INVALID", "变更已被其他进程推进", { id: current.id });
      delete report.reason;
      db.query(`UPDATE changesets SET state = ?, report = ?, updated_at = ?, risk_tier = ?, reviews_required = ?,
        proposer_runtime = ?, stage_started_at = ?, activated_at = ? WHERE id = ?`)
        .run(to, canonicalJson(report), at, outcome?.tier ?? current.risk_tier, outcome?.reviews ?? current.reviews_required,
          outcome?.runtime === undefined ? current.proposer_runtime : outcome.runtime,
          stamp ? at : current.stage_started_at, stamp === "active" ? at : current.activated_at, current.id);
      db.query("INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'changeset.state', NULL, ?)")
        .run(ids.new("ev"), at, canonicalJson({ id: current.id, from: current.state, to, code: report.code ?? null }));
      return this.store.get(current.id);
    }).immediate();
  }

  private lock(id: string): void {
    const { db } = this.store.context;
    db.transaction(() => {
      const lock = db.query<{ pid: number }>("SELECT pid FROM changeset_stage_locks WHERE id = ?").get(id);
      if (lock) {
        try {
          process.kill(lock.pid, 0);
          throw new FactoryError("CHANGESET_STATE_INVALID", "变更已有推进进程", { id });
        } catch (error) {
          if ((error as { code?: unknown }).code !== "ESRCH") throw error;
        }
      }
      db.query("INSERT OR REPLACE INTO changeset_stage_locks (id, pid) VALUES (?, ?)").run(id, process.pid);
    }).immediate();
  }
}
