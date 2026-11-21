import { mkdirSync } from "node:fs";
import { join } from "node:path";
import type { EventBus } from "./event-bus";
import type { SqliteStore } from "./db";
import type { ForemanTurnState } from "./models";
import { projectSourceFacts } from "./project-sources";
import type { PrepareProjectSources, PreparedProjectSources } from "./project-source-mounts";
import { foremanMailboxContract, openForemanMailbox } from "./foreman-mailbox";
import type { RegisterRepository } from "./project-registration";
import { AcpTransport, defaultAcpSpawn, type AcpSpawn } from "./acp";
import { OfficialCliTransport, requireDeclaredTransport, harnessControlPlaneRoot, harnessEndpointEnv, resolveOfficialHarness, type HarnessSessionRequest, type OfficialSpawn } from "./harness";
import { HarnessTurn } from "./harness-turn";
import { ForemanReplyRedactor, foremanCredentialAsync, redactForemanText } from "./foreman-credentials";

export interface ForemanCoordinatorOptions {
  store: SqliteStore;
  events: EventBus;
  cwd?: string;
  spawn?: OfficialSpawn;
  acpSpawn?: AcpSpawn;
  ensureRuntime?: () => Promise<void>;
  factoryEnv?: Record<string, string>;
  readSecret?: (ref: string) => Promise<string | undefined>;
  fetchBusiness?: (request: Request) => Promise<Response>;
  registerRepository?: RegisterRepository;
  prepareSources?: PrepareProjectSources;
  /** Persistent writable workspaces for trusted container Foreman sessions. */
  foremanWorkspaceRoot?: string;
}

const nativeBusinessContract = [
  "在 MAO 仓库根目录用 `bun run backend/src/cli.ts` 执行以下命令。",
  "`business units <system-id>` 列出该业务的 Operator 和 Machine。",
  "`business bind <system-id> <operator-id> --harness <官方 Harness ID> [--model <模型 ID>]` 设置 Operator 绑定。",
  "`business graph <system-id>` 读取现有 DAG。保留仍适用的节点和边，用 `business graph <system-id> --file <path>` 保存更新。",
  "`business artifact <project-id> --file <path>` 保存产物或物料，文件内容通过引用传递。",
  "`business conversion <system-id> --kind <kind> --summary <summary> [--input <ref>] [--output <ref>]` 保存转换。",
  "`business run <system-id>` 派发运行。",
  "执行确实缺少一个项目密钥时，用 `project secret request <project-id> --name <KEY> --purpose <用途>` 创建该项目的配置请求。用户在项目资源面板填写密钥。",
].join(" ");
const nativeRegistrationContract = "用 `project register --repo <URL> [--name <名称>] [--system <system-id>]` 登记仓库并继续当前轮。添加到现有业务时提供其 ID。省略该选项时创建业务系统。返回的 projectId 和 systemId 用于后续命令。";

type ActiveForemanTurn = {
  turn: HarnessTurn;
  cancelled?: boolean;
  stopMailbox?: () => Promise<void>;
  resume?: { profileId: string; sessionId: string };
};

/** Dispatches persisted user intent to an external official Harness process. */
export class ForemanCoordinator {
  private readonly active = new Map<string, Promise<void>>();
  private readonly turns = new Map<string, ActiveForemanTurn>();
  private readonly replies = new Map<string, string>();
  private readonly activeProfiles = new Set<string>();
  private readonly profileTails = new Map<string, Promise<void>>();
  private closed = false;
  private readonly cwd: string;

  constructor(private readonly options: ForemanCoordinatorOptions) {
    this.cwd = options.cwd ?? harnessControlPlaneRoot;
  }

