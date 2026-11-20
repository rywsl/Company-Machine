import type { HarnessDefinition, HarnessEvent, HarnessSession, HarnessSessionRequest, HarnessTransport } from "./harness";
import { spawnOfficialProcess } from "./harness";
import type { ProjectSourceMount } from "./project-source-mounts";
import { waitForHarness } from "./harness-turn";

export type AcpProcess = {
  stdout: ReadableStream<Uint8Array>;
  stdin?: AcpInput;
  kill(): void | Promise<void>;
};

export type AcpInput = {
  write(data: Uint8Array): Promise<void> | void;
  end?(): Promise<void> | void;
};

export type AcpSpawn = (argv: string[], options: { cwd: string; env: Record<string, string>; stdout: "pipe"; stdin: "pipe"; containerImage?: string; network?: "none" | "egress"; egressBaseUrl?: string; foremanMailbox?: string; projectSources?: ProjectSourceMount[] }) => AcpProcess | Promise<AcpProcess>;

const PROMPT_ID = 3;
const START_TIMEOUT_MS = 30_000;
const MAX_FRAME_BYTES = 1024 * 1024;
const MAX_PENDING_EVENTS = 128;
const MAX_PENDING_BYTES = 4 * MAX_FRAME_BYTES;
const STOP_REASONS = new Set(["end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"]);

