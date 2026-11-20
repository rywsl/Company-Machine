/** Official Harness declarations and transport boundaries.
 *
 * MAO schedules and observes these processes. The model loop, tool policy and
 * provider authentication remain inside the official Harness.
 */

import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { realpathSync, statSync } from "node:fs";
import { resolveKiriClient } from "../runtime/kiri";
import { resolvePodmanExecutable, runCommand } from "../runtime/podman";
import { resolveDockerHubImage } from "../runtime/dockerhub-mirror";
import { prepareHarnessEgress } from "./harness-sandbox";
import { foremanMailboxTarget } from "./foreman-mailbox";
import { projectSourceRoot, type ProjectSourceMount } from "./project-source-mounts";
import { materialInputRoot, materialOutputRoot, type MaterialInputMount } from "./material-files";
import { randomUuid, SystemClock, type Clock } from "./legacy/foundation";

export const harnessControlPlaneRoot = resolve(import.meta.dir, "../..");

export type OfficialHarnessName = "claude-code" | "codex" | "deepseek-harness" | "zcode" | "kimi-code" | "pi" | "qwen-code";
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
  requireModelSelection?: boolean;
  cwd: string;
  env?: Record<string, string>;
  resumeId?: string;
  containerImage?: string;
  network?: "none" | "egress";
  egressBaseUrl?: string;
  foremanMailbox?: string;
  projectSources?: ProjectSourceMount[];
  materialFiles?: { outputDirectory: string; inputs: MaterialInputMount[] };
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
  exited?: Promise<number>;
  stdin?: {
    write(data: Uint8Array): Promise<void> | void;
    end?(): Promise<void> | void;
  };
  kill(): void | Promise<void>;
};

type SpawnOptions = Pick<HarnessSessionRequest, "cwd" | "containerImage" | "network" | "egressBaseUrl" | "foremanMailbox" | "projectSources" | "materialFiles">
  & { env: Record<string, string>; stdout: "pipe"; stdin?: "pipe" };
type HarnessRelay = Awaited<ReturnType<typeof prepareHarnessEgress>>;
export type OfficialSpawn = (argv: string[], options: SpawnOptions) => OfficialProcess | Promise<OfficialProcess>;

const DEFINITIONS: Record<OfficialHarnessName, HarnessDefinition> = {
  "claude-code": { id: "claude-code", displayName: "Claude Code", executable: "claude", transportPreference: "official-cli", transcriptFormat: "stream-json", supportsResume: true },
  codex: { id: "codex", displayName: "Codex", executable: "codex", transportPreference: "official-cli", transcriptFormat: "codex-jsonl", supportsResume: true },
  "deepseek-harness": { id: "deepseek-harness", displayName: "DeepSeek Harness", executable: "deepseek-harness", transportPreference: "acp", transcriptFormat: "acp-events", supportsResume: true },
  zcode: { id: "zcode", displayName: "ZCode", executable: "zcode", transportPreference: "acp", transcriptFormat: "acp-events", supportsResume: true },
  "kimi-code": { id: "kimi-code", displayName: "Kimi Code", executable: "kimi", transportPreference: "acp", transcriptFormat: "acp-events", supportsResume: true },
  pi: { id: "pi", displayName: "Pi", executable: "pi", transportPreference: "official-cli", transcriptFormat: "stream-json", supportsResume: true },
  "qwen-code": { id: "qwen-code", displayName: "Qwen Code", executable: "qwen", transportPreference: "official-cli", transcriptFormat: "stream-json", supportsResume: false },
};

export function officialHarnesses(): HarnessDefinition[] {
  return Object.values(DEFINITIONS);
}

export function officialHarness(id: string): HarnessDefinition | undefined {
  return Object.hasOwn(DEFINITIONS, id) ? DEFINITIONS[id as OfficialHarnessName] : undefined;
}

