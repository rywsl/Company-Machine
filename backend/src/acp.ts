import type { HarnessDefinition, HarnessEvent, HarnessSession, HarnessSessionRequest, HarnessTransport } from "./harness";

export type AcpProcess = {
  stdout: ReadableStream<Uint8Array>;
  kill(): void;
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
  const method = typeof message.method === "string" ? message.method : typeof message.type === "string" ? message.type : "";
  const params = message.params && typeof message.params === "object" ? message.params as Record<string, unknown> : message;
  const text = typeof params.text === "string" ? params.text : typeof params.delta === "string" ? params.delta : undefined;
  const eventType: HarnessEvent["type"] = method.includes("started") || method.includes("created") ? "session.started"
    : method.includes("tool") && method.includes("start") ? "tool.started"
      : method.includes("tool") ? "tool.finished"
        : method.includes("failed") || method.includes("error") ? "session.failed"
          : method.includes("finished") || method.includes("complete") ? "session.finished" : "text.delta";
  return { type: eventType, sessionId: typeof params.sessionId === "string" ? params.sessionId : undefined, text, metadata: params };
}

class AcpSession implements HarnessSession {
  readonly id: string;
  private closed = false;

  constructor(private readonly process: AcpProcess, id: string) {
    this.id = id;
  }

  async *events(): AsyncIterable<HarnessEvent> {
    const reader = this.process.stdout.getReader();
    const decoder = new TextDecoder();
    let pending = "";
    try {
      while (!this.closed) {
        const chunk = await reader.read();
        if (chunk.done) break;
        pending += decoder.decode(chunk.value, { stream: true });
        const lines = pending.split(/\r?\n/);
        pending = lines.pop() ?? "";
        for (const line of lines) {
          const event = decodeAcpLine(line);
          if (event) yield event;
        }
      }
      if (pending) {
        const event = decodeAcpLine(pending);
        if (event) yield event;
      }
    } finally {
      reader.releaseLock();
    }
  }

  async close(): Promise<void> {
    this.closed = true;
    this.process.kill();
  }
}

export class AcpTransport implements HarnessTransport {
  constructor(private readonly definition: HarnessDefinition, private readonly spawn: AcpSpawn, private readonly argvSuffix: string[] = []) {}

  async start(request: HarnessSessionRequest): Promise<HarnessSession> {
    const process = this.spawn([this.definition.executable, ...this.argvSuffix], { cwd: request.cwd, env: request.env ?? {}, stdout: "pipe", stdin: "pipe" });
    return new AcpSession(process, request.resumeId ?? `${this.definition.id}:${Date.now()}`);
  }
}
