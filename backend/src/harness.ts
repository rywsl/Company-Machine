/** Official Harness declarations and transport boundaries.
 *
 * MAO schedules and observes these processes. The model loop, tool policy and
 * provider authentication remain inside the official Harness.
 */

export type OfficialHarnessName = "claude-code" | "codex" | "deepseek-harness" | "zcode" | "kimi-code" | "pi";
export type HarnessTransportKind = "acp" | "official-cli";

export interface HarnessDefinition {
  id: OfficialHarnessName;
  displayName: string;
  executable: string;
  transportPreference: HarnessTransportKind;
  transcriptFormat: "stream-json" | "codex-jsonl" | "acp-events";
  supportsResume: boolean;
}

export interface HarnessSessionRequest {
  task: string;
  contract: string;
  model: string;
  cwd: string;
  env?: Record<string, string>;
  resumeId?: string;
  containerImage?: string;
}

export interface HarnessEvent {
  type: "session.started" | "text.delta" | "record" | "secret.request" | "tool.started" | "tool.finished" | "session.finished" | "session.failed";
  sessionId?: string;
  text?: string;
  metadata?: Record<string, unknown>;
}

export interface HarnessSession {
  readonly id: string;
  events(): AsyncIterable<HarnessEvent>;
  close(): Promise<void>;
}

export interface HarnessTransport {
  start(request: HarnessSessionRequest): Promise<HarnessSession>;
}

export type OfficialProcess = {
  stdout: ReadableStream<Uint8Array>;
  stdin?: {
    write(data: Uint8Array): Promise<void> | void;
    end?(): Promise<void> | void;
  };
  kill(): void;
};

export type OfficialSpawn = (argv: string[], options: { cwd: string; env: Record<string, string>; stdout: "pipe"; stdin?: "pipe"; containerImage?: string }) => OfficialProcess;

const DEFINITIONS: Record<OfficialHarnessName, HarnessDefinition> = {
  "claude-code": { id: "claude-code", displayName: "Claude Code", executable: "claude", transportPreference: "official-cli", transcriptFormat: "stream-json", supportsResume: true },
  codex: { id: "codex", displayName: "Codex", executable: "codex", transportPreference: "official-cli", transcriptFormat: "codex-jsonl", supportsResume: true },
  "deepseek-harness": { id: "deepseek-harness", displayName: "DeepSeek Harness", executable: "deepseek-harness", transportPreference: "acp", transcriptFormat: "acp-events", supportsResume: true },
  zcode: { id: "zcode", displayName: "ZCode", executable: "zcode", transportPreference: "acp", transcriptFormat: "acp-events", supportsResume: true },
  "kimi-code": { id: "kimi-code", displayName: "Kimi Code", executable: "kimi", transportPreference: "acp", transcriptFormat: "acp-events", supportsResume: true },
  pi: { id: "pi", displayName: "Pi", executable: "pi", transportPreference: "acp", transcriptFormat: "acp-events", supportsResume: true },
};

export function officialHarnesses(): HarnessDefinition[] {
  return Object.values(DEFINITIONS);
}

export function officialHarness(id: string): HarnessDefinition | undefined {
  return DEFINITIONS[id as OfficialHarnessName];
}

export function chooseTransport(definition: HarnessDefinition, acpAvailable: boolean): HarnessTransportKind {
  return definition.transportPreference === "acp" && acpAvailable ? "acp" : "official-cli";
}

export function officialCliInvocation(definition: HarnessDefinition, request: HarnessSessionRequest): string[] {
  const prompt = `${request.contract}\n\n${request.task}`;
  if (definition.id === "codex") return [definition.executable, "exec", "--json", "--skip-git-repo-check", "--model", request.model, prompt];
  return [definition.executable, "-p", prompt, "--model", request.model, "--output-format", "stream-json"];
}