export function chooseTransport(definition: HarnessDefinition, acpAvailable: boolean): HarnessTransportKind {
  return definition.transportPreference === "acp" && acpAvailable ? "acp" : "official-cli";
}

/**
 * Selects a transport without inventing a CLI contract for an ACP-only
 * Harness. The compatibility helper above remains available to callers that
 * only need to inspect the declared preference.
 */
export function requireDeclaredTransport(definition: HarnessDefinition, acpAvailable: boolean): HarnessTransportKind {
  if (definition.transportPreference === "acp" && !acpAvailable) {
    throw new Error(`${definition.displayName} requires the official ACP transport`);
  }
  return definition.transportPreference;
}

export function harnessEndpointEnv(definition: HarnessDefinition, baseUrl: string): Record<string, string> {
  if (!baseUrl || (definition.id !== "claude-code" && definition.id !== "codex")) return {};
  const url = new URL(baseUrl);
  if (!/^https?:$/.test(url.protocol) || url.username || url.password) throw new Error("Harness Base URL must be an HTTP(S) address without credentials");
  return definition.id === "claude-code" ? { ANTHROPIC_BASE_URL: baseUrl } : { OPENAI_BASE_URL: baseUrl };
}

export function officialCliInvocation(definition: HarnessDefinition, request: HarnessSessionRequest): string[] {
  const prompt = `${request.contract}\n\n${request.task}`;
  if (definition.id === "codex") return [definition.executable, "exec", ...(request.resumeId ? ["resume"] : []), "--json", "--skip-git-repo-check", "--model", request.model, ...(request.resumeId ? [request.resumeId] : []), prompt];
  if (definition.id === "claude-code") return [definition.executable, "--print", "--model", request.model, "--output-format", "stream-json", "--verbose", "--include-partial-messages", ...(request.resumeId ? ["--resume", request.resumeId] : []), "--", prompt];
  if (definition.id === "kimi-code") return [definition.executable, "--model", request.model, "--output-format", "stream-json", ...(request.resumeId ? ["--session", request.resumeId] : []), "--prompt", prompt];
  if (definition.id === "pi") return [definition.executable, "--print", "--mode", "json", "--model", request.model, ...(request.resumeId ? ["--session", request.resumeId] : []), "--", prompt];
  if (definition.id === "qwen-code") return [definition.executable, "-p", prompt, "--model", request.model, "--output-format", "stream-json"];
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
  const assistantMessageEvent = record.assistantMessageEvent && typeof record.assistantMessageEvent === "object"
    ? record.assistantMessageEvent as Record<string, unknown> : undefined;
  const type = typeof record.type === "string" ? record.type : typeof item?.type === "string" ? item.type : "";
  const session = record.session && typeof record.session === "object" ? record.session as Record<string, unknown> : undefined;
  const identifier = record.session_id ?? record.sessionId ?? record.thread_id ?? session?.id;
  const sessionId = typeof identifier === "string" ? identifier : undefined;
  if (type === "stream_event") {
    const event = record.event as { type?: string; delta?: { type?: string; text?: string } } | undefined;
    return event?.type === "content_block_delta" && event.delta?.type === "text_delta" && typeof event.delta.text === "string"
      ? { type: "text.delta", sessionId, text: event.delta.text, metadata: { partial: true } }
      : null;
  }
  if (type === "user") return null;
  const content = message?.content;
  const contentText = Array.isArray(content)
    ? content.filter((part) => part && typeof part === "object" && typeof (part as Record<string, unknown>).text === "string").map((part) => (part as Record<string, unknown>).text as string).join("")
    : undefined;
  const text = typeof record.text === "string" ? record.text : typeof record.delta === "string" ? record.delta
    : typeof assistantMessageEvent?.text === "string" ? assistantMessageEvent.text
      : typeof assistantMessageEvent?.delta === "string" ? assistantMessageEvent.delta
        : typeof item?.text === "string" ? item.text : typeof record.result === "string" ? record.result : contentText;
  const assistantEventType = typeof assistantMessageEvent?.type === "string" ? assistantMessageEvent.type : "";
  const eventType: HarnessEvent["type"] = type.includes("error") || type.includes("fail") || record.is_error === true ? "session.failed"
    : type === "item.completed" && item?.type === "agent_message" ? "text.delta"
    : type === "message_update" && assistantEventType === "text_delta" ? "text.delta"
      : type.includes("start") || type.includes("init") || type === "session" ? "session.started"
      : type === "mao.record" || type === "record" ? "record"
      : type.includes("secret") && type.includes("request") ? "secret.request"
      : type.includes("tool") && !type.includes("complete") ? "tool.started"
        : type.includes("complete") || type.includes("finish") || type.endsWith("_end") || type === "agent_settled" || type === "result" ? "session.finished"
          : "text.delta";
  return { type: eventType, sessionId, text, metadata: record };
}

