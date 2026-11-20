import { isAbsolute, resolve } from "node:path";
import type { ContainerProvider } from "../runtime/podman";
import type { BusinessSystem } from "./models";
import type { Context } from "./legacy/context";
import { LineRunner } from "./legacy/line-runner";
import type { Run } from "./legacy/runs";
import { Runner } from "./legacy/runner";
import { loadSnapshot, RegistrySnapshotStore, type Snapshot } from "./legacy/snapshot";

export type BusinessKernelBinding = {
  registryRoot: string;
  line: string;
  /** Optional MAO factory root used by the execution bridge. */
  factoryRoot?: string;
  /** Inputs are legacy Bean ids grouped by the Line interface port. */
  inputs?: Record<string, string | string[]>;
  variants?: Record<string, string>;
  budgetCapUsd?: number;
  useCache?: boolean;
  sinkMode?: "execute" | "dry-run";
};

export type BusinessKernelSnapshot = BusinessKernelBinding & {
  epoch: string;
  snapshot: Snapshot;
  stations: string[];
  machines: string[];
};

export function businessKernelBinding(system: BusinessSystem): BusinessKernelBinding | undefined {
  const value = system.metadata.kernel;
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  if (typeof record.registry_root !== "string" || !isAbsolute(record.registry_root)
    || typeof record.line !== "string" || !record.line.trim()) {
    throw new Error("business system kernel binding requires an absolute registry_root and line");
  }
  const inputs = record.inputs;
  const normalizedInputs: Record<string, string | string[]> | undefined = inputs && typeof inputs === "object" && !Array.isArray(inputs)
    ? Object.fromEntries(Object.entries(inputs).flatMap(([port, value]): Array<[string, string | string[]]> => {
      if (typeof value === "string" && value.trim()) return [[port, value]];
      if (Array.isArray(value) && value.every((item) => typeof item === "string" && item.trim())) return [[port, value as string[]]];
      return [];
    }))
    : undefined;
  const variants = record.variants && typeof record.variants === "object" && !Array.isArray(record.variants)
    ? Object.fromEntries(Object.entries(record.variants).filter(([, value]) => typeof value === "string" && value.trim())) as Record<string, string>
    : undefined;
  const budgetCapUsd = typeof record.budget_cap_usd === "number" && Number.isFinite(record.budget_cap_usd) && record.budget_cap_usd >= 0
    ? record.budget_cap_usd : undefined;
  const sinkMode = record.sink_mode === "dry-run" || record.sink_mode === "execute" ? record.sink_mode : undefined;
  return {
    registryRoot: resolve(record.registry_root), line: record.line.trim(),
    ...(typeof record.factory_root === "string" && isAbsolute(record.factory_root) ? { factoryRoot: resolve(record.factory_root) } : {}),
    ...(normalizedInputs && Object.keys(normalizedInputs).length ? { inputs: normalizedInputs } : {}),
    ...(variants && Object.keys(variants).length ? { variants } : {}),
    ...(budgetCapUsd === undefined ? {} : { budgetCapUsd }),
    ...(typeof record.use_cache === "boolean" ? { useCache: record.use_cache } : {}),
    ...(sinkMode ? { sinkMode } : {}),
  };
}

function kernelFromSnapshot(configured: BusinessKernelBinding, snapshot: Snapshot): BusinessKernelSnapshot {
  const lineKey = snapshot.key("Line", configured.line);
  const line = snapshot.spec(lineKey);
  const stations = new Set<string>();
  const machines = new Set<string>();
  const steps = line?.steps && typeof line.steps === "object" ? Object.values(line.steps) : [];
  for (const step of steps) {
    if (!step || typeof step !== "object" || typeof (step as any).station !== "string") continue;
    const stationKey = snapshot.key("Station", (step as any).station);
    const station = snapshot.spec(stationKey);
    stations.add(stationKey);
    if (typeof station.machine === "string") {
      const machineKey = snapshot.key("Machine", station.machine);
      snapshot.spec(machineKey);
      machines.add(machineKey);
    }
  }
  return { ...configured, line: lineKey, epoch: snapshot.epoch, snapshot, stations: [...stations].sort(), machines: [...machines].sort() };
}

