import type { EventBus } from "./event-bus";
import type { SqliteStore } from "./db";
import type { PrepareProjectSources } from "./project-source-mounts";
import type { Project } from "./models";

export interface MachineExecutorOptions {
  store: SqliteStore;
  events: EventBus;
  /**
   * Prepare the project boundary before the Operator starts. The default
   * server implementation validates local and uploaded sources without
   * cloning repositories. Tests and deployments may provide a stricter
   * preparation function.
   */
  prepareSources?: PrepareProjectSources;
  /** Records a portable source activation that downstream Operators can consume. */
  onActivated?: (project: Project, facts: readonly Record<string, unknown>[], machineId: string) => string[];
  onProgress?: (runId: string, completedUnitRunId: string) => void;
}

/** Executes the ordinary, source-backed Machine unit in a durable run. */
export class MachineExecutor {
  private readonly active = new Map<string, Promise<void>>();
  private readonly activeRuns = new Map<string, string>();
  private readonly cancelledRuns = new Set<string>();
  private stopped = false;

  constructor(private readonly options: MachineExecutorOptions) {}

  dispatch(runId: string, unitRunId: string, recovering = false): Promise<void> {
    if (this.stopped) return Promise.resolve();
    this.cancelledRuns.delete(runId);
    const existing = this.active.get(unitRunId);
    if (existing) return existing;
    const work = this.run(runId, unitRunId, recovering).finally(() => {
      this.active.delete(unitRunId);
      this.activeRuns.delete(unitRunId);
    });
    this.active.set(unitRunId, work);
    this.activeRuns.set(unitRunId, runId);
    return work;
  }

  async cancelRun(runId: string): Promise<void> {
    this.cancelledRuns.add(runId);
    await Promise.allSettled([...this.active.entries()]
      .filter(([unitRunId]) => this.activeRuns.get(unitRunId) === runId)
      .map(([, work]) => work));
  }

  async close(): Promise<void> {
    await Promise.allSettled(this.active.values());
  }

  stop(): void {
    this.stopped = true;
  }

  private async run(runId: string, unitRunId: string, recovering: boolean): Promise<void> {
    const { store, events } = this.options;
    if (this.cancelledRuns.has(runId)) return;
    const unitRun = store.getUnitRun(runId, unitRunId);
    const run = store.getBusinessRun(runId);
    const unit = unitRun ? store.getBusinessUnit(unitRun.unitId) : undefined;
    if (!unitRun || !run || !unit || unit.kind !== "machine" || unitRun.status !== "running") return;

    const sessionId = unitRun.sessionId ?? `machine:${unitRun.id}`;
    const attempt = store.startMachineAttempt(unitRun.id, sessionId, recovering);
    if (!attempt) return;
    events.publish("machine.run.claimed", {
      runId,
      unitRunId,
      machineId: unit.id,
      sessionId,
      attempt: attempt.attempt,
      recovering,
    });
    try {
      if (!unit.projectId) throw new Error("machine-project-unavailable");
      const project = store.getProject(unit.projectId);
      if (!project || project.businessSystemId !== unit.businessSystemId) throw new Error("machine-project-unavailable");
      let sourceFacts: readonly Record<string, unknown>[] = [];
      if (this.options.prepareSources) {
        const prepared = await this.options.prepareSources(project);
        try {
          sourceFacts = prepared?.facts ?? [];
          if (this.stopped || this.cancelledRuns.has(runId)) return;
        } finally {
          prepared?.close();
        }
      }
      if (this.stopped || this.cancelledRuns.has(runId)) return;
      const outputs = this.options.onActivated?.(project, sourceFacts, unit.id) ?? [];
      const completed = store.completeUnitRun(unitRun.id, "completed", outputs, undefined, attempt.ownerToken);
      store.finishMachineAttempt(unitRun.id, attempt.attempt, completed ? "completed" : "failed", completed ? undefined : "machine-run-already-finished");
      if (!completed) return;
      events.publish("machine.run.completed", {
        runId,
        unitRunId,
        machineId: unit.id,
        state: "SUCCEEDED",
        attempt: attempt.attempt,
        outputs: completed.outputs,
      });
      this.options.onProgress?.(runId, completed.id);
    } catch (error) {
      if (this.stopped || this.cancelledRuns.has(runId)) return;
      const message = error instanceof Error ? error.message : String(error);
      const failed = store.completeUnitRun(unitRun.id, "failed", [], message, attempt.ownerToken);
      store.finishMachineAttempt(unitRun.id, attempt.attempt, failed ? "failed" : "completed", failed ? message : undefined);
      if (!failed) return;
      events.publish("machine.run.failed", {
        runId,
        unitRunId,
        machineId: unit.id,
        attempt: attempt.attempt,
        error: message,
      });
      this.options.onProgress?.(runId, failed.id);
    }
  }
}