class OfficialCliSession implements HarnessSession {
  readonly id: string;
  private closed = false;
  private reader?: ReadableStreamDefaultReader<Uint8Array>;
  private closing?: Promise<void>;

  constructor(private readonly process: OfficialProcess, id: string) { this.id = id; }

  async *events(): AsyncIterable<HarnessEvent> {
    if (this.closed) return;
    const reader = this.process.stdout.getReader();
    this.reader = reader;
    const decoder = new TextDecoder();
    let pending = "";
    let partial = false;
    let failed = false;
    const decode = function* (line: string): Iterable<HarnessEvent> {
      const event = decodeOfficialLine(line);
      if (!event) return;
      if (event.type === "session.failed") failed = true;
      if (event.metadata?.partial === true) {
        partial = true;
        yield event;
        return;
      }
      if (event.type === "text.delta" && event.text) {
        const records = event.text.split(/\r?\n/).map((text) => text.trim()).filter(Boolean).map((text) => {
          if (!text.startsWith("{")) return { text };
          const record = decodeOfficialLine(text);
          return record?.type === "record" || record?.type === "secret.request" ? { record } : { text };
        });
        for (const item of records) if (item.record) yield { ...item.record, sessionId: event.sessionId };
        if (records.some((item) => item.record)) event.text = records.flatMap((item) => item.text ?? []).join("\n");
        if (event.metadata?.type === "assistant" && partial) {
          partial = false;
          return;
        }
      }
      if (event.type !== "text.delta" || event.text || event.sessionId) yield event;
    };
    try {
      while (!this.closed) {
        const chunk = await reader.read();
        if (this.closed) return;
        if (chunk.done) break;
        pending += decoder.decode(chunk.value, { stream: true });
        const lines = pending.split(/\r?\n/);
        pending = lines.pop() ?? "";
        for (const line of lines) {
          if (line.length > 1024 * 1024) throw new Error("Official Harness emitted an oversized JSONL record");
          yield* decode(line);
        }
        if (pending.length > 1024 * 1024) throw new Error("Official Harness emitted an oversized JSONL record");
      }
      pending += decoder.decode();
      if (pending) yield* decode(pending);
      const exitCode = this.process.exited ? await this.process.exited : 0;
      if (!this.closed && !failed && exitCode !== 0) yield { type: "session.failed", metadata: { exitCode } };
    } finally {
      this.reader = undefined;
      reader.releaseLock();
    }
  }

  close(): Promise<void> {
    return this.closing ??= this.stop();
  }

  private async stop(): Promise<void> {
    this.closed = true;
    void this.reader?.cancel().catch(() => {});
    await this.process.kill();
  }
}

export class OfficialCliTransport implements HarnessTransport {
  constructor(private readonly definition: HarnessDefinition, private readonly spawn: OfficialSpawn = defaultOfficialSpawn, private readonly clock: Clock = new SystemClock()) {}

