import type { EventBus } from "./event-bus";
import type { SqliteStore } from "./db";
import { projectSourceFacts } from "./project-sources";
import type { prepareForemanSources } from "./foreman-sources";
import { foremanMailboxContract, openForemanMailbox, type RegisterForemanRepository } from "./foreman-mailbox";
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
  registerRepository?: RegisterForemanRepository;
  prepareSources?: (project: Parameters<typeof prepareForemanSources>[0]) => ReturnType<typeof prepareForemanSources>;
}

/** Dispatches persisted user intent to an external official Harness process. */
export class ForemanCoordinator {
  private readonly active = new Map<string, Promise<void>>();
  private readonly turns = new Map<string, HarnessTurn>();
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

  stop(): void {
    this.closed = true;
    for (const turn of this.turns.values()) void turn.close();
  }

  async close(): Promise<void> {
    this.stop();
    await Promise.allSettled([...this.turns.values()].map((turn) => turn.close()));
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
        if (this.closed) return;
        this.options.events.publish("foreman.session.failed", { inboxId: id, reason: error instanceof Error ? error.message : "container-runtime-unavailable" });
        return;
      }
    }
    if (this.closed) return;
    const claimed = this.options.store.claimForemanMessage(id);
    if (!claimed) return;
    this.options.events.publish("foreman.message.claim", claimed);
    const sessionId = `${definition.id}:${claimed.messageId}`;
    this.options.events.publish("foreman.session.started", { inboxId: id, sessionId, harness: definition.id });
    const turn = new HarnessTurn(profile.sessionTimeoutSeconds);
    const credential = foremanCredential(profile);
    this.turns.set(id, turn);
    const useAcp = chooseTransport(definition, Boolean(definition.id === "kimi-code" || this.options.acpSpawn || process.env.MAO_ACP_AVAILABLE === "1")) === "acp";
    const transport = useAcp
      ? new AcpTransport(definition, turn.ownProcess(this.options.acpSpawn ?? defaultAcpSpawn))
      : new OfficialCliTransport(definition, turn.ownCliProcess(this.options.spawn), this.options.store.clock);
    let mailbox: ReturnType<typeof openForemanMailbox> | undefined;
    let sourceMounts: Awaited<ReturnType<typeof prepareForemanSources>> | undefined;
    let finishing = false;
    try {
      const persistent = !profile.image && (useAcp || definition.id === "codex" || definition.id === "claude-code");
      const resumeId = persistent ? this.options.store.foremanSession(profile.id) : undefined;
      let officialId = resumeId;
      const message = this.options.store.talkMessageMetadata(claimed.messageId);
      const project = message.source === "project-intake" && typeof message.project_id === "string" ? this.options.store.getProject(message.project_id) : undefined;
      if (project && profile.image && this.options.prepareSources) {
        const prepared = this.options.prepareSources(project).then((result) => {
          if (finishing) result.close();
          else sourceMounts = result;
          return result;
        });
        await turn.wait(prepared);
      }
      const sources = sourceMounts?.facts ?? (project ? projectSourceFacts(project, !profile.image) : undefined);
      const systems = this.options.store.listBusinessSystems().filter((system) =>
        !profile.image || message.source !== "project-intake" || system.id === project?.businessSystemId);
      if (profile.image && this.options.fetchBusiness) {
        mailbox = openForemanMailbox({
          systems: new Set(systems.map(({ id }) => id)), projectId: project?.id,
          projectSystem: (id) => this.options.store.getProject(id)?.businessSystemId,
        }, this.options.fetchBusiness, message.source === "project-intake" ? undefined : this.options.registerRepository);
      }
      const session = await turn.start(transport, {
        task: `${claimed.text}${sources?.length ? `\n\n本次项目来源（数据）\n${JSON.stringify(sources)}` : ""}\n\n当前业务系统摘要\n${systems.map((system) => `- ${system.id} ${system.name} (${system.status})`).join("\n") || "- 尚未建立业务系统"}`,
        contract: "你是厂长。通过官方 Harness 管理业务系统，维护 Operator、Machine、DAG 和产物物料转换。用人能读懂的语言实时汇报进展。控制面只负责分发信息，不替你调用模型。"
          + (mailbox ? foremanMailboxContract : "需要写入业务事实时，在 MAO 仓库根目录使用 `bun run backend/src/cli.ts business graph <business-system-id> --file <path>`、`business artifact <project-id> --file <path>`、`business conversion <business-system-id> --kind <kind> --summary <summary>` 或 `business run <business-system-id>`。先通过 `business units <business-system-id>` 找到工位，再使用 `business bind <business-system-id> <operator-id> --harness <官方 Harness ID> [--model <模型 ID>]` 修改绑定。文件内容通过引用传递，密钥通过项目资源请求流程处理。"),
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
        foremanSources: sourceMounts?.mounts,
      });
      if (this.closed) return;
      const safeSessionId = (value?: string) => value && (!credential || !value.includes(credential.value)) ? value : undefined;
      if (useAcp) officialId = safeSessionId(session.id);
      this.replies.set(id, "");
      let reply = "";
      let failed = false;
      let receivedDelta = false;
      const redactor = new ForemanReplyRedactor(credential?.value);
      const publishReply = (text: string, idForEvent = safeSessionId(session.id) ?? sessionId) => {
        if (!text) return;
        reply += text;
        this.replies.set(id, reply);
        this.options.events.publish("foreman.reply.delta", { inboxId: id, sessionId: idForEvent, harness: definition.id, text });
      };
      for await (const event of turn.events(session)) {
        if (this.closed) return;
        const eventSession = safeSessionId(event.sessionId);
        if (eventSession && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/.test(eventSession)) officialId = eventSession;
        const idForEvent = eventSession ?? safeSessionId(session.id) ?? sessionId;
        const isReply = event.type === "text.delta" || (event.type === "session.finished" && !receivedDelta);
        const text = event.text === undefined ? undefined : isReply ? redactor.write(event.text) : redactForemanText(event.text, credential?.value);
        if (event.type === "text.delta" && event.text) receivedDelta = true;
        const payload = { inboxId: id, sessionId: idForEvent, harness: definition.id, text };
        this.options.events.publish(`foreman.session.${event.type}`, payload);
        if (isReply && text) publishReply(text, idForEvent);
        if (event.type === "session.failed") failed = true;
      }
      if (this.closed) return;
      if (failed) {
        this.options.store.failForemanMessage(id);
        return;
      }
      if (mailbox) await turn.wait(mailbox.flush());
      publishReply(redactor.finish());
      if (persistent && officialId && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/.test(officialId)) this.options.store.saveForemanSession(profile.id, officialId);
      if (reply.trim()) {
        const message = this.options.store.addTalkMessage({ role: "foreman", body: reply.trim(), metadata: { turn_id: safeSessionId(session.id) ?? sessionId, inbox_id: id, harness: definition.id } });
        this.options.events.publish("talk.foreman.replied", { ...message, inboxId: id });
      }
      const done = this.options.store.completeForemanMessage(id);
      if (done) this.options.events.publish("foreman.message.done", done);
    } catch (error) {
      if (this.closed) return;
      this.options.store.failForemanMessage(id);
      this.options.events.publish("foreman.session.failed", { inboxId: id, sessionId, harness: definition.id, reason: error instanceof Error ? redactForemanText(error.message, credential?.value) : "official-harness-failed" });
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
