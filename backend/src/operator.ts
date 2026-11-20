import type { EventBus } from "./event-bus";
import type { SqliteStore } from "./db";
import { randomUUID } from "node:crypto";
import { AcpTransport, defaultAcpSpawn, type AcpSpawn } from "./acp";
import { OfficialCliTransport, chooseTransport, harnessEndpointEnv, resolveOfficialHarness, type HarnessEvent, type HarnessSession, type OfficialSpawn } from "./harness";

export interface OperatorCoordinatorOptions {
  store: SqliteStore;
  events: EventBus;
  cwd?: string;
  spawn?: OfficialSpawn;
  acpSpawn?: AcpSpawn;
  onProgress?: (runId: string) => void;
  ensureRuntime?: () => Promise<void>;
  readSecret?: (ref: string) => string | undefined;
}

export function projectCredentialName(name: unknown): name is string {
  return typeof name === "string" && /^[A-Z][A-Z0-9_]{0,95}$/.test(name)
    && !/^(?:PATH|HOME|USERPROFILE|SYSTEMROOT|COMSPEC|SHELL|TMP|TEMP|TMPDIR|LD_.*|DYLD_.*|NODE_.*|BUN_.*|MAO_.*|GIT_.*|DOCKER_.*|PODMAN_.*|KIRI_.*)$/.test(name);
}

function redactCredentialValues(value: unknown, secrets: string[]): unknown {
  if (typeof value === "string") return secrets.reduce((text, secret) => text.replaceAll(secret, "[REDACTED]"), value);
  if (Array.isArray(value)) return value.map((item) => redactCredentialValues(item, secrets));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, redactCredentialValues(item, secrets)]));
  return value;
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
    const work = this.run(runId, unitRun.id).finally(() => {
      this.active.delete(unitRun.id);
      if (!this.closed) this.options.onProgress?.(runId);
    });
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
    if (!unit || unit.kind !== "operator" || !unit.projectId) {
      this.options.store.completeUnitRun(unitRun.id, "blocked", [], "operator-project-unavailable");
      return;
    }
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
    const useAcp = chooseTransport(definition, Boolean(definition.id === "kimi-code" || this.options.acpSpawn || process.env.MAO_ACP_AVAILABLE === "1")) === "acp";
    const transport = useAcp
      ? new AcpTransport(definition, this.options.acpSpawn ?? this.options.spawn ?? defaultAcpSpawn)
      : new OfficialCliTransport(definition, this.options.spawn);
    let session: HarnessSession | undefined;
    const outputIds = new Set(unitRun.outputs);
    const credentials: Record<string, string> = Object.create(null);
    const requested = new Set<string>();
    try {
      if (profile.image) await this.options.ensureRuntime?.();
      for (const resource of this.options.store.unitSecretRequests(unitRun.id)) {
        if (resource.projectId !== unit.projectId || resource.status !== "available" || !projectCredentialName(resource.name)) continue;
        const value = this.options.readSecret?.(resource.vaultRef);
        if (!value) throw new Error("project-credential-vault-entry-missing");
        credentials[resource.name] = value;
      }
      session = await transport.start({
        task: this.taskFor(unit, runId, unitRun),
        contract: "你是业务系统 Operator。只使用官方 Harness 能力处理项目。每个可交付结果必须额外输出一行 JSONL，格式为 {\"type\":\"mao.record\",\"record_type\":\"material\",\"kind\":\"...\",\"ref\":\"...\",\"summary\":\"面向人的说明\",\"files\":[{\"ref\":\"...\",\"path\":\"...\"}]}。文件字段只放引用，不嵌入文件内容。缺少项目资源时输出 {\"type\":\"mao.secret_request\",\"name\":\"...\",\"purpose\":\"...\",\"scope\":\"...\"}，由项目资源请求流程向用户呈现。",
        model: profile.model,
        env: { ...credentials, ...harnessEndpointEnv(definition, profile.baseUrl) },
        cwd: this.cwd,
        containerImage: profile.image,
        network: profile.network === "egress" ? "egress" : "none",
      });
      this.sessions.set(unitRun.id, session);
      if (useAcp) this.options.store.noteUnitSession(unitRun.id, session.id);
      const secretValues = Object.values(credentials);
      let failed = false;
      for await (const emitted of session.events()) {
        const event = redactCredentialValues(emitted, secretValues) as HarnessEvent;
        if (event.sessionId) this.options.store.noteUnitSession(unitRun.id, event.sessionId);
        if (event.type === "record") {
          const artifact = this.recordFromEvent(event, unit.projectId, unit, definition.id);
          if (artifact) {
            outputIds.add(artifact.ref);
            this.options.events.publish("artifact.created", artifact);
          }
        }
        if (event.type === "secret.request") {
          const metadata = event.metadata && typeof event.metadata === "object" ? event.metadata as Record<string, unknown> : {};
          const name = metadata.name;
          if (projectCredentialName(name) && !Object.hasOwn(credentials, name)) {
            const request = this.options.store.createSecretRequest({ projectId: unit.projectId, name, vaultRef: `vault://secret/${randomUUID()}`, metadata: { purpose: metadata.purpose, scope: metadata.scope, checks: metadata.checks } });
            this.options.store.linkUnitSecret(unitRun.id, request.id);
            if (!requested.has(request.id) && request.status !== "available") {
              this.options.events.publish("secret.requested", { id: request.id, projectId: request.projectId, name: request.name, purpose: request.metadata.purpose, scope: request.metadata.scope });
            }
            requested.add(request.id);
          } else {
            failed = true;
          }
        }
        const payload = { runId, unitRunId, unitId: unit.id, sessionId: event.sessionId ?? session.id, harness: definition.id, text: event.text, metadata: event.metadata };
        this.options.events.publish(`operator.session.${event.type}`, payload);
        if (event.type === "session.failed") failed = true;
      }
      if (this.closed) return;
      if (requested.size) {
        const waiting = this.options.store.completeUnitRun(unitRun.id, "waiting_resources", [...outputIds]);
        if (waiting) this.options.events.publish("operator.run.waiting_resources", waiting);
        this.options.store.resumeResourceRuns(requested.values().next().value!);
        return;
      }
      const completed = this.options.store.completeUnitRun(unitRun.id, failed ? "failed" : "completed", [...outputIds], failed ? "official-harness-failed" : undefined);
      if (completed) this.options.events.publish("operator.run.completed", completed);
    } catch (error) {
      if (this.closed) return;
      const message = redactCredentialValues(error instanceof Error ? error.message : "official-harness-failed", Object.values(credentials)) as string;
      const completed = this.options.store.completeUnitRun(unitRun.id, "failed", [...outputIds], message);
      if (completed) this.options.events.publish("operator.run.failed", completed);
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
