import type { HarnessDefinition, HarnessEvent, HarnessSession, HarnessSessionRequest, HarnessTransport } from "./harness";

export type AcpProcess = {
  stdout: ReadableStream<Uint8Array>;
  stdin?: AcpInput;
  kill(): void;
};

export type AcpInput = {
  write(data: Uint8Array): Promise<void> | void;
  end?(): Promise<void> | void;
};

export type AcpSpawn = (argv: string[], options: { cwd: string; env: Record<string, string>; stdout: "pipe"; stdin: "pipe" }) => AcpProcess;

export function decodeAcpLine(line: string): HarnessEvent | null {
  let message: Record<string, unknown>;
  try {
    const parsed = JSON.parse(line);
    if (!parsed || typeof parsed !== "object") return null;
    message = parsed as Record<string, unknown>;
  } catch {
    return null;
  }
  if ("id" in message && !message.method && !message.type) return null;
  const method = typeof message.method === "string" ? message.method : typeof message.type === "string" ? message.type : "";
  const params = message.params && typeof message.params === "object" ? message.params as Record<string, unknown> : message;
  const update = params.update && typeof params.update === "object" ? params.update as Record<string, unknown> : undefined;
  const content = update?.content && typeof update.content === "object" ? update.content as Record<string, unknown> : undefined;
  const text = typeof params.text === "string" ? params.text
    : typeof params.delta === "string" ? params.delta
      : typeof update?.text === "string" ? update.text
        : typeof content?.text === "string" ? content.text : undefined;
  const eventType: HarnessEvent["type"] = method.includes("started") || method.includes("created") ? "session.started"
    : method === "mao.record" || method === "record" ? "record"
    : method.includes("tool") && method.includes("start") ? "tool.started"
      : method.includes("tool") ? "tool.finished"
        : method.includes("failed") || method.includes("error") ? "session.failed"
          : method.includes("finished") || method.includes("complete") ? "session.finished" : "text.delta";
  return { type: eventType, sessionId: typeof params.sessionId === "string" ? params.sessionId : undefined, text, metadata: params };
}

class AcpSession implements HarnessSession {
  readonly id: string;
  private closed = false;
  private readonly reader: ReadableStreamDefaultReader<Uint8Array>;
  private readonly decoder = new TextDecoder();
  private pending = "";
  private readonly queued: string[] = [];

  constructor(private readonly process: AcpProcess, id: string) {
    this.id = id;
    this.reader = process.stdout.getReader();
  }

  async initialize(request: HarnessSessionRequest): Promise<void> {
    if (!this.process.stdin) throw new Error("ACP process stdin is unavailable");
    await this.request(1, "initialize", {
      protocolVersion: 1,
      clientInfo: { name: "machine-agent-org", version: "1" },
      clientCapabilities: {},
    });
    let sessionId: string | undefined;
    if (request.resumeId) {
      try {
        const loaded = await this.request(2, "session/load", { sessionId: request.resumeId, cwd: request.cwd });
        sessionId = this.resultSessionId(loaded) ?? request.resumeId;
      } catch {
        const created = await this.request(2, "session/new", { cwd: request.cwd, mcpServers: [] });
        sessionId = this.resultSessionId(created);
      }
    } else {
      const created = await this.request(2, "session/new", { cwd: request.cwd, mcpServers: [] });
      sessionId = this.resultSessionId(created);
    }
    if (!sessionId) throw new Error("ACP did not return a session id");
    await this.write({ jsonrpc: "2.0", id: 3, method: "session/prompt", params: {
      sessionId,
      prompt: [{ type: "text", text: `${request.contract}\n\n${request.task}` }],
    } });
  }

  private resultSessionId(message: Record<string, unknown>): string | undefined {
    const result = message.result && typeof message.result === "object" ? message.result as Record<string, unknown> : undefined;
    return typeof result?.sessionId === "string" ? result.sessionId : undefined;
  }

  private async write(message: Record<string, unknown>): Promise<void> {
    if (!this.process.stdin) throw new Error("ACP process stdin is unavailable");
    await this.process.stdin.write(new TextEncoder().encode(`${JSON.stringify(message)}\n`));
  }

  private async readLine(): Promise<string | undefined> {
    while (true) {
      const newline = this.pending.search(/\r?\n/);
      if (newline >= 0) {
        const line = this.pending.slice(0, newline);
        this.pending = this.pending.slice(this.pending[newline] === "\r" ? newline + 2 : newline + 1);
        return line;
      }
      const chunk = await this.reader.read();
      if (chunk.done) {
        if (!this.pending) return undefined;
        const line = this.pending;
        this.pending = "";
        return line;
      }
      this.pending += this.decoder.decode(chunk.value, { stream: true });
    }
  }

  private async request(id: number, method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    await this.write({ jsonrpc: "2.0", id, method, params });
    while (!this.closed) {
      const line = await this.readLine();
      if (line === undefined) throw new Error(`ACP closed before ${method} response`);
      let message: unknown;
      try { message = JSON.parse(line); } catch { continue; }
      if (!message || typeof message !== "object") continue;
      const record = message as Record<string, unknown>;
      if (record.id === id) {
        if (record.error && typeof record.error === "object") throw new Error(`ACP ${method} failed`);
        return record;
      }
      this.queued.push(line);
    }
    throw new Error("ACP session closed");
  }

  async *events(): AsyncIterable<HarnessEvent> {
    try {
      while (!this.closed) {
        const line = this.queued.shift() ?? await this.readLine();
        if (line === undefined) break;
        const event = decodeAcpLine(line);
        if (event) yield event;
      }
    } finally {
      this.reader.releaseLock();
    }
  }

  async close(): Promise<void> {
    this.closed = true;
    await this.process.stdin?.end?.();
    this.process.kill();
  }
}

export class AcpTransport implements HarnessTransport {
  constructor(private readonly definition: HarnessDefinition, private readonly spawn: AcpSpawn, private readonly argvSuffix: string[] = []) {}

  async start(request: HarnessSessionRequest): Promise<HarnessSession> {
    const process = this.spawn([this.definition.executable, ...this.argvSuffix], { cwd: request.cwd, env: request.env ?? {}, stdout: "pipe", stdin: "pipe" });
    const session = new AcpSession(process, request.resumeId ?? `${this.definition.id}:${Date.now()}`);
    try {
      await session.initialize(request);
      return session;
    } catch (error) {
      await session.close();
      throw error;
    }
  }
}

export function defaultAcpSpawn(argv: string[], options: { cwd: string; env: Record<string, string>; stdout: "pipe"; stdin: "pipe" }): AcpProcess {
  const bun = (globalThis as { Bun?: { spawn: (argv: string[], options: Record<string, unknown>) => any } }).Bun;
  if (!bun) throw new Error("Bun runtime is required");
  const process = bun.spawn(argv, { cwd: options.cwd, env: options.env, stdout: options.stdout, stdin: options.stdin, stderr: "pipe" });
  const stdin = process.stdin && typeof process.stdin.write === "function" ? {
    write: async (data: Uint8Array) => { await process.stdin.write(data); await process.stdin.flush?.(); },
    end: async () => { await process.stdin.end?.(); },
  } : undefined;
  return { stdout: process.stdout as ReadableStream<Uint8Array>, stdin, kill: () => process.kill() };
}
