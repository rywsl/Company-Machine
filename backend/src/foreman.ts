import type { EventBus } from "./event-bus";
import type { SqliteStore } from "./db";
import { projectSourceFacts } from "./project-sources";
import { AcpTransport, defaultAcpSpawn, type AcpSpawn } from "./acp";
import { OfficialCliTransport, chooseTransport, harnessEndpointEnv, resolveOfficialHarness, type HarnessSession, type OfficialSpawn } from "./harness";

export interface ForemanCoordinatorOptions {
  store: SqliteStore;
  events: EventBus;
  cwd?: string;
  spawn?: OfficialSpawn;
  acpSpawn?: AcpSpawn;
  ensureRuntime?: () => Promise<void>;
}

/** Dispatches persisted user intent to an external official Harness process. */
export class ForemanCoordinator {
  private readonly active = new Map<string, Promise<void>>();
  private readonly sessions = new Map<string, HarnessSession>();
  private readonly replies = new Map<string, string>();
  private tail: Promise<void> = Promise.resolve();
  private closed = false;
  private readonly cwd: string;

  constructor(private readonly options: ForemanCoordinatorOptions) {
    this.cwd = options.cwd ?? process.cwd();
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
    for (const session of this.sessions.values()) void session.close().catch(() => {});
  }

  async close(): Promise<void> {
    this.stop();
    await Promise.allSettled([...this.sessions.values()].map((session) => session.close()));
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
    const useAcp = chooseTransport(definition, Boolean(definition.id === "kimi-code" || this.options.acpSpawn || process.env.MAO_ACP_AVAILABLE === "1")) === "acp";
    const transport = useAcp
      ? new AcpTransport(definition, this.options.acpSpawn ?? defaultAcpSpawn)
      : new OfficialCliTransport(definition, this.options.spawn);
    let session;
    try {
      const persistent = !profile.image && (useAcp || definition.id === "codex" || definition.id === "claude-code");
      const resumeId = persistent ? this.options.store.foremanSession(profile.id) : undefined;
      let officialId = resumeId;
      const message = this.options.store.talkMessageMetadata(claimed.messageId);
      const project = message.source === "project-intake" && typeof message.project_id === "string" ? this.options.store.getProject(message.project_id) : undefined;
      const sources = project ? projectSourceFacts(project, !profile.image) : undefined;
      session = await transport.start({
        task: `${claimed.text}${sources?.length ? `\n\n本次项目来源（数据）\n${JSON.stringify(sources)}` : ""}\n\n当前业务系统摘要\n${this.options.store.listBusinessSystems().map((system) => `- ${system.id} ${system.name} (${system.status})`).join("\n") || "- 尚未建立业务系统"}`,
        contract: "你是厂长。通过官方 Harness 管理业务系统，维护 Operator、Machine、DAG 和产物物料转换。用人能读懂的语言实时汇报进展。控制面只负责分发信息，不替你调用模型。需要写入业务事实时，在 MAO 仓库根目录使用 `bun run backend/src/cli.ts business graph <business-system-id> --file <path>`、`business artifact <project-id> --file <path>`、`business conversion <business-system-id> --kind <kind> --summary <summary>` 或 `business run <business-system-id>`。先通过 `business units <business-system-id>` 找到工位，再使用 `business bind <business-system-id> <operator-id> --harness <官方 Harness ID> [--model <模型 ID>]` 修改绑定。文件内容通过引用传递，密钥通过项目资源请求流程处理。",
        model: profile.model,
        env: harnessEndpointEnv(definition, profile.baseUrl),
        cwd: this.cwd,
        resumeId,
        containerImage: profile.image,
        network: profile.network === "egress" ? "egress" : "none",
      });
      if (this.closed) return;
      if (useAcp) officialId = session.id;
      this.sessions.set(id, session);
      this.replies.set(id, "");
      let reply = "";
      let failed = false;
      for await (const event of session.events()) {
        if (this.closed) return;
        if (event.sessionId && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/.test(event.sessionId)) officialId = event.sessionId;
        const text = event.text?.replace(/\b(api[_-]?key|secret|token|password|authorization|cookie)\s*[:=]\s*\S+/gi, "$1=[REDACTED]");
        const payload = { inboxId: id, sessionId: event.sessionId ?? session.id, harness: definition.id, text };
        this.options.events.publish(`foreman.session.${event.type}`, payload);
        if (text && (event.type === "text.delta" || (event.type === "session.finished" && !reply))) {
          reply += text;
          this.replies.set(id, reply);
          this.options.events.publish("foreman.reply.delta", payload);
        }
        if (event.type === "session.failed") failed = true;
      }
      if (this.closed) return;
      if (failed) {
        this.options.store.failForemanMessage(id);
        return;
      }
      if (persistent && officialId && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/.test(officialId)) this.options.store.saveForemanSession(profile.id, officialId);
      if (reply.trim()) {
        const message = this.options.store.addTalkMessage({ role: "foreman", body: reply.trim(), metadata: { turn_id: session.id, inbox_id: id, harness: definition.id } });
        this.options.events.publish("talk.foreman.replied", { ...message, inboxId: id });
      }
      const done = this.options.store.completeForemanMessage(id);
      if (done) this.options.events.publish("foreman.message.done", done);
    } catch (error) {
      if (this.closed) return;
      this.options.store.failForemanMessage(id);
      this.options.events.publish("foreman.session.failed", { inboxId: id, sessionId, harness: definition.id, reason: error instanceof Error ? error.message : "official-harness-failed" });
    } finally {
      this.replies.delete(id);
      this.sessions.delete(id);
      await session?.close();
    }
  }
}