  async start(request: HarnessSessionRequest): Promise<HarnessSession> {
    const process = await this.spawn(officialCliInvocation(this.definition, request), {
      cwd: request.cwd, env: request.env ?? {}, stdout: "pipe", stdin: "pipe", containerImage: request.containerImage,
      network: request.network, egressBaseUrl: request.egressBaseUrl, foremanMailbox: request.foremanMailbox, projectSources: request.projectSources,
      materialFiles: request.materialFiles,
    });
    return new OfficialCliSession(process, request.resumeId ?? `${this.definition.id}:${this.clock.now().getTime()}`);
  }
}

export function harnessProcessCommand(
  argv: string[], options: Pick<SpawnOptions, "env" | "containerImage" | "network" | "foremanMailbox" | "projectSources" | "materialFiles">,
): { command: string[]; env: Record<string, string> } {
  const { command, env } = processCommand(argv, options);
  return { command, env };
}

function validateImage(image?: string): void {
  if (image && !/^[^\s@]+(?:\/[^\s@]+)*@sha256:[0-9a-f]{64}$/i.test(image)) throw new Error("Harness image must be pinned by digest");
}

export function supportsLocalProjectMounts(): boolean {
  if (process.platform !== "linux") return false;
  const runtime = process.env.MAO_CONTAINER_RUNTIME?.toLowerCase() ?? "podman";
  return runtime === "kiri"
    ? resolveKiriClient().mode === "linux-socket"
    : runtime === "podman" && !process.env.CONTAINER_HOST && !process.env.CONTAINER_CONNECTION;
}

function materialMountPath(path: string) {
  if (!isAbsolute(path) || /[,\u0000-\u001f\u007f]/.test(path) || resolve(path, "..") === path) return;
  const info = statSync(path, { throwIfNoEntry: false });
  if (info && realpathSync(path) === path) return info;
}

