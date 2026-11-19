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
}

export interface HarnessEvent {
  type: "session.started" | "text.delta" | "tool.started" | "tool.finished" | "session.finished" | "session.failed";
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
