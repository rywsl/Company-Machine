import type { Context } from "./legacy/context";
import { readGovernedRegistry } from "./legacy/registry-config";
import { EpochStore } from "./legacy/registry-epochs";
import { RegistrySnapshotStore } from "./legacy/snapshot";

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
