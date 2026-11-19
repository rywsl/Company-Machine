import type { EventBus } from "./event-bus";
import type { SqliteStore } from "./db";
import { randomUUID } from "node:crypto";
import { AcpTransport, type AcpSpawn } from "./acp";
import { OfficialCliTransport, chooseTransport, resolveOfficialHarness, type HarnessEvent, type HarnessSession, type OfficialSpawn } from "./harness";

export interface OperatorCoordinatorOptions {
  store: SqliteStore;
  events: EventBus;
  cwd?: string;
  spawn?: OfficialSpawn;
  acpSpawn?: AcpSpawn;
  onProgress?: (runId: string) => void;
}

/** Runs a business-system operator through an external official Harness. */
export class OperatorCoordinator {
  private readonly active = new Map<string, Promise<void>>();
  private readonly sessions = new Map<string, HarnessSession>();
  private closed = false;
  private readonly cwd: string;

  constructor(private readonly options: OperatorCoordinatorOptions) {
    this.cwd = options.cwd ?? process.cwd();
  }

  dispatch(runId: string, unitRunId?: string): Promise<void> {
    const unitRun = unitRunId
      ? this.options.store.listUnitRuns(runId).find((item) => item.id === unitRunId)
      : this.options.store.listUnitRuns(runId).find((item) => item.status === "queued");
    if (!unitRun) return Promise.resolve();
    const existing = this.active.get(unitRun.id);
    if (existing) return existing;
    const work = this.run(runId, unitRun.id).finally(() => this.active.delete(unitRun.id));
    this.active.set(unitRun.id, work);
    return work;
  }

  stop(): void {
    this.closed = true;
    for (const session of this.sessions.values()) void session.close();
  }

  async close(): Promise<void> {
    this.stop();
    await Promise.allSettled([...this.sessions.values()].map((session) => session.close()));
    await Promise.allSettled(this.active.values());
  }

  private async run(runId: string, unitRunId: string): Promise<void> {
    if (this.closed) return;
    const unitRun = this.options.store.listUnitRuns(runId).find((item) => item.id === unitRunId);
    if (!unitRun || unitRun.status !== "queued") return;
    const unit = this.options.store.getBusinessUnit(unitRun.unitId);
    if (!unit || unit.kind !== "operator" || !unit.projectId) return;
    const profile = this.options.store.listHarnessProfiles().find((item) => item.role === "operator" && (!unit.harnessRuntime || item.runtime.toLowerCase().includes(unit.harnessRuntime.toLowerCase())));
    const definition = profile ? resolveOfficialHarness(profile.runtime) : undefined;
    if (!profile || !definition) {
      this.options.store.completeUnitRun(unitRun.id, "blocked", [], "operator-harness-not-configured");
      this.options.events.publish("operator.run.blocked", { runId, unitRunId, reason: "operator-harness-not-configured" });
      return;
    }
    const sessionId = `${definition.id}:${unitRun.id}`;
    const claimed = this.options.store.claimUnitRun(runId, unit.id, sessionId);
    if (!claimed || claimed.status !== "running") return;
    this.options.events.publish("operator.run.claimed", { runId, unitRunId, unitId: unit.id, sessionId, harness: definition.id });
    const useAcp = chooseTransport(definition, Boolean(this.options.acpSpawn || process.env.MAO_ACP_AVAILABLE === "1")) === "acp";
    const transport = useAcp
      ? new AcpTransport(definition, this.options.acpSpawn ?? ((argv, spawnOptions) => {
        if (!this.options.spawn) throw new Error("ACP spawn is unavailable");
        return this.options.spawn(argv, { cwd: spawnOptions.cwd, env: spawnOptions.env, stdout: "pipe", stdin: "pipe", containerImage: spawnOptions.containerImage });
      }))
      : new OfficialCliTransport(definition, this.options.spawn);
    let session: HarnessSession | undefined;
    const outputIds: string[] = [];
    try {
      session = await transport.start({
        task: this.taskFor(unit, runId, unitRun),
        contract: "你是业务系统 Operator。只使用官方 Harness 能力处理项目。每个可交付结果必须额外输出一行 JSONL，格式为 {\"type\":\"mao.record\",\"record_type\":\"material\",\"kind\":\"...\",\"ref\":\"...\",\"summary\":\"面向人的说明\",\"files\":[{\"ref\":\"...\",\"path\":\"...\"}]}。文件字段只放引用，不嵌入文件内容。缺少项目资源时输出 {\"type\":\"mao.secret_request\",\"name\":\"...\",\"purpose\":\"...\",\"scope\":\"...\"}，由项目资源请求流程向用户呈现。",
        model: profile.model,
        cwd: this.cwd,
        resumeId: sessionId,
        containerImage: profile.image,
      });
      this.sessions.set(unitRun.id, session);
      let failed = false;
      for await (const event of session.events()) {
        if (event.type === "record") {
          const artifact = this.recordFromEvent(event, unit.projectId, unit, definition.id);
          if (artifact) {
            outputIds.push(artifact.ref);
            this.options.events.publish("artifact.created", artifact);
          }
        }
        if (event.type === "secret.request") {
          const metadata = event.metadata && typeof event.metadata === "object" ? event.metadata as Record<string, unknown> : {};
          const name = typeof metadata.name === "string" ? metadata.name : undefined;
          if (name) {
            const request = this.options.store.createSecretRequest({ projectId: unit.projectId, name, vaultRef: `vault://secret/${randomUUID()}`, metadata: { purpose: metadata.purpose, scope: metadata.scope, checks: metadata.checks } });
            this.options.events.publish("secret.requested", { id: request.id, projectId: request.projectId, name: request.name, purpose: request.metadata.purpose, scope: request.metadata.scope });
          }
        }
        const payload = { runId, unitRunId, unitId: unit.id, sessionId: event.sessionId ?? session.id, harness: definition.id, text: event.text, metadata: event.metadata };
        this.options.events.publish(`operator.session.${event.type}`, payload);
        if (event.type === "session.failed") failed = true;
      }
      if (this.closed) return;
      const completed = this.options.store.completeUnitRun(unitRun.id, failed ? "failed" : "completed", outputIds, failed ? "official-harness-failed" : undefined);
      if (completed) this.options.events.publish("operator.run.completed", completed);
      this.options.onProgress?.(runId);
    } catch (error) {
      const completed = this.options.store.completeUnitRun(unitRun.id, "failed", outputIds, error instanceof Error ? error.message : "official-harness-failed");
      if (completed) this.options.events.publish("operator.run.failed", completed);
      this.options.onProgress?.(runId);
    } finally {
      this.sessions.delete(unitRun.id);
      await session?.close();
    }
  }