/** Load and validate the MAO Registry facts for a business system. */
export async function loadBusinessKernel(system: BusinessSystem): Promise<BusinessKernelSnapshot | undefined> {
  const configured = businessKernelBinding(system);
  if (!configured) return undefined;
  return kernelFromSnapshot(configured, await loadSnapshot(configured.registryRoot));
}

/** Persist and recover the exact Registry snapshot selected for a BusinessRun. */
export async function captureBusinessKernel(kernel: BusinessKernelSnapshot, factoryRoot: string, clock?: import("./legacy/foundation").Clock): Promise<BusinessKernelSnapshot> {
  const context = (await import("./legacy/context")).Context.open(factoryRoot, { clock });
  try {
    const snapshot = await new RegistrySnapshotStore(context.db).capture(kernel.registryRoot);
    return kernelFromSnapshot(kernel, snapshot);
  } finally { context.close(); }
}

export async function materializeBusinessKernel(system: BusinessSystem, factoryRoot: string, epoch: string, lineOverride?: string): Promise<BusinessKernelSnapshot | undefined> {
  const configured = businessKernelBinding(system);
  if (!configured) return undefined;
  const context = (await import("./legacy/context")).Context.open(factoryRoot);
  try {
    const snapshot = await new RegistrySnapshotStore(context.db).materialize(epoch, configured.registryRoot);
    return kernelFromSnapshot({ ...configured, ...(lineOverride ? { line: lineOverride } : {}) }, snapshot);
  } finally { context.close(); }
}

export function kernelRunMetadata(kernel: BusinessKernelSnapshot): { kernel: Record<string, unknown> } {
  return {
    kernel: {
      registry_root: "registry://configured",
      line: kernel.line,
      epoch: kernel.epoch,
      stations: kernel.stations,
      machines: kernel.machines,
      ...(kernel.inputs ? { inputs: kernel.inputs } : {}),
      ...(kernel.variants ? { variants: kernel.variants } : {}),
      ...(kernel.budgetCapUsd === undefined ? {} : { budget_cap_usd: kernel.budgetCapUsd }),
      ...(kernel.useCache === undefined ? {} : { use_cache: kernel.useCache }),
      ...(kernel.sinkMode ? { sink_mode: kernel.sinkMode } : {}),
      ...(kernel.factoryRoot ? { factory_root: "factory://configured" } : {}),
      ...(kernel.inputs ? { execution: "legacy" } : {}),
    },
  };
}

export type BusinessKernelExecution = {
  /** Start and fully execute the legacy LineRunner run. */
  start(kernel: BusinessKernelSnapshot, onStarted?: (run: Run) => void): Promise<Run>;
  /** Resume a persisted legacy LineRunner run after a control-plane restart. */
  resume(kernel: BusinessKernelSnapshot, runId: string): Promise<Run>;
};

/**
 * Adapts a validated BusinessSystem kernel binding to the existing MAO
 * Runner/LineRunner. The adapter receives an opened Context and official
 * ContainerProvider, so it cannot create a second execution implementation.
 */
export function createBusinessKernelExecution(context: Context, provider: ContainerProvider): BusinessKernelExecution {
  return {
    async start(kernel, onStarted) {
      if (!kernel.inputs || Object.keys(kernel.inputs).length === 0) {
        throw new Error("business kernel execution requires kernel.inputs grouped by Line port");
      }
      const runner = new Runner(context, kernel.snapshot, provider);
      const line = new LineRunner(runner);
      const started = line.start(kernel.line, {
        inputs: kernel.inputs,
        variants: kernel.variants,
        budgetCapUsd: kernel.budgetCapUsd,
        useCache: kernel.useCache,
        sinkMode: kernel.sinkMode,
        trigger: { on: "business-run", by: "mao.business" },
      });
      onStarted?.(started);
      return line.run(started.id);
    },
    async resume(kernel, runId) {
      const runner = new Runner(context, kernel.snapshot, provider);
      return new LineRunner(runner).run(runId);
    },
  };
}
