import type { EventBus } from "./event-bus";
import type { SqliteStore } from "./db";
import { AcpTransport, defaultAcpSpawn, type AcpSpawn } from "./acp";
import { OfficialCliTransport, chooseTransport, resolveOfficialHarness, type HarnessSession, type OfficialSpawn } from "./harness";

export interface ForemanCoordinatorOptions {
  store: SqliteStore;
  events: EventBus;
  cwd?: string;
  spawn?: OfficialSpawn;
  acpSpawn?: AcpSpawn;
}

/** Dispatches persisted user intent to an external official Harness process. */
export class ForemanCoordinator {
  private readonly active = new Map<string, Promise<void>>();
  private readonly sessions = new Map<string, HarnessSession>();
  private closed = false;
  private readonly cwd: string;

  constructor(private readonly options: ForemanCoordinatorOptions) {
    this.cwd = options.cwd ?? process.cwd();
  }

  dispatch(id: string): Promise<void> {
    const existing = this.active.get(id);
    if (existing) return existing;
    const work = this.run(id).finally(() => this.active.delete(id));
    this.active.set(id, work);
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

  private async run(id: string): Promise<void> {
    if (this.closed) return;
    const envelope = this.options.store.listForemanInbox(200).find((item) => item.id === id);
    if (!envelope || envelope.status !== "queued") return;
    const profile = this.options.store.listHarnessProfiles().find((item) => item.role === "foreman");
    const definition = profile ? resolveOfficialHarness(profile.runtime) : undefined;
    if (!profile || !definition) {
      this.options.events.publish("foreman.session.failed", { inboxId: id, reason: "foreman-harness-not-configured" });
      return;
    }
    const claimed = this.options.store.claimForemanMessage(id);
    if (!claimed) return;
    this.options.events.publish("foreman.message.claim", claimed);
    const sessionId = `${definition.id}:${claimed.messageId}`;
    this.options.events.publish("foreman.session.started", { inboxId: id, sessionId, harness: definition.id });
    const useAcp = chooseTransport(definition, Boolean(this.options.acpSpawn || process.env.MAO_ACP_AVAILABLE === "1")) === "acp";
    const transport = useAcp
      ? new AcpTransport(definition, this.options.acpSpawn ?? defaultAcpSpawn)
      : new OfficialCliTransport(definition, this.options.spawn);
    let session;
    try {
      session = await transport.start({
        task: `${claimed.text}\n\n当前业务系统摘要\n${this.options.store.listBusinessSystems().map((system) => `- ${system.id} ${system.name} (${system.status})`).join("\n") || "- 尚未建立业务系统"}`,
        contract: "你是厂长。通过官方 Harness 管理业务系统，维护 Operator、Machine、DAG 和产物物料转换。用人能读懂的语言实时汇报进展。控制面只负责分发信息，不替你调用模型。需要写入业务事实时，在 MAO 仓库根目录使用 `bun run backend/src/cli.ts business graph <business-system-id> --file <path>`、`business artifact <project-id> --file <path>`、`business conversion <business-system-id> --kind <kind> --summary <summary>` 或 `business run <business-system-id>`。文件内容通过引用传递，密钥通过项目资源请求流程处理。",
        model: profile.model,
        cwd: this.cwd,
        resumeId: sessionId,
        containerImage: profile.image,
        network: profile.network === "egress" ? "egress" : "none",
      });
      this.sessions.set(id, session);
      let reply = "";
      let failed = false;
      for await (const event of session.events()) {
        const payload = { inboxId: id, sessionId: event.sessionId ?? session.id, harness: definition.id, text: event.text, metadata: event.metadata };
        this.options.events.publish(`foreman.session.${event.type}`, payload);
        if (event.text) {
          reply += event.text;
          this.options.events.publish("foreman.reply.delta", payload);
        }
        if (event.type === "session.failed") failed = true;
      }
      if (failed) return;
      if (reply.trim()) {
        const message = this.options.store.addTalkMessage({ role: "foreman", body: reply.trim(), metadata: { turn_id: session.id, harness: definition.id } });
        this.options.events.publish("talk.foreman.replied", message);
      }
      const done = this.options.store.completeForemanMessage(id);
      if (done) this.options.events.publish("foreman.message.done", done);
    } catch (error) {
      this.options.events.publish("foreman.session.failed", { inboxId: id, sessionId, harness: definition.id, reason: error instanceof Error ? error.message : "official-harness-failed" });
    } finally {
      this.sessions.delete(id);
      await session?.close();
    }
  }
}
