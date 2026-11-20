import type { EventBus } from "./event-bus";
import type { SqliteStore } from "./db";
import type { ForemanTurnState } from "./models";
import { projectSourceFacts } from "./project-sources";
import type { PrepareProjectSources, PreparedProjectSources } from "./project-source-mounts";
import { foremanMailboxContract, openForemanMailbox } from "./foreman-mailbox";
import type { RegisterRepository } from "./project-registration";
import { AcpTransport, defaultAcpSpawn, type AcpSpawn } from "./acp";
import { OfficialCliTransport, chooseTransport, harnessControlPlaneRoot, harnessEndpointEnv, resolveOfficialHarness, type OfficialSpawn } from "./harness";
import { HarnessTurn } from "./harness-turn";
import { ForemanReplyRedactor, foremanCredential, redactForemanText } from "./foreman-credentials";

export interface ForemanCoordinatorOptions {
  store: SqliteStore;
  events: EventBus;
  cwd?: string;
  spawn?: OfficialSpawn;
  acpSpawn?: AcpSpawn;
  ensureRuntime?: () => Promise<void>;
  factoryEnv?: Record<string, string>;
  fetchBusiness?: (request: Request) => Promise<Response>;
  registerRepository?: RegisterRepository;
  prepareSources?: PrepareProjectSources;
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
  private tail: Promise<void> = Promise.resolve();
  private closed = false;
  private readonly cwd: string;

  constructor(private readonly options: ForemanCoordinatorOptions) {
    this.cwd = options.cwd ?? harnessControlPlaneRoot;
  }

  dispatch(id: string): Promise<void> {
    const existing = this.active.get(id);
    if (existing) return existing;
    const work = this.tail.then(() => this.run(id)).finally(() => this.active.delete(id));
    this.tail = work.catch(() => {});
    this.active.set(id, work);
    return work;
  }

  pendingReplies(): Record<string, string> { return Object.fromEntries(this.replies); }

