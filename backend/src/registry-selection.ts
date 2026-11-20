import type { Context } from "./legacy/context";
import { canaryBucket } from "./legacy/change-rollout";
import { canonicalJson } from "./legacy/foundation";
import { readGovernedRegistry } from "./legacy/registry-config";
import { EpochStore } from "./legacy/registry-epochs";
import { RegistrySnapshotStore, type Snapshot } from "./legacy/snapshot";

export class RegistrySelection {
  private git?: Promise<EpochStore>;

  constructor(
    private readonly context: Context, private readonly registry: string,
    private readonly requestedEpoch?: string, private readonly explicitRegistry?: string,
  ) {}

  async current() {
    if (this.requestedEpoch || readGovernedRegistry(this.context.root)) {
      const epochs = await this.openGit(this.requestedEpoch ? this.explicitRegistry : undefined);
      return epochs.materialize(this.requestedEpoch ?? epochs.current());
    }
    return new RegistrySnapshotStore(this.context.db).capture(this.registry);
  }

  async forLine(line: string, inputs: (snapshot: Snapshot) => Record<string, string | string[]>): Promise<Snapshot> {
    if (this.requestedEpoch || !readGovernedRegistry(this.context.root)) return this.current();
    const epochs = await this.openGit();
    const pointers = epochs.pointers();
    const active = await epochs.materialize(pointers.active ?? epochs.current());
    const { db } = this.context;
    if (!pointers.candidate || !db.query("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='changesets'").get()) return active;
    const row = db.query<{ percent: number }>(`SELECT json_extract(document,'$.spec.rollout.canary.percent') AS percent FROM changesets
      WHERE "commit"=? AND base_epoch=? AND state='canary'`).get(pointers.candidate, active.epoch);
    if (!row || !Number.isInteger(row.percent) || row.percent < 1 || row.percent > 100) return active;
    const lineKey = active.key("Line", line);
    if (Object.hasOwn(active.docs, lineKey) && canaryBucket(`${lineKey}:${canonicalJson(inputs(active))}`) >= row.percent) return active;
    const candidate = await epochs.materialize(pointers.candidate);
    return Object.hasOwn(candidate.docs, lineKey) ? candidate : active;
  }

  async saved(epoch: string) {
    if (/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(epoch)) {
      const configured = readGovernedRegistry(this.context.root);
      return (await this.openGit(configured && !this.requestedEpoch ? undefined : this.explicitRegistry)).materialize(epoch);
    }
    return new RegistrySnapshotStore(this.context.db).materialize(epoch, this.registry);
  }

  private openGit(registry?: string): Promise<EpochStore> {
    this.git ??= EpochStore.open(this.context, { registry });
    return this.git;
  }
}