/** Decode one JSONL record emitted by an official Harness CLI. */
export function decodeOfficialLine(line: string): HarnessEvent | null {
  let value: unknown;
  try { value = JSON.parse(line); } catch { return null; }
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const item = record.item && typeof record.item === "object" ? record.item as Record<string, unknown> : undefined;
  const message = record.message && typeof record.message === "object" ? record.message as Record<string, unknown> : undefined;
  const type = typeof record.type === "string" ? record.type : typeof item?.type === "string" ? item.type : "";
  const sessionId = typeof record.session_id === "string" ? record.session_id : typeof record.sessionId === "string" ? record.sessionId : undefined;
  const content = message?.content;
  const contentText = Array.isArray(content)
    ? content.filter((part) => part && typeof part === "object" && typeof (part as Record<string, unknown>).text === "string").map((part) => (part as Record<string, unknown>).text as string).join("")
    : undefined;
  const text = typeof record.text === "string" ? record.text : typeof record.delta === "string" ? record.delta : typeof item?.text === "string" ? item.text : contentText;
  const eventType: HarnessEvent["type"] = type.includes("error") || type.includes("fail") ? "session.failed"
    : type.includes("start") || type.includes("init") ? "session.started"
      : type === "mao.record" || type === "record" ? "record"
      : type.includes("secret") && type.includes("request") ? "secret.request"
      : type.includes("tool") && !type.includes("complete") ? "tool.started"
        : type.includes("complete") || type.includes("finish") || type === "result" ? "session.finished"
          : "text.delta";
  return { type: eventType, sessionId, text, metadata: record };
}

class OfficialCliSession implements HarnessSession {
  readonly id: string;
  private closed = false;

  constructor(private readonly process: OfficialProcess, id: string) { this.id = id; }

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
        for (const line of lines) { const event = decodeOfficialLine(line); if (event) yield event; }
      }
      if (pending) { const event = decodeOfficialLine(pending); if (event) yield event; }
    } finally { reader.releaseLock(); }
  }

  async close(): Promise<void> { this.closed = true; this.process.kill(); }
}

export class OfficialCliTransport implements HarnessTransport {
  constructor(private readonly definition: HarnessDefinition, private readonly spawn: OfficialSpawn = defaultOfficialSpawn) {}

  async start(request: HarnessSessionRequest): Promise<HarnessSession> {
    const process = this.spawn(officialCliInvocation(this.definition, request), { cwd: request.cwd, env: request.env ?? {}, stdout: "pipe", stdin: "pipe", containerImage: request.containerImage });
    return new OfficialCliSession(process, request.resumeId ?? `${this.definition.id}:${Date.now()}`);
  }
}

function defaultOfficialSpawn(argv: string[], options: { cwd: string; env: Record<string, string>; stdout: "pipe"; stdin?: "pipe"; containerImage?: string }): OfficialProcess {
  const bun = (globalThis as { Bun?: { spawn: (argv: string[], options: Record<string, unknown>) => any } }).Bun;
  if (!bun) throw new Error("Bun runtime is required");
  const hostEnv = ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {});
  const env: Record<string, string> = {};
  for (const key of ["PATH", "HOME", "USERPROFILE", "SystemRoot", "TMP", "TEMP", "LANG", "LC_ALL"]) {
    const value = hostEnv[key];
    if (typeof value === "string") env[key] = value;
  }
  Object.assign(env, options.env);
  const image = options.containerImage;
  if (image && !/^[^\s@]+(?:\/[^\s@]+)*@sha256:[0-9a-f]{64}$/i.test(image)) throw new Error("Harness image must be pinned by digest");
  const command = image ? ["podman", "run", "--rm", "-i", "--network", "none", "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", image, ...argv] : argv;
  const process = bun.spawn(command, { cwd: options.cwd, env, stdout: options.stdout, stdin: options.stdin ?? "ignore", stderr: "pipe" });
  const stdin = process.stdin && typeof process.stdin.write === "function" ? {
    write: async (data: Uint8Array) => { await process.stdin.write(data); await process.stdin.flush?.(); },
    end: async () => { await process.stdin.end?.(); },
  } : undefined;
  return { stdout: process.stdout as ReadableStream<Uint8Array>, stdin, kill: () => process.kill() };
}

export function resolveOfficialHarness(runtime: string): HarnessDefinition | undefined {
  const normalized = runtime.toLowerCase();
  return officialHarnesses().find((item) => normalized === item.id || normalized.includes(item.id));
}