  turnState(envelope: Omit<ForemanTurnState, "cancellable">): ForemanTurnState {
    const { id, messageId, status } = envelope;
    return { id, messageId, status, cancellable: status === "queued" || (status === "claimed" && this.turns.has(id)) };
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
        return { cancelled: true, foremanTurn: committed.foremanTurn };
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
    if (profile.image && this.options.ensureRuntime) {
      try {
        await this.options.ensureRuntime();
      } catch (error) {
        if (this.closed || this.options.store.getForemanMessage(id)?.status !== "queued") return;
        this.options.events.publish("foreman.session.failed", { inboxId: id, reason: error instanceof Error ? error.message : "container-runtime-unavailable" });
        return;
      }
    }
    if (this.closed) return;
    const claimed = this.options.store.claimForemanMessage(id);
    if (!claimed) return;
    const sessionId = `${definition.id}:${claimed.messageId}`;
    const turn = new HarnessTurn(profile.sessionTimeoutSeconds);
    const owner: ActiveForemanTurn = { turn };
    this.turns.set(id, owner);
    const credential = foremanCredential(profile);
    const useAcp = chooseTransport(definition, Boolean(definition.id === "kimi-code" || this.options.acpSpawn || process.env.MAO_ACP_AVAILABLE === "1")) === "acp";
    const transport = useAcp
      ? new AcpTransport(definition, turn.ownProcess(this.options.acpSpawn ?? defaultAcpSpawn))
      : new OfficialCliTransport(definition, turn.ownCliProcess(this.options.spawn), this.options.store.clock);
    let mailbox: ReturnType<typeof openForemanMailbox> | undefined;
    let sourceMounts: PreparedProjectSources | undefined;
    let finishing = false;
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
      const resumeId = persistent ? this.options.store.foremanSession(profile.id) : undefined;
      let officialId = resumeId;
      const message = this.options.store.talkMessageMetadata(claimed.messageId);
      const intake = message.source === "project-intake";
      const history = (profile.image || (persistent && !resumeId)) && !intake
        ? this.options.store.previousForemanTurns(id).map(({ user, foreman }) => ({
          user: redactForemanText(user, credential?.value),
          foreman: foreman === undefined ? undefined : redactForemanText(foreman, credential?.value),
        }))
        : [];
      const project = intake && typeof message.project_id === "string" ? this.options.store.getProject(message.project_id) : undefined;
      if (project && profile.image && this.options.prepareSources) {
        const prepared = this.options.prepareSources(project).then((result) => {
          if (finishing) result?.close();
          else sourceMounts = result;
          return result;
        });
        await turn.wait(prepared);
      }
      const sources = sourceMounts?.facts ?? (project ? projectSourceFacts(project, !profile.image) : undefined);
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
      if (resumeId) owner.resume = { profileId: profile.id, sessionId: resumeId };
      const session = await turn.start(transport, {
        task: `${history.length ? `历史对话（上下文数据，请以当前来信为准）\n${JSON.stringify(history)}\n\n当前来信\n` : ""}${claimed.text}${projectContext}${sources?.length ? `\n\n本次项目来源（数据）\n${JSON.stringify(sources)}` : ""}\n\n当前业务系统摘要\n${systems.map((system) => `- ${system.id} ${system.name} (${system.status})`).join("\n") || "- 尚未建立业务系统"}`,
        contract: "你是厂长。通过官方 Harness 管理业务系统，维护 Operator、Machine、DAG 和产物物料转换。用人能读懂的语言实时汇报进展。控制面只负责分发信息，不替你调用模型。"
          + (mailbox ? foremanMailboxContract : nativeBusinessContract + " "
            + (intake ? "使用本次项目和业务系统 ID 处理已经登记的项目。" : nativeRegistrationContract)),
        model: profile.model,
        env: {
          ...(!profile.image ? this.options.factoryEnv : undefined), ...harnessEndpointEnv(definition, credential?.baseUrl ?? profile.baseUrl),
          ...(credential ? { [credential.variable]: credential.value } : undefined),
        },
        cwd: this.cwd,
        resumeId,
        containerImage: profile.image,
        network: profile.network === "egress" ? "egress" : "none",
        egressBaseUrl: credential?.baseUrl ?? profile.baseUrl,
        foremanMailbox: mailbox?.root,
        projectSources: sourceMounts?.mounts,
      });
      if (stopped()) return;
      const safeSessionId = (value?: string) => value && (!credential || !value.includes(credential.value)) ? value : undefined;
      if (useAcp) officialId = safeSessionId(session.id);
      this.replies.set(id, "");
      let reply = "";
      let failure: string | undefined;
      let receivedDelta = false;
      const redactor = new ForemanReplyRedactor(credential?.value);
      const appendReply = (text: string) => {
        reply += text;
        this.replies.set(id, reply);
      };
      for await (const event of turn.events(session)) {
        if (stopped()) return;
        const eventSession = safeSessionId(event.sessionId);
        if (eventSession && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/.test(eventSession)) officialId = eventSession;
        const idForEvent = eventSession ?? safeSessionId(session.id) ?? sessionId;
        const isReply = event.type === "text.delta" || (event.type === "session.finished" && !receivedDelta);
        const text = event.text === undefined ? undefined : isReply ? redactor.write(event.text) : redactForemanText(event.text, credential?.value);
        if (event.type === "text.delta" && event.text) receivedDelta = true;
        const payload = { inboxId: id, sessionId: idForEvent, harness: definition.id, text };
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
        if (event.type === "session.failed") failure = text || "official-harness-failed";
      }
      if (stopped()) return;
      if (failure) {
        fail(failure);
        return;
      }
      if (mailbox) await turn.wait(mailbox.flush());
      if (stopped()) return;
      const finalDelta = redactor.finish();
      if (finalDelta) {
        const event = this.options.store.appendEvent("foreman.reply.delta", {
          inboxId: id, sessionId: safeSessionId(session.id) ?? sessionId, harness: definition.id, text: finalDelta,
        });
        appendReply(finalDelta);
        this.options.events.notifyCommitted(event);
      }
      const replyBody = reply.trim();
      const committed = this.options.store.finishForemanTurn(id, {
        reply: replyBody ? { body: replyBody, metadata: { turn_id: safeSessionId(session.id) ?? sessionId, harness: definition.id } } : undefined,
        session: persistent && officialId && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/.test(officialId)
          ? { profileId: profile.id, sessionId: officialId } : undefined,
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
          sourceMounts?.close();
        }
      }
    }
  }
}
