import type { ContainerProvider } from "../../runtime/podman";
import { selectedContainerProvider } from "../../runtime/selected";
import { object } from "./change-facts";
import { runShadowWith, type ShadowExecution } from "./change-shadow";
import { verificationReject, verifyChangeSet, type VerificationContracts, type VerificationOutcome } from "./change-verification";
import type { ChangeSet, ChangeSetStore } from "./changesets";
import { ContractRunner } from "./contract";
import { FactoryError } from "./errors";
import { canonicalJson, utcIso } from "./foundation";
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
      if (change.state === "verified" || change.state === "shadow") return await this.shadow(change);
      return change;
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
    if (change.state === "shadow" && change.report.ok === true) return change;
    if (change.reviews_required > 0) return change;
    if (!Array.isArray(change.report.stages) || !change.report.stages.includes("shadow")) return change;
    const { epochs, context } = this.store;
    const stale = () => ({ stage: "shadow", ok: false, code: "CHANGESET_STALE", detail: { base_epoch: change.base_epoch, active: epochs.current() } });
    const by = { by: "governance", reason: "shadow" };
    const candidateRef = epochs.pointers().candidate;
    if (change.base_epoch !== epochs.current()) {
      if (candidateRef === change.commit) await epochs.setPointer("candidate", null, by);
      return this.transition(change, "rejected", stale());
    }
    if (candidateRef && candidateRef !== change.commit) return change;
    const active = await epochs.materialize(change.base_epoch);
    const candidate = await epochs.materialize(change.commit);
    const rollout = object(object(change.document.spec).rollout);
    const shadow = object(rollout.shadow);
    const goldenOnly = change.risk_tier === "T1" && shadow.runs === undefined;
    const claimed = context.db.transaction(() => {
      if (epochs.current() !== change.base_epoch) return this.transition(change, "rejected", stale());
      const other = context.db.query("SELECT id FROM changesets WHERE state = 'shadow' AND id <> ? LIMIT 1").get(change.id);
      const pointer = epochs.pointers().candidate;
      if (other || (pointer && pointer !== change.commit)) return undefined;
      return change.state === "shadow" ? change : this.transition(change, "shadow", { ...change.report, stage: "shadow", ok: null, code: null });
    }).immediate();
    if (!claimed || claimed.state === "rejected") return claimed ?? change;
    change = claimed;
    await epochs.setPointer("candidate", change.commit, by);
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
    if (change.base_epoch !== epochs.current()) {
      if (epochs.pointers().candidate === change.commit) await epochs.setPointer("candidate", null, by);
      return this.transition(change, "rejected", stale());
    }
    if (!ok && epochs.pointers().candidate === change.commit) await epochs.setPointer("candidate", null, by);
    return this.transition(change, ok ? "shadow" : "rejected", { ...change.report, stage: "shadow", ok, code: ok ? null : "SHADOW_FAILED", detail });
  }

  private runner = async (snapshot: Snapshot): Promise<Runner> => {
    const provider = this.options.provider ? await this.options.provider() : selectedContainerProvider();
    return new Runner(this.store.context, snapshot, provider);
  };

  private async contracts(snapshot: Snapshot): Promise<VerificationContracts> {
    return this.options.contracts ? this.options.contracts(snapshot) : new ContractRunner(await this.runner(snapshot));
  }

  private transition(current: ChangeSet, to: string, report: Data, outcome?: VerificationOutcome): ChangeSet {
    const { db, clock, ids } = this.store.context;
    return db.transaction(() => {
      const at = utcIso(clock.now());
      if (this.store.get(current.id).state !== current.state) throw new FactoryError("CHANGESET_STATE_INVALID", "变更已被其他进程推进", { id: current.id });
      db.query(`UPDATE changesets SET state = ?, report = ?, updated_at = ?, risk_tier = ?, reviews_required = ?, proposer_runtime = ? WHERE id = ?`)
        .run(to, canonicalJson(report), at, outcome?.tier ?? current.risk_tier, outcome?.reviews ?? current.reviews_required,
          outcome?.runtime === undefined ? current.proposer_runtime : outcome.runtime, current.id);
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
