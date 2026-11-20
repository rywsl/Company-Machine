import type { EventBus } from "./event-bus";
import type { ArtifactFileGrant, SqliteStore } from "./db";
import type { Artifact, BusinessUnit, UnitRun } from "./models";
import { materialOutputRoot, type CollectedMaterialFile, type createMaterialFileStage, type StagedMaterialFile } from "./material-files";
import { projectSourceFacts } from "./project-sources";
import type { PrepareProjectSources, PreparedProjectSources } from "./project-source-mounts";
import { AcpTransport, defaultAcpSpawn, type AcpSpawn } from "./acp";
import { OfficialCliTransport, requireDeclaredTransport, harnessControlPlaneRoot, harnessEndpointEnv, type HarnessEvent, type OfficialSpawn } from "./harness";
import { HarnessTurn } from "./harness-turn";
import { resolveOperatorBinding } from "./operator-binding";
import { createUnitRunOwner, unitRunOwnerExited, unitRunProcessScope } from "./unit-run-owner";

export interface OperatorCoordinatorOptions {
  store: SqliteStore;
  events: EventBus;
  cwd?: string;
  spawn?: OfficialSpawn;
  acpSpawn?: AcpSpawn;
  onProgress?: (runId: string, completedUnitRunId?: string) => void;
  ensureRuntime?: () => Promise<void>;
  readSecret?: (ref: string) => string | undefined;
  prepareSources?: PrepareProjectSources;
  materialStage?: () => ReturnType<typeof createMaterialFileStage> | undefined;
}

type ArtifactDraft = Omit<Artifact, "id" | "createdAt">;
type PendingFiles = { draft: ArtifactDraft; staged: Array<{ position: number; file: StagedMaterialFile }> };
type MaterialRecord = ReturnType<SqliteStore["resolveBusinessMaterialRefs"]>[number];
type MountedFile = ArtifactFileGrant & { containerPath: string };