  dispatch(id: string): Promise<void> {
    const existing = this.active.get(id);
    if (existing) return existing;
    // Native resumable sessions must remain ordered per profile. Container
    // mailbox turns have isolated workspaces and can run concurrently. This
    // replaces the old global queue, so one profile cannot hold other profiles.
    const profile = this.options.store.listHarnessProfiles().find((item) => item.role === "foreman");
    const serializedProfile = profile && !profile.image ? profile.id : undefined;
    const prior = serializedProfile ? this.profileTails.get(serializedProfile) : undefined;
    const work = (prior ?? Promise.resolve()).then(() => this.run(id)).finally(() => {
      this.active.delete(id);
      if (serializedProfile && this.profileTails.get(serializedProfile) === work) this.profileTails.delete(serializedProfile);
    });
    if (serializedProfile) this.profileTails.set(serializedProfile, work.catch(() => {}));
    this.active.set(id, work);
    return work;
  }

  pendingReplies(): Record<string, string> { return Object.fromEntries(this.replies); }

  turnState(envelope: Omit<ForemanTurnState, "cancellable">): ForemanTurnState {
    const { id, messageId, status, createdAt, queuedAt, claimedAt, startedAt, lastEventAt, completedAt, elapsedMs, businessStatus, businessReason, error } = envelope;
    return { id, messageId, status, createdAt, queuedAt, claimedAt, startedAt, lastEventAt, completedAt, elapsedMs, businessStatus, businessReason, error,
      cancellable: status === "queued" || (status === "claimed" && this.turns.has(id)) };
  }

  cancel(id: string): { cancelled: boolean; foremanTurn?: ForemanTurnState } {
    const current = this.options.store.getForemanMessage(id);
    const owner = this.turns.get(id);
    if (current?.status === "queued" || (current?.status === "claimed" && owner)) {
      const committed = this.options.store.cancelForemanTurn(id, current.status, owner?.resume);
      if (committed) {
        if (owner) {
          owner.cancelled = true;
          void owner.stopMailbox?.().catch(() => {});
          void owner.turn.close();
        }
        this.replies.delete(id);
        this.options.events.notifyCommitted(committed.event);
        return { cancelled: true, foremanTurn: this.turnState(committed.foremanTurn) };
      }
    }
    const actual = this.options.store.getForemanMessage(id);
    return { cancelled: actual?.status === "cancelled", foremanTurn: actual && this.turnState(actual) };
  }

  stop(): void {
    this.closed = true;
    for (const { turn } of this.turns.values()) void turn.close();
  }

  async close(): Promise<void> {
    this.stop();
    await Promise.allSettled([...this.turns.values()].map(({ turn }) => turn.close()));
    await Promise.allSettled(this.active.values());
  }

