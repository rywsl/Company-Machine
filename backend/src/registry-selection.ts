import { join } from "node:path";
import type { Context } from "./legacy/context";
import { EpochStore } from "./legacy/registry-epochs";
import { RegistrySnapshotStore } from "./legacy/snapshot";

export async function hasGovernedRegistry(root: string): Promise<boolean> {
  try {
    const config = JSON.parse(await Bun.file(join(root, "factory.json")).text());
    return typeof config?.governance?.registry === "string" && config.governance.registry.length > 0;
  } catch (error) {
    if ((error as { code?: string }).code === "ENOENT" || error instanceof SyntaxError) return false;
    throw error;
  }
}

export class RegistrySelection {
  private git?: Promise<EpochStore>;

  constructor(
    private readonly context: Context, private readonly registry: string,
    private readonly requestedEpoch?: string, private readonly explicitRegistry?: string,
  ) {}

  async current() {
    if (this.requestedEpoch || await hasGovernedRegistry(this.context.root)) {
      const epochs = await this.openGit(this.requestedEpoch ? this.explicitRegistry : undefined);
      return epochs.materialize(this.requestedEpoch ?? epochs.current());
    }
    return new RegistrySnapshotStore(this.context.db).capture(this.registry);
  }

  async saved(epoch: string) {
    if (/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(epoch)) {
      const configured = await hasGovernedRegistry(this.context.root);
      return (await this.openGit(configured && !this.requestedEpoch ? undefined : this.explicitRegistry)).materialize(epoch);
    }
    return new RegistrySnapshotStore(this.context.db).materialize(epoch, this.registry);
  }

  private openGit(registry?: string): Promise<EpochStore> {
    this.git ??= EpochStore.open(this.context, { registry });
    return this.git;
  }
}