export function projectCredentialName(name: unknown): name is string {
  return typeof name === "string" && /^[A-Z][A-Z0-9_]{0,95}$/.test(name)
    && !/^(?:PATH|HOME|USERPROFILE|SYSTEMROOT|COMSPEC|SHELL|TMP|TEMP|TMPDIR|LD_.*|DYLD_.*|NODE_.*|BUN_.*|MAO_.*|GIT_.*|DOCKER_.*|PODMAN_.*|KIRI_.*|CONTAINER_HOST|CONTAINER_CONNECTION|CONTAINERS_.*|REGISTRY_AUTH_FILE)$/.test(name);
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
  private readonly turns = new Map<string, HarnessTurn>();
  private readonly awaitingRuntime = new Map<string, string>();
  private readonly cancelledRuns = new Set<string>();
  private closed = false;
  private readonly cwd: string;

  constructor(private readonly options: OperatorCoordinatorOptions) {
    this.cwd = options.cwd ?? harnessControlPlaneRoot;
  }

  dispatch(runId: string, unitRunId?: string): Promise<void> {
    const unitRun = unitRunId
      ? this.options.store.getUnitRun(runId, unitRunId)
      : this.options.store.nextQueuedUnitRun(runId);
    if (!unitRun) return Promise.resolve();
    const existing = this.active.get(unitRun.id);
    if (existing) return existing;
    const runState = this.options.store.getBusinessRun(runId)?.status;
    if (runState === "queued" || (runState === "running" && unitRun.status === "queued")) this.cancelledRuns.delete(runId);
    const advance = (completedUnitRunId?: string | null) => {
      this.active.delete(unitRun.id);
      if (!this.closed && completedUnitRunId !== null) {
        this.options.onProgress?.(runId, unitRunId ? completedUnitRunId : undefined);
      }
    };
    const work = this.run(runId, unitRun.id).then(advance, (error) => {
      advance();
      throw error;
    });
    this.active.set(unitRun.id, work);
    return work;
  }

  reconcileInterruptedRuns(): void {
    if (!unitRunProcessScope || this.closed) return;
    const { store, events } = this.options;
    for (const owner of store.runningUnitRunOwners(unitRunProcessScope)) {
      if (!unitRunOwnerExited(owner, unitRunProcessScope)) continue;
      const failed = store.interruptUnitRun(owner.unitRunId, owner.token);
      if (failed) events.notifyCommitted(failed);
    }
  }

  async takeRuntimeDeferredRuns(): Promise<string[]> {
    const deferred = [...this.awaitingRuntime];
    this.awaitingRuntime.clear();
    await Promise.allSettled(deferred.map(([id]) => this.active.get(id)));
    return [...new Set(deferred.map(([, runId]) => runId))];
  }

  stop(): void {
    this.closed = true;
    this.awaitingRuntime.clear();
    for (const turn of this.turns.values()) void turn.close();
  }

  async cancelRun(runId: string): Promise<void> {
    this.cancelledRuns.add(runId);
    const unitRuns = this.options.store.listUnitRuns(runId);
    await Promise.all(unitRuns.map((unitRun) => this.turns.get(unitRun.id)?.close()));
    await Promise.allSettled(unitRuns.map((unitRun) => this.active.get(unitRun.id)).filter((work): work is Promise<void> => Boolean(work)));
  }

  async close(): Promise<void> {
    this.stop();
    await Promise.allSettled([...this.turns.values()].map((turn) => turn.close()));
    await Promise.allSettled(this.active.values());
  }

  private async run(runId: string, unitRunId: string): Promise<string | null | undefined> {
    if (this.closed || this.cancelledRuns.has(runId)) return;
    this.awaitingRuntime.delete(unitRunId);
    const unitRun = this.options.store.getUnitRun(runId, unitRunId);
    if (!unitRun || unitRun.status !== "queued") return;
    const unit = this.options.store.getBusinessUnit(unitRun.unitId);
    if (!unit || unit.kind !== "operator" || !unit.projectId) {
      this.options.store.completeUnitRun(unitRun.id, "blocked", [], "operator-project-unavailable");
      return;
    }
    const binding = resolveOperatorBinding(unit, this.options.store.listHarnessProfiles());
    if ("reason" in binding) {
      this.options.store.completeUnitRun(unitRun.id, "blocked", [], binding.reason);
      this.options.events.publish("operator.run.blocked", { runId, unitRunId, reason: binding.reason });
      return;
    }
    const { definition, profile, model, explicitModel } = binding;
    const sessionId = `${definition.id}:${unitRun.id}`;
    const acpAvailable = Boolean(this.options.acpSpawn || process.env.MAO_ACP_AVAILABLE === "1");
    let useAcp: boolean;
    try {
      useAcp = requireDeclaredTransport(definition, acpAvailable) === "acp";
    } catch (error) {
      this.options.store.completeUnitRun(unitRun.id, "blocked", [], error instanceof Error ? error.message : "official-acp-unavailable");
      this.options.events.publish("operator.run.blocked", { runId, unitRunId, reason: error instanceof Error ? error.message : "official-acp-unavailable" });
      return;
    }
    if (explicitModel && definition.transportPreference === "acp" && !useAcp) {
      const reason = "operator-harness-requires-acp-for-model";
      this.options.store.completeUnitRun(unitRun.id, "blocked", [], reason);
      this.options.events.publish("operator.run.blocked", { runId, unitRunId, reason });
      return;
    }
    const owner = createUnitRunOwner();
    const turn = new HarnessTurn(profile.sessionTimeoutSeconds);
    this.turns.set(unitRun.id, turn);
    const transport = useAcp
      ? new AcpTransport(definition, turn.ownProcess(this.options.acpSpawn ?? this.options.spawn ?? defaultAcpSpawn))
      : new OfficialCliTransport(definition, turn.ownCliProcess(this.options.spawn), this.options.store.clock);
    const outputIds = new Set(unitRun.outputs);
    const credentials: Record<string, string> = Object.create(null);
    const requested = new Set<string>();
    let recordedSessionId = sessionId;
    let sourceMounts: PreparedProjectSources | undefined;
    let materialStage: ReturnType<typeof createMaterialFileStage> | undefined;
    const pendingFiles: PendingFiles[] = [];
    let finishing = false;
    let claimed: UnitRun | undefined;
    try {
      if (profile.image && this.options.ensureRuntime) {
        try {
          await turn.wait(this.options.ensureRuntime());
        } catch {
          if (!this.closed) this.awaitingRuntime.set(unitRunId, runId);
          return null;
        }
      }
      if (this.closed || this.cancelledRuns.has(runId)) return null;
      claimed = this.options.store.claimUnitRun(runId, unit.id, sessionId, owner);
      if (!claimed) return;
      this.options.events.publish("operator.run.claimed", { runId, unitRunId, unitId: unit.id, sessionId, harness: definition.id, model });
      const project = this.options.store.getProject(unit.projectId);
      const ownProject = project?.businessSystemId === unit.businessSystemId ? project : undefined;
      if (ownProject && profile.image && this.options.prepareSources) {
        await turn.wait(this.options.prepareSources(ownProject).then((result) => {
          if (finishing) result?.close();
          else sourceMounts = result;
        }));
      }
      const sources = sourceMounts?.facts ?? (ownProject ? projectSourceFacts(ownProject, !profile.image) : []);
      const materials = this.options.store.resolveBusinessMaterialRefs(unit.businessSystemId, unitRun.inputs);
      materialStage = ownProject && profile.image ? this.options.materialStage?.() : undefined;
      const artifactIds = materials.flatMap(({ record }) => record ? [record.id] : []);
      const materialInputs = materialStage ? await turn.wait(materialStage.prepareInputs(this.options.store.artifactFileGrants(unit.businessSystemId, artifactIds))) : undefined;
      for (const resource of this.options.store.unitSecretRequests(unitRun.id)) {
        if (resource.projectId !== unit.projectId || resource.status !== "available" || !projectCredentialName(resource.name)) continue;
        const value = this.options.readSecret?.(resource.vaultRef);
        if (!value) throw new Error("project-credential-vault-entry-missing");
        credentials[resource.name] = value;
      }
      const session = await turn.start(transport, {
        task: this.taskFor(unit, runId, sources, materials, materialInputs?.files ?? []),
        contract: "你是业务系统 Operator。只使用官方 Harness 能力处理项目。每个可交付结果必须额外输出一行 JSONL，格式为 {\"type\":\"mao.record\",\"record_type\":\"material\",\"kind\":\"...\",\"ref\":\"...\",\"summary\":\"面向人的说明\",\"files\":[{\"ref\":\"...\",\"path\":\"...\"}]}。文件字段只放引用，不嵌入文件内容。缺少项目资源时输出 {\"type\":\"mao.secret_request\",\"name\":\"...\",\"purpose\":\"...\",\"scope\":\"...\"}，由项目资源请求流程向用户呈现。"
          + (materialStage ? `读取输入文件时使用其 containerPath。把本轮生成的文件写入 ${materialOutputRoot}，每项使用单层文件名，例如 {\"staged\":\"preview.mp4\",\"path\":\"preview.mp4\"}。成功后平台保存文件并生成内容引用。` : ""),
        model,
        requireModelSelection: explicitModel,
        env: { ...credentials, ...harnessEndpointEnv(definition, profile.baseUrl) },
        cwd: this.cwd,
        containerImage: profile.image,
        network: profile.network === "egress" ? "egress" : "none",
        egressBaseUrl: profile.baseUrl,
        projectSources: sourceMounts?.mounts,
        materialFiles: materialStage ? { outputDirectory: materialStage.outputDirectory, inputs: materialInputs?.mounts ?? [] } : undefined,
      });
      if (this.closed || this.cancelledRuns.has(runId)) return;
      if (useAcp) {
        this.options.store.noteUnitSession(unitRun.id, session.id, owner.token);
        recordedSessionId = session.id;
      }
      const secretValues = Object.values(credentials);
      let failed = false;
      let finished = false;
      let recordsProduced = false;
      for await (const emitted of turn.events(session)) {
        if (this.closed) return;
        const event = redactCredentialValues(emitted, secretValues) as HarnessEvent;
        if (event.sessionId && event.sessionId !== recordedSessionId) {
          this.options.store.noteUnitSession(unitRun.id, event.sessionId, owner.token);
          recordedSessionId = event.sessionId;
        }
        if (event.type === "record") {
          const draft = this.recordFromEvent(event, unit.projectId, unit, definition.id);
          if (!draft) {
            failed = true;
          } else {
            recordsProduced = true;
            const invalidRefs = this.options.store.invalidBusinessInputRefs(unit.businessSystemId, [
              ...(draft.inputs ?? []), ...(draft.lineage ?? []),
            ]);
            if (invalidRefs.length) {
              failed = true;
              continue;
            }
            const staged = (draft.files ?? []).flatMap((file, position) => file && typeof file === "object" && Object.hasOwn(file, "staged")
              ? [{ position, file: file as unknown as StagedMaterialFile }] : []);
            if (staged.length) {
              if (!materialStage) throw new Error("当前 Operator 容器不支持暂存产物");
              pendingFiles.push({ draft, staged });
            } else {
              const artifact = this.options.store.addArtifact(draft);
              outputIds.add(artifact.ref);
              this.options.events.publish("artifact.created", artifact);
              if (artifact.inputs?.length || artifact.lineage?.length) {
                const conversion = this.options.store.createBusinessConversion({
                  businessSystemId: unit.businessSystemId,
                  kind: "record-production",
                  summary: artifact.summary ?? artifact.title ?? `生成 ${artifact.kind}`,
                  status: "completed",
                  inputs: [...new Set([...(artifact.inputs ?? []), ...(artifact.lineage ?? [])])],
                  outputs: [artifact.ref],
                  producer: artifact.producer ? { operator: artifact.producer.operator, harness: artifact.producer.harness } : undefined,
                  metadata: { artifact_id: artifact.id, schema: "mao.business-record/1" },
                });
                this.options.events.publish("business-conversion.created", conversion);
              }
            }
          }
        }
        if (event.type === "secret.request") {
          const metadata = event.metadata && typeof event.metadata === "object" ? event.metadata as Record<string, unknown> : {};
          const name = metadata.name;
          if (projectCredentialName(name) && !Object.hasOwn(credentials, name)) {
            const request = this.options.store.createSecretRequest({ projectId: unit.projectId, name, vaultRef: `vault://secret/${this.options.store.uuid()}`, metadata: { purpose: metadata.purpose, scope: metadata.scope, checks: metadata.checks } });
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
        if (event.type === "session.finished") finished = true;
      }
      if (this.closed || this.cancelledRuns.has(runId)) return;
      if (requested.size) {
        const waiting = this.options.store.completeUnitRun(unitRun.id, "waiting_resources", [...outputIds], undefined, owner.token);
        if (waiting) this.options.events.publish("operator.run.waiting_resources", waiting);
        this.options.store.resumeResourceRuns(requested.values().next().value!);
        return;
      }
      if (!failed && !recordsProduced) failed = true;
      let completed: UnitRun | undefined;
      if (!failed && materialStage && pendingFiles.length) {
        if (!finished) throw new Error("官方 Harness 没有完成本轮，无法收取产物");
        await turn.wait(session.close());
        const settled: Array<{ draft: ArtifactDraft; byPosition: Map<number, CollectedMaterialFile> }> = [];
        for (const { draft, staged } of pendingFiles) {
          const files = await turn.wait(materialStage.collect(staged.map(({ file }) => file), secretValues));
          const byPosition = new Map(staged.map(({ position }, index) => [position, files[index]]));
          settled.push({ draft: { ...draft, files: draft.files?.map((file, index) => byPosition.get(index) ?? file) }, byPosition });
        }
        const store = this.options.store;
        const saved = store.db.transaction(() => {
          if (!store.ownsRunningUnitRun(unitRun.id, owner.token)) throw new Error("Operator 已结束，无法登记产物");
          const outputs = [...outputIds];
          const events = settled.map(({ draft, byPosition }) => {
            const artifact = store.addArtifact(draft);
            store.registerArtifactFiles(artifact.id, [...byPosition].map(([position, { digest, size }]) => ({ position, digest, size })));
            outputs.push(`${artifact.recordType}://${artifact.id}`);
            const artifactEvent = store.appendEvent("artifact.created", artifact);
            if (artifact.inputs?.length || artifact.lineage?.length) {
              const conversion = store.createBusinessConversion({
                businessSystemId: unit.businessSystemId,
                kind: "record-production",
                summary: artifact.summary ?? artifact.title ?? `生成 ${artifact.kind}`,
                status: "completed",
                inputs: [...new Set([...(artifact.inputs ?? []), ...(artifact.lineage ?? [])])],
                outputs: [artifact.ref],
                producer: artifact.producer ? { operator: artifact.producer.operator, harness: artifact.producer.harness } : undefined,
                metadata: { artifact_id: artifact.id, schema: "mao.business-record/1" },
              });
              return [artifactEvent, store.appendEvent("business-conversion.created", conversion)];
            }
            return [artifactEvent];
          }).flat();
          return { events, completed: store.completeUnitRun(unitRun.id, "completed", [...new Set(outputs)], undefined, owner.token) };
        }).immediate();
        for (const event of saved.events) this.options.events.notifyCommitted(event);
        completed = saved.completed;
      } else {
        completed = this.options.store.completeUnitRun(unitRun.id, failed ? "failed" : "completed", [...outputIds], failed ? (recordsProduced ? "official-harness-failed" : "official-harness-produced-no-record") : undefined, owner.token);
      }
      if (completed) this.options.events.publish("operator.run.completed", completed);
      if (completed?.status === "completed") return completed.id;
    } catch (error) {
      if (this.closed) return;
      if (!claimed) throw error;
      const message = redactCredentialValues(error instanceof Error ? error.message : "official-harness-failed", Object.values(credentials)) as string;
      const completed = this.options.store.completeUnitRun(unitRun.id, "failed", [...outputIds], message, owner.token);
      if (completed) this.options.events.publish("operator.run.failed", completed);
    } finally {
      finishing = true;
      try { await turn.close(); }
      finally {
        this.turns.delete(unitRun.id);
        try { sourceMounts?.close(); }
        finally { materialStage?.close(); }
      }
    }
  }

  private taskFor(unit: BusinessUnit, runId: string, sources: ReturnType<typeof projectSourceFacts>, materials: MaterialRecord[], mounted: MountedFile[]): string {
    const positions = new Map(mounted.map((file) => [`${file.artifactId}:${file.position}`, file.containerPath]));
    const inputs = materials.map(({ ref, record }) => {
      if (!record) return { ref };
      return {
        ref, record_type: record.recordType, kind: record.kind, title: record.title,
        description: record.description, summary: record.summary, purpose: record.purpose,
        files: record.files?.map(({ ref, path, mediaType, digest }, index) => ({
          ref, path, mediaType, digest, containerPath: positions.get(`${record.id}:${index}`),
        })),
      };
    });
    return [
      `执行业务运行 ${runId}。Operator ${unit.name} 负责项目 ${unit.projectId}。`,
      (() => {
        const kernel = this.options.store.getBusinessRun(runId)?.metadata.kernel;
        return kernel && typeof kernel === "object" ? `本次运行已固定 MAO 内核快照：${JSON.stringify(kernel)}` : "本次运行未声明 legacy MAO Registry 绑定。";
      })(),
      "以下 JSON 描述项目来源和输入物料。使用其中的信息完成本次任务。",
      JSON.stringify({ projectSources: sources, materials: inputs }),
      "完成后输出可交付物并说明结果。",
    ].join("\n");
  }

  private recordFromEvent(event: HarnessEvent, projectId: string, unit: { id: string; machineRef?: string }, harness: string): ArtifactDraft | undefined {
    const metadata = event.metadata && typeof event.metadata === "object" ? event.metadata as Record<string, unknown> : {};
    const value = metadata.record && typeof metadata.record === "object" ? metadata.record as Record<string, unknown> : metadata;
    if (typeof value.kind !== "string" || typeof value.ref !== "string"
      || (typeof value.title !== "string" && typeof value.description !== "string" && typeof value.summary !== "string")) return undefined;
    if (value.schema !== undefined && value.schema !== "mao.business-record/1") return undefined;
    if (Array.isArray(value.files) && value.files.some((file) => {
      if (!file || typeof file !== "object") return true;
      const item = file as Record<string, unknown>;
      return typeof item.ref !== "string" && typeof item.staged !== "string";
    })) return undefined;
    return {
      projectId,
      recordType: value.record_type === "artifact" ? "artifact" : "material",
      kind: value.kind,
      ref: value.ref,
      title: typeof value.title === "string" ? value.title : undefined,
      description: typeof value.description === "string" ? value.description : undefined,
      summary: typeof value.summary === "string" ? value.summary : undefined,
      purpose: typeof value.purpose === "string" ? value.purpose : undefined,
      status: typeof value.status === "string" ? value.status : "produced",
      producer: { operator: unit.id, machine: unit.machineRef, harness },
      files: Array.isArray(value.files) ? value.files as never : undefined,
      inputs: Array.isArray(value.inputs) ? value.inputs.filter((item): item is string => typeof item === "string") : undefined,
      outputs: Array.isArray(value.outputs) ? value.outputs.filter((item): item is string => typeof item === "string") : undefined,
      lineage: Array.isArray(value.lineage) ? value.lineage.filter((item): item is string => typeof item === "string") : undefined,
      metadata: value,
    };
  }
}