function processCommand(
  argv: string[], options: Pick<SpawnOptions, "env" | "containerImage" | "network" | "foremanMailbox" | "projectSources" | "materialFiles">,
  relay?: HarnessRelay, containerName?: string,
) {
  const hostEnv = ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {});
  const env: Record<string, string> = {};
  for (const key of ["PATH", "HOME", "USERPROFILE", "SystemRoot", "TMP", "TEMP", "LANG", "LC_ALL"]) {
    const value = hostEnv[key];
    if (typeof value === "string") env[key] = value;
  }
  const configured = relay?.env(options.env) ?? options.env;
  const image = options.containerImage;
  validateImage(image);
  const network = options.network ?? "none";
  if (network !== "none" && network !== "egress") throw new Error("Harness network must be none or egress");
  if (image && network === "egress" && !relay) throw new Error("Harness egress requires a verified isolated relay");
  const scratch = { HOME: "/tmp", XDG_CONFIG_HOME: "/tmp/.config", XDG_CACHE_HOME: "/tmp/.cache" };
  const envNames = Object.keys(configured).filter((name) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(name) && !Object.hasOwn(scratch, name));
  if (image) for (const name of envNames) env[name] = configured[name];
  else Object.assign(env, configured);
  const kiri = image && hostEnv.MAO_CONTAINER_RUNTIME?.toLowerCase() === "kiri"
    ? resolveKiriClient()
    : undefined;
  const mounts: string[] = [];
  if (options.materialFiles) {
    if (!image || !supportsLocalProjectMounts()) throw new Error("Material file mounts require a local Linux container runtime");
    if (options.foremanMailbox !== undefined) throw new Error("Material file mounts cannot be combined with a Foreman mailbox");
    const { outputDirectory, inputs } = options.materialFiles;
    if (!materialMountPath(outputDirectory)?.isDirectory()) throw new Error("Invalid material output directory");
    mounts.push("--mount", "type=bind,source=" + outputDirectory + ",target=" + materialOutputRoot);
    const indexes = new Set<number>();
    for (const { index, source } of inputs) {
      if (!Number.isSafeInteger(index) || index < 1 || indexes.has(index) || !materialMountPath(source)?.isFile()) {
        throw new Error("Invalid material input mount");
      }
      indexes.add(index);
      mounts.push("--mount", "type=bind,source=" + source + ",target=" + materialInputRoot + "/" + index + ",readonly");
    }
  }
  if (options.foremanMailbox !== undefined) {
    const root = options.foremanMailbox;
    if (!image || process.platform !== "linux" || !isAbsolute(root) || /[,\u0000]/.test(root)
        || (kiri ? kiri.mode !== "linux-socket" : hostEnv.CONTAINER_HOST || hostEnv.CONTAINER_CONNECTION)) {
      throw new Error("Foreman mailbox requires a local Linux container runtime");
    }
    mounts.push(
      "--mount", "type=bind,source=" + join(root, "requests") + ",target=" + foremanMailboxTarget + "/requests",
      "--mount", "type=bind,source=" + join(root, "responses") + ",target=" + foremanMailboxTarget + "/responses,readonly",
    );
  }
  const indexes = new Set<number>();
  if (options.projectSources?.length && (!image || !supportsLocalProjectMounts())) {
    throw new Error("Project source mounts require a local Linux container runtime");
  }
  for (const { index, source } of options.projectSources ?? []) {
    if (!Number.isSafeInteger(index) || index < 1 || indexes.has(index)
        || !isAbsolute(source) || /[,\u0000-\u001f\u007f]/.test(source)) throw new Error("Invalid project source mount");
    try {
      if (realpathSync(source) !== source) throw new Error("Not a canonical source");
    } catch { throw new Error("Project source mount is no longer available"); }
    indexes.add(index);
    mounts.push("--mount", "type=bind,source=" + source + ",target=" + projectSourceRoot + "/" + index + ",readonly");
  }
  const envFlags = envNames.flatMap((name) => ["--env", name]);
  const scratchFlags = Object.entries(scratch).flatMap(([name, value]) => ["--env", `${name}=${value}`]);
  const engine = kiri?.argv ?? [resolvePodmanExecutable(hostEnv.MAO_PODMAN_EXECUTABLE)];
  const command = image
    ? [
      ...engine, "run", "--rm", "-i", "--init", "--pids-limit", "256", "--tmpfs", "/tmp:rw,nosuid,size=256m",
      "--network", relay?.network ?? "none", "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
      "--read-only", ...(containerName ? ["--name", containerName] : []), ...(relay ? ["--pull=never"] : []),
      ...mounts, ...envFlags, ...scratchFlags, relay?.image ?? image, ...argv,
    ]
    : argv;
  if (kiri) Object.assign(env, kiri.env);
  return { command, env, engine, engineEnv: kiri?.env };
}

function defaultOfficialSpawn(argv: string[], options: SpawnOptions): Promise<OfficialProcess> {
  return spawnOfficialProcess(argv, { ...options, stdin: undefined });
}

export function windowsClaudeExecutable(launcher: string | null): string | undefined {
  if (!launcher) return undefined;
  if (/\.exe$/i.test(launcher)) return launcher;
  if (basename(launcher).toLowerCase() !== "claude.cmd") return undefined;
  const executable = join(dirname(launcher), "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe");
  return statSync(executable, { throwIfNoEntry: false })?.isFile() ? executable : undefined;
}