  private taskFor(unit: { name: string; projectId?: string; machineRef?: string }, runId: string, unitRun: { inputs: string[] }): string {
    return `执行业务运行 ${runId}。Operator ${unit.name} 负责项目 ${unit.projectId ?? "unknown"}，机器来源 ${unit.machineRef ?? "由项目配置决定"}。输入物料引用 ${unitRun.inputs.join(", ") || "无"}。完成后输出可交付物并说明结果。`;
  }

  private recordFromEvent(event: HarnessEvent, projectId: string, unit: { id: string; machineRef?: string }, harness: string) {
    const metadata = event.metadata && typeof event.metadata === "object" ? event.metadata as Record<string, unknown> : {};
    const value = metadata.record && typeof metadata.record === "object" ? metadata.record as Record<string, unknown> : metadata;
    if (typeof value.kind !== "string" || typeof value.ref !== "string" || typeof value.summary !== "string") return undefined;
    return this.options.store.addArtifact({
      projectId,
      recordType: value.record_type === "artifact" ? "artifact" : "material",
      kind: value.kind,
      ref: value.ref,
      title: typeof value.title === "string" ? value.title : undefined,
      description: typeof value.description === "string" ? value.description : undefined,
      summary: value.summary,
      purpose: typeof value.purpose === "string" ? value.purpose : undefined,
      status: typeof value.status === "string" ? value.status : "produced",
      producer: { operator: unit.id, machine: unit.machineRef, harness },
      files: Array.isArray(value.files) ? value.files as never : undefined,
      inputs: Array.isArray(value.inputs) ? value.inputs.filter((item): item is string => typeof item === "string") : undefined,
      outputs: Array.isArray(value.outputs) ? value.outputs.filter((item): item is string => typeof item === "string") : undefined,
      lineage: Array.isArray(value.lineage) ? value.lineage.filter((item): item is string => typeof item === "string") : undefined,
      metadata: value,
    });
  }
}