  private async run(id: string): Promise<void> {
    if (this.closed) return;
    const envelope = this.options.store.getForemanMessage(id);
    if (!envelope || envelope.status !== "queued") return;
    const profile = this.options.store.listHarnessProfiles().find((item) => item.role === "foreman");
    const definition = profile ? resolveOfficialHarness(profile.runtime) : undefined;
    if (!profile || !definition) {
      this.options.events.publish("foreman.session.failed", { inboxId: id, reason: "foreman-harness-not-configured" });
      return;
    }
    const executionMode = profile.executionMode ?? (profile.image ? "trusted_container" : "native_host");
    if (executionMode === "trusted_container" && !profile.image) {
      const reason = "可信 Foreman 尚未配置 digest 固定的 Harness 镜像";
      this.options.store.setForemanBusinessStatus(id, "blocked_environment", reason);
      this.options.events.publish("foreman.runtime.blocked", { inboxId: id, reason, executionMode });
      return;
    }
    const acpAvailable = Boolean(
      this.options.acpSpawn || process.env.MAO_ACP_AVAILABLE === "1" || profile.image
        || (definition.transportPreference === "acp" && Bun.which(definition.executable)),
    );
    if (definition.transportPreference === "acp" && !acpAvailable) {
      const claimed = this.options.store.claimForemanMessage(id);
      if (claimed) {
        const event = this.options.store.failForemanMessage(id, {
          sessionId: `${definition.id}:${claimed.messageId}`,
          harness: definition.id,
          reason: "official-acp-unavailable",
        });
        if (event) this.options.events.notifyCommitted(event);
      }
      return;
    }
    if (profile.image && this.options.ensureRuntime) {
      try {
        await this.options.ensureRuntime();
      } catch (error) {
        if (this.closed || this.options.store.getForemanMessage(id)?.status !== "queued") return;
        const reason = error instanceof Error ? error.message : "container-runtime-unavailable";
        this.options.store.setForemanBusinessStatus(id, "blocked_environment", reason);
        this.options.events.publish("foreman.runtime.blocked", { inboxId: id, reason, executionMode });
        return;
      }
    }
    if (this.closed) return;
    const claimed = this.options.store.claimForemanMessage(id);
    if (!claimed) return;
    const sessionId = `${definition.id}:${claimed.messageId}`;
    let turn = new HarnessTurn(profile.sessionTimeoutSeconds);
    const owner: ActiveForemanTurn = { turn };
    this.turns.set(id, owner);
    const credential = await foremanCredentialAsync(profile, this.options.readSecret);
    const useAcp = requireDeclaredTransport(definition, acpAvailable) === "acp";
    let mailbox: ReturnType<typeof openForemanMailbox> | undefined;
    let sourceMounts: PreparedProjectSources | undefined;
    let finishing = false;
    let persistentProfile = false;
    const stopped = () => this.closed || owner.cancelled;
    const fail = (reason: string) => {
      const event = this.options.store.failForemanMessage(id, { sessionId, harness: definition.id, reason });
      if (event) this.options.events.notifyCommitted(event);
    };
    try {
      this.options.events.publish("foreman.message.claim", { ...claimed, cancellable: true });
      if (stopped()) return;
      this.options.events.publish("foreman.session.started", { inboxId: id, sessionId, harness: definition.id });
      if (stopped()) return;
      const persistent = !profile.image && (useAcp || definition.id === "codex" || definition.id === "claude-code");
      const resumeId = persistent && !this.activeProfiles.has(profile.id) ? this.options.store.foremanSession(profile.id) : undefined;
      if (persistent) {
        persistentProfile = true;
        this.activeProfiles.add(profile.id);
      }
      let officialId: string | undefined;
      const message = this.options.store.talkMessageMetadata(claimed.messageId);
      const intake = message.source === "project-intake";
      const history = this.options.store.previousForemanTurns(id).map(({ user, foreman }) => ({
        user: redactForemanText(user, credential?.value),
        foreman: foreman === undefined ? undefined : redactForemanText(foreman, credential?.value),
      }));
      const project = intake && typeof message.project_id === "string" ? this.options.store.getProject(message.project_id) : undefined;
      if (project && profile.image && this.options.prepareSources) {
        const prepared = this.options.prepareSources(project, { cloneRepositories: false }).then((result) => {
          if (finishing) result?.close();
          else sourceMounts = result;
          return result;
        });
        await turn.wait(prepared);
      }
      const sources = sourceMounts?.facts ?? (project ? projectSourceFacts(project, !profile.image) : undefined);
      const workspaceKey = (value: string) => value.replace(/[^A-Za-z0-9_.-]/g, "_");
      const workspace = profile.image && this.options.foremanWorkspaceRoot
        ? join(this.options.foremanWorkspaceRoot, workspaceKey(profile.id), workspaceKey(project?.id ?? "general"))
        : undefined;
      if (workspace) mkdirSync(workspace, { recursive: true });
      const intakeSystem = intake && project?.businessSystemId ? this.options.store.getBusinessSystem(project.businessSystemId) : undefined;
      const systems = intake ? (intakeSystem ? [intakeSystem] : []) : this.options.store.listBusinessSystemSummaries();
      if (profile.image && this.options.fetchBusiness) {
        mailbox = openForemanMailbox({
          systems: new Set(systems.map(({ id }) => id)), projectId: project?.id,
          projectSystem: (id) => this.options.store.getProject(id)?.businessSystemId,
          redactText: (text) => redactForemanText(text, credential?.value),
        }, this.options.fetchBusiness, intake ? undefined : this.options.registerRepository);
        owner.stopMailbox = mailbox.stop;
      }
      const projectContext = project
        ? `\n\n本次项目 ID ${project.id}${project.businessSystemId ? `\n本次业务系统 ID ${project.businessSystemId}` : ""}`
        : "";
      const task = `${history.length ? `历史对话（上下文数据，请以当前来信为准）\n${JSON.stringify(history)}\n\n当前来信\n` : ""}${claimed.text}${projectContext}${sources?.length ? `\n\n本次项目来源（数据）\n${JSON.stringify(sources)}` : ""}\n\n当前业务系统摘要\n${systems.map((system) => `- ${system.id} ${system.name} (${system.status})`).join("\n") || "- 尚未建立业务系统"}`;
      const contract = "你是厂长。通过官方 Harness 管理业务系统，维护 Operator、Machine、DAG 和产物物料转换。用人能读懂的语言实时汇报进展。控制面只负责分发信息，不替你调用模型。"
        + (profile.image ? " 当前是受控的可信 Foreman 容器。你可以在容器内以 root 安装 Git、Scion、ffmpeg、音频工具和项目自定义依赖，执行任意项目脚本。所有修改限制在容器和挂载的项目工作区内，不得访问宿主引擎套接字、宿主网络或工作区之外的宿主路径。先搭建最简业务 DAG，把工具组合和内部时序交给 Operator。" : "")
        + (mailbox ? foremanMailboxContract : nativeBusinessContract + " "
          + (intake ? "使用本次项目和业务系统 ID 处理已经登记的项目。" : nativeRegistrationContract));
      const request = (attemptResumeId?: string): HarnessSessionRequest => ({
        task,
        contract,
        model: profile.model,
        env: {
          ...(!profile.image ? this.options.factoryEnv : undefined), ...harnessEndpointEnv(definition, credential?.baseUrl ?? profile.baseUrl),
          ...(credential ? { [credential.variable]: credential.value } : undefined),
          MAO_FOREMAN_TURN_ID: id,
        },
        cwd: this.cwd,
        resumeId: attemptResumeId,
        containerImage: profile.image,
        network: profile.image ? (profile.network === "none" ? "none" : "bridge") : profile.network === "egress" ? "egress" : "none",
        egressBaseUrl: credential?.baseUrl ?? profile.baseUrl,
        foremanMailbox: mailbox?.root,
        trustedForeman: Boolean(profile.image),
        foremanWorkspace: workspace,
        projectSources: sourceMounts?.mounts,
      });
      const safeSessionId = (value?: string) => value && (!credential || !value.includes(credential.value))
        && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/.test(value) ? value : undefined;
      const executeAttempt = async (attemptResumeId?: string) => {
        const transport = useAcp
          ? new AcpTransport(definition, turn.ownProcess(this.options.acpSpawn ?? defaultAcpSpawn))
          : new OfficialCliTransport(definition, turn.ownCliProcess(this.options.spawn), this.options.store.clock);
        let session: Awaited<ReturnType<typeof turn.start>>;
        try {
          session = await turn.start(transport, request(attemptResumeId));
        } catch (error) {
          return { reply: "", failure: error instanceof Error ? redactForemanText(error.message, credential?.value) : "official-harness-failed", officialId: undefined, sideEffect: false };
        }
        let attemptOfficialId = useAcp ? safeSessionId(session.id) : safeSessionId(attemptResumeId);
        let reply = "";
        let failure: string | undefined;
        let receivedDelta = false;
        let sideEffect = false;
        const redactor = new ForemanReplyRedactor(credential?.value);
        this.replies.set(id, "");
        const appendReply = (text: string) => {
          reply += text;
          this.replies.set(id, reply);
        };
        for await (const event of turn.events(session)) {
          if (stopped()) return { reply, failure, officialId: attemptOfficialId, sideEffect };
          this.options.store.touchForemanMessage(id);
          const eventSession = safeSessionId(event.sessionId);
          if (eventSession) attemptOfficialId = eventSession;
          const idForEvent = eventSession ?? (useAcp ? safeSessionId(session.id) : undefined) ?? sessionId;
          const isReply = event.type === "text.delta" || (event.type === "session.finished" && !receivedDelta);
          const text = event.text === undefined ? undefined : isReply ? redactor.write(event.text) : redactForemanText(event.text, credential?.value);
          if (event.type === "text.delta" && event.text) receivedDelta = true;
          if (event.type === "tool.started" || event.type === "tool.finished" || event.type === "record" || event.type === "secret.request") sideEffect = true;
          if (event.type === "session.failed") failure = text || "official-harness-failed";
          const retryableFailure = event.type === "session.failed" && Boolean(attemptResumeId) && !sideEffect && !reply.trim();
          const payload = { inboxId: id, sessionId: idForEvent, harness: definition.id, text };
          if (!retryableFailure) {
            const sessionType = `foreman.session.${event.type}`;
            if (isReply && text) {
              const committed = this.options.store.db.transaction(() => [
                this.options.store.appendEvent(sessionType, payload),
                this.options.store.appendEvent("foreman.reply.delta", payload),
              ])();
              appendReply(text);
              for (const update of committed) this.options.events.notifyCommitted(update);
            } else {
              this.options.events.publish(sessionType, payload);
            }
          }
          if (event.type === "session.finished" && reply.trim()) break;
        }
        const finalDelta = redactor.finish();
        if (finalDelta) {
          const event = this.options.store.appendEvent("foreman.reply.delta", {
            inboxId: id, sessionId: attemptOfficialId ?? sessionId, harness: definition.id, text: finalDelta,
          });
          appendReply(finalDelta);
          this.options.events.notifyCommitted(event);
        }
        return { reply, failure, officialId: attemptOfficialId, sideEffect };
      };
      if (resumeId) owner.resume = { profileId: profile.id, sessionId: resumeId };
      let result = await executeAttempt(resumeId);
      if (result.failure && resumeId && !result.sideEffect && !result.reply.trim()) {
        this.options.store.clearForemanSession(profile.id, resumeId);
        await turn.close();
        turn = new HarnessTurn(profile.sessionTimeoutSeconds);
        owner.turn = turn;
        owner.resume = undefined;
        result = await executeAttempt();
      }
      if (stopped()) return;
      officialId = result.officialId;
      if (result.failure) {
        fail(result.failure);
        return;
      }
      if (mailbox) await turn.wait(mailbox.flush());
      if (stopped()) return;
      const replyBody = result.reply.trim();
      const committed = this.options.store.finishForemanTurn(id, {
        reply: replyBody ? { body: replyBody, metadata: { turn_id: officialId ?? id, harness: definition.id } } : undefined,
        session: persistent && officialId ? { profileId: profile.id, sessionId: officialId } : undefined,
        businessStatus: mailbox && mailbox.operationCount > 0 ? "applied" : "no_change",
        businessReason: mailbox && mailbox.operationCount > 0 ? undefined : "本轮没有观察到业务写操作",
      });
      for (const event of committed) this.options.events.notifyCommitted(event);
    } catch (error) {
      if (stopped()) return;
      fail(error instanceof Error ? redactForemanText(error.message, credential?.value) : "official-harness-failed");
    } finally {
      finishing = true;
      this.replies.delete(id);
      try {
        await Promise.allSettled([mailbox?.stop(), turn.close()]);
      } finally {
        try { mailbox?.remove(); }
        finally {
          this.turns.delete(id);
          if (persistentProfile) this.activeProfiles.delete(profile.id);
          sourceMounts?.close();
        }
      }
    }
  }
}