function object(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

function modelOption(state: Record<string, unknown>): Record<string, unknown> | undefined {
  return Array.isArray(state.configOptions) ? state.configOptions.map(object)
    .find((option) => option?.category === "model" && option.type === "select") : undefined;
}

function hasOptionValue(options: unknown, model: string): boolean {
  return Array.isArray(options) && options.some((entry) => {
    const option = object(entry);
    return option?.value === model || (Array.isArray(option?.options) && option.options.some((item) => object(item)?.value === model));
  });
}

function decodeLine(line: string): Record<string, unknown> | undefined {
  try {
    return object(JSON.parse(line));
  } catch {
    return undefined;
  }
}

export function decodeAcpLine(line: string): HarnessEvent | null {
  const message = decodeLine(line);
  return message ? decodeAcpEvent(message) : null;
}

function decodeAcpEvent(message: Record<string, unknown>): HarnessEvent | null {
  if ("id" in message) return null;
  const method = typeof message.method === "string" ? message.method : typeof message.type === "string" ? message.type : "";
  const params = object(message.params) ?? message;
  const update = object(params.update);
  const content = object(update?.content);
  const discriminator = update?.sessionUpdate;
  const text = [params.text, params.delta, update?.text, content?.text].find((value): value is string => typeof value === "string");
  let eventType: HarnessEvent["type"];
  if (method === "session/update") {
    if (discriminator === "tool_call" || discriminator === "tool_call_update") {
      if (update?.status === "completed" || update?.status === "failed") eventType = "tool.finished";
      else if (discriminator === "tool_call" || update?.status === "in_progress") eventType = "tool.started";
      else return null;
    } else {
      if ((discriminator !== undefined && discriminator !== "agent_message_chunk") || text === undefined) return null;
      eventType = "text.delta";
    }
  } else if (method === "mao.record" || method === "record") eventType = "record";
  else if (method === "secret.request" || method === "mao.secret_request" || method === "mao.secret.request") eventType = "secret.request";
  else if (method === "session/started" || method === "session.started" || method === "session/created") eventType = "session.started";
  else if (method === "session/complete" || method === "session/finished" || method === "session.finished") eventType = "session.finished";
  else if (method === "session/failed" || method === "session.failed" || method === "session/error") eventType = "session.failed";
  else return null;
  return { type: eventType, sessionId: typeof params.sessionId === "string" ? params.sessionId : undefined, text, metadata: params };
}

class AcpRpcError extends Error {
  constructor(method: string, readonly code: number | undefined) {
    super("ACP " + method + " failed" + (code === undefined ? "" : " (JSON-RPC " + code + ")"));
  }
}

function responseResult(message: Record<string, unknown>, method: string): Record<string, unknown> {
  if (message.error !== undefined) {
    const error = object(message.error);
    throw new AcpRpcError(method, typeof error?.code === "number" ? error.code : undefined);
  }
  const result = object(message.result);
  if (!result) throw new Error("ACP returned an invalid " + method + " response");
  return result;
}

class AcpSession implements HarnessSession {
  private sessionId = "";
  private closed = false;
  private readonly reader: ReadableStreamDefaultReader<Uint8Array>;
  private readonly decoder = new TextDecoder("utf-8", { fatal: true });
  private chunk: Uint8Array | undefined;
  private offset = 0;
  private readonly queued: HarnessEvent[] = [];
  private queuedBytes = 0;
  private frameBytes = 0;

  constructor(private readonly process: AcpProcess) {
    this.reader = process.stdout.getReader();
  }

  get id(): string { return this.sessionId; }

  async initialize(request: HarnessSessionRequest, nativeModel: boolean): Promise<void> {
    const initialized = await this.request(1, "initialize", {
      protocolVersion: 1,
      clientInfo: { name: "machine-agent-org", version: "1" },
      clientCapabilities: {},
    });
    if (initialized.protocolVersion !== 1) throw new Error("ACP agent did not negotiate protocol version 1");
    const workspace = { cwd: request.containerImage ? "/tmp" : request.cwd, mcpServers: [] };
    let nextId = 2;
    const requestId = () => { if (nextId === PROMPT_ID) nextId++; return nextId++; };
    let sessionState: Record<string, unknown> = {};
    if (request.resumeId && object(initialized.agentCapabilities)?.loadSession === true) {
      try {
        const loaded = await this.request(requestId(), "session/load", { ...workspace, sessionId: request.resumeId });
        this.sessionId = typeof loaded.sessionId === "string" && loaded.sessionId.trim() ? loaded.sessionId : request.resumeId;
        sessionState = loaded;
      } catch (error) {
        if (!(error instanceof AcpRpcError)) throw error;
      }
    }
    if (!this.sessionId) {
      const created = await this.request(requestId(), "session/new", workspace);
      if (typeof created.sessionId !== "string" || !created.sessionId.trim()) throw new Error("ACP did not return a session id");
      this.sessionId = created.sessionId;
      sessionState = created;
    }
    const option = modelOption(sessionState);
    if (!option && request.requireModelSelection && !nativeModel) {
      throw new Error("此官方 Harness 未提供 ACP 模型选择");
    }
    if (option && option.currentValue !== request.model) {
      if (typeof option.id !== "string" || !hasOptionValue(option.options, request.model)) {
        throw new Error("此官方 Harness 未提供配置的模型");
      }
      const updated = await this.request(requestId(), "session/set_config_option", {
        sessionId: this.id, configId: option.id, value: request.model,
      });
      if (modelOption(updated)?.currentValue !== request.model) throw new Error("官方 Harness 未确认配置的模型");
    }
    await this.write({ jsonrpc: "2.0", id: PROMPT_ID, method: "session/prompt", params: {
      sessionId: this.id,
      prompt: [{ type: "text", text: request.contract + "\n\n" + request.task }],
    } });
  }

  private async write(message: Record<string, unknown>): Promise<void> {
    if (!this.process.stdin) throw new Error("ACP process stdin is unavailable");
    const bytes = new TextEncoder().encode(`${JSON.stringify(message)}\n`);
    if (bytes.length - 1 > MAX_FRAME_BYTES) throw new Error("ACP message exceeds 1 MiB");
    await this.process.stdin.write(bytes);
  }

  private async readLine(): Promise<string | undefined> {
    const fragments: string[] = [];
    let length = 0;
    while (true) {
      if (!this.chunk || this.offset === this.chunk.length) {
        const next = await this.reader.read();
        if (next.done) {
          if (!length) return undefined;
          this.frameBytes = length;
          fragments.push(this.decoder.decode());
          return fragments.join("");
        }
        this.chunk = next.value;
        this.offset = 0;
      }
      const newline = this.chunk.indexOf(10, this.offset);
      const end = newline === -1 ? this.chunk.length : newline;
      length += end - this.offset;
      if (length > MAX_FRAME_BYTES) throw new Error("ACP message exceeds 1 MiB");
      const text = this.decoder.decode(this.chunk.subarray(this.offset, end), { stream: newline === -1 });
      if (text) fragments.push(text);
      this.offset = newline === -1 ? end : end + 1;
      if (newline !== -1) {
        this.frameBytes = length;
        const line = fragments.join("");
        return line.endsWith("\r") ? line.slice(0, -1) : line;
      }
    }
  }

  private async readMessage(): Promise<Record<string, unknown> | undefined> {
    while (!this.closed) {
      const line = await this.readLine();
      if (line === undefined || this.closed) return undefined;
      if (!line.trim()) continue;
      const message = decodeLine(line);
      if (!message) throw new Error("Invalid ACP JSON-RPC message");
      if (typeof message.method === "string" && (typeof message.id === "number" || typeof message.id === "string")) {
        // Answer unsupported client methods so the Agent can complete its turn.
        await this.write({ jsonrpc: "2.0", id: message.id, error: { code: -32601, message: "Client method not supported" } });
        continue;
      }
      return message;
    }
    return undefined;
  }

  private async request(id: number, method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    await this.write({ jsonrpc: "2.0", id, method, params });
    while (!this.closed) {
      const message = await this.readMessage();
      if (!message) throw new Error("ACP closed before " + method + " response");
      if (message.id === id && !message.method) return responseResult(message, method);
      // Updates replayed during session/load belong to the previous conversation.
      if (method === "session/load" || "id" in message) continue;
      const event = decodeAcpEvent(message);
      if (!event) continue;
      this.queuedBytes += this.frameBytes;
      if (this.queuedBytes > MAX_PENDING_BYTES || this.queued.length === MAX_PENDING_EVENTS) {
        throw new Error("Too many ACP notifications before session startup");
      }
      this.queued.push(event);
    }
    throw new Error("ACP session closed");
  }

  async *events(): AsyncIterable<HarnessEvent> {
    try {
      this.queuedBytes = 0;
      for (const event of this.queued.splice(0)) {
        if (this.closed) return;
        if (event.type !== "session.finished" && event.type !== "session.failed" && (!event.sessionId || event.sessionId === this.id)) {
          yield { ...event, sessionId: this.id };
        }
      }
      while (!this.closed) {
        const message = await this.readMessage();
        if (!message) {
          if (this.closed) return;
          throw new Error("ACP closed before session/prompt response");
        }
        if (message.id === PROMPT_ID) {
          const { stopReason } = responseResult(message, "session/prompt");
          if (typeof stopReason !== "string" || !STOP_REASONS.has(stopReason)) throw new Error("ACP returned an invalid prompt stop reason");
          const type = stopReason === "end_turn" ? "session.finished" : "session.failed";
          yield { type, sessionId: this.id, metadata: { stopReason } };
          return;
        }
        const event = decodeAcpEvent(message);
        if (!event || (event.sessionId && event.sessionId !== this.id)) continue;
        const terminal = event.type === "session.finished" || event.type === "session.failed";
        if (terminal && message.jsonrpc === "2.0") continue;
        yield { ...event, sessionId: this.id };
        if (terminal) return;
      }
    } catch (error) {
      if (this.closed) return;
      yield { type: "session.failed", sessionId: this.id, metadata: { reason: error instanceof Error ? error.message : "ACP transport failed" } };
    }
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    try {
      await this.process.kill();
    } finally {
      void this.reader.cancel().catch(() => {});
      this.reader.releaseLock();
      void Promise.resolve().then(() => this.process.stdin?.end?.()).catch(() => {});
    }
  }
}

export class AcpTransport implements HarnessTransport {
  constructor(private readonly definition: HarnessDefinition, private readonly spawn: AcpSpawn, private readonly argvSuffix: string[] = []) {}

  async start(request: HarnessSessionRequest, signal?: AbortSignal): Promise<HarnessSession> {
    const nativeModel = !this.argvSuffix.length && this.definition.id === "kimi-code";
    const args = this.argvSuffix.length ? this.argvSuffix
      : nativeModel ? ["--model", request.model, "acp"] : [];
    const process = await this.spawn([this.definition.executable, ...args], {
      cwd: request.cwd, env: request.env ?? {}, stdout: "pipe", stdin: "pipe", containerImage: request.containerImage, network: request.network,
      egressBaseUrl: request.egressBaseUrl, foremanMailbox: request.foremanMailbox, projectSources: request.projectSources,
    });
    const session = new AcpSession(process);
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      const startup = Promise.race([
        session.initialize(request, nativeModel),
        new Promise<never>((_, reject) => { timeout = setTimeout(() => reject(new Error("ACP session startup timed out")), START_TIMEOUT_MS); }),
      ]);
      await (signal ? waitForHarness(startup, signal) : startup);
      return session;
    } catch (error) {
      await session.close();
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }
}

export function defaultAcpSpawn(argv: string[], options: Parameters<AcpSpawn>[1]): Promise<AcpProcess> {
  return spawnOfficialProcess(argv, options);
}