export async function spawnOfficialProcess(
  argv: string[], options: SpawnOptions, prepareEgress = prepareHarnessEgress,
): Promise<OfficialProcess> {
  const bun = (globalThis as { Bun?: { spawn: (argv: string[], options: Record<string, unknown>) => any } }).Bun;
  if (!bun) throw new Error("Bun runtime is required");
  const image = options.containerImage;
  const needsRelay = image !== undefined && options.network === "egress";
  validateImage(image);
  const initial = image ? processCommand(argv, options) : undefined;
  const resolvedImage = image && initial ? await ensureContainerImage(image, initial.engine, initial.engineEnv) : image;
  const effectiveOptions = resolvedImage === image ? options : { ...options, containerImage: resolvedImage };
  const relay = needsRelay && resolvedImage ? await prepareEgress(resolvedImage, options.egressBaseUrl) : undefined;
  const containerName = relay?.name ?? (resolvedImage ? `mao-harness-${randomUuid()}` : undefined);
  try {
    const { command, env, engine, engineEnv } = processCommand(argv, effectiveOptions, relay, containerName);
    // The npm batch wrapper can interfere with prompts containing newlines.
    const claude = !image && process.platform === "win32" && command[0] === "claude"
      ? windowsClaudeExecutable(Bun.which("claude"))
      : undefined;
    const launch = claude ? [claude, ...command.slice(1)] : command;
    const child = bun.spawn(launch, { cwd: options.cwd, env, stdout: options.stdout, stdin: options.stdin ?? "ignore", stderr: "ignore" });
    let cleanup: Promise<void> | undefined;
    const removeContainer = () => cleanup ??= (async () => {
      if (relay) await relay.close();
      else if (containerName) await runCommand([...engine, "rm", "-f", containerName], { timeoutMs: 15_000, env: engineEnv });
    })().catch(() => { /* The engine can disappear during app shutdown. */ });
    if (relay) void (child.exited as Promise<number>).finally(removeContainer).catch(() => {});
    const stdin = child.stdin && typeof child.stdin.write === "function" ? {
      write: async (data: Uint8Array) => { await child.stdin.write(data); await child.stdin.flush?.(); },
      end: async () => { await child.stdin.end?.(); },
    } : undefined;
    let stopping: Promise<void> | undefined;
    async function stop(): Promise<void> {
      if (resolvedImage && !relay && child.exitCode === 0) return; // The engine has already processed --rm.
      try {
        if (!resolvedImage && process.platform === "win32" && child.exitCode === null && child.signalCode === null
            && Number.isSafeInteger(child.pid) && child.pid > 0) {
          // npm's Windows command wrapper and its native descendants can share the stdout pipe.
          const taskkill = resolve(process.env.SystemRoot ?? "C:\\Windows", "System32", "taskkill.exe");
          const result = await runCommand([taskkill, "/pid", String(child.pid), "/t", "/f"], { timeoutMs: 3_000 }).catch(() => undefined);
          if (result?.exitCode === 0) return;
        }
        child.kill();
      } finally { await removeContainer(); }
    }
    return {
      stdin, stdout: child.stdout as ReadableStream<Uint8Array>, exited: child.exited,
      kill: () => stopping ??= stop(),
    };
  } catch (error) {
    await relay?.close().catch(() => {});
    throw error;
  }
}

async function ensureContainerImage(image: string, engine: string[], engineEnv?: Record<string, string>): Promise<string> {
  const inspect = async (reference: string) => runCommand(
    [...engine, "image", "inspect", "--format", "{{.Id}}", reference],
    { timeoutMs: 60_000, env: engineEnv },
  );
  const expected = image.slice(image.indexOf("@") + 1).toLowerCase();
  const present = await inspect(image);
  if (present.exitCode === 0 && present.stdout.length > 0) return image;

  const candidate = await resolveDockerHubImage(image);
  const pulled = await runCommand([...engine, "pull", candidate], { timeoutMs: 10 * 60_000, env: engineEnv });
  if (pulled.exitCode !== 0) {
    throw new Error(`Harness image is unavailable: ${image}`);
  }
  const resolved = await inspect(candidate);
  const id = new TextDecoder().decode(resolved.stdout).trim().toLowerCase();
  if (resolved.exitCode !== 0 || id !== expected) {
    throw new Error(`Harness image digest verification failed: ${image}`);
  }
  return candidate;
}

export function resolveOfficialHarness(runtime: string): HarnessDefinition | undefined {
  const normalized = runtime.toLowerCase();
  return officialHarnesses().find((item) => normalized === item.id || normalized.includes(item.id));
}
