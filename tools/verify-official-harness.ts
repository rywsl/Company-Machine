import { officialHarnesses, type HarnessDefinition } from "../backend/src/harness";

type ProbeResult = {
  runtime: string;
  executable: string;
  version?: string;
  acp?: "verified" | "unavailable" | "failed";
  error?: string;
};

const encoder = new TextEncoder();
const decoder = new TextDecoder();

function requestedRuntime(args: string[]): string | undefined {
  const index = args.indexOf("--runtime");
  if (index === -1) return undefined;
  const value = args[index + 1];
  if (!value || value.startsWith("--")) throw new Error("--runtime requires an official Harness id");
  return value.toLowerCase();
}

async function readLine(stream: ReadableStream<Uint8Array>, timeoutMs: number): Promise<string> {
  const reader = stream.getReader();
  const timer = setTimeout(() => void reader.cancel(), timeoutMs);
  try {
    let buffer = "";
    while (true) {
      const next = await reader.read();
      if (next.done) throw new Error("official ACP closed before initialize response");
      buffer += decoder.decode(next.value, { stream: true });
      const newline = buffer.indexOf("\n");
      if (newline !== -1) return buffer.slice(0, newline).replace(/\r$/, "");
    }
  } finally {
    clearTimeout(timer);
    reader.releaseLock();
  }
}

async function probeVersion(executable: string): Promise<string> {
  const process = Bun.spawn([executable, "--version"], { stdout: "pipe", stderr: "pipe" });
  const output = await new Response(process.stdout).text();
  const exitCode = await process.exited;
  if (exitCode !== 0) throw new Error("official version command failed");
  const version = output.trim().split(/\r?\n/, 1)[0];
  if (!version) throw new Error("official version command returned no text");
  return version.slice(0, 200);
}

function acpArgs(definition: HarnessDefinition): string[] {
  return definition.id === "kimi-code" ? ["acp"] : [];
}

async function probeAcp(definition: HarnessDefinition, executable: string): Promise<"verified"> {
  const process = Bun.spawn([executable, ...acpArgs(definition)], { stdin: "pipe", stdout: "pipe", stderr: "pipe" });
  try {
    const request = {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: 1,
        clientInfo: { name: "machine-agent-org-live-probe", version: "1" },
        clientCapabilities: {},
      },
    };
    await process.stdin.write(encoder.encode(JSON.stringify(request) + "\n"));
    const line = await readLine(process.stdout, 10_000);
    const response = JSON.parse(line) as { id?: unknown; result?: { protocolVersion?: unknown } };
    if (response.id !== 1 || response.result?.protocolVersion !== 1) throw new Error("official ACP protocol negotiation failed");
    return "verified";
  } finally {
    process.kill();
    await process.exited.catch(() => -1);
  }
}

async function probe(definition: HarnessDefinition): Promise<ProbeResult | undefined> {
  const executable = Bun.which(definition.executable);
  if (!executable) return undefined;
  const result: ProbeResult = { runtime: definition.id, executable };
  try {
    result.version = await probeVersion(definition.executable);
    if (definition.transportPreference === "acp") result.acp = await probeAcp(definition, definition.executable);
  } catch (error) {
    result.error = error instanceof Error ? error.message : "official Harness probe failed";
    if (definition.transportPreference === "acp") result.acp = "failed";
  }
  return result;
}

const runtime = requestedRuntime(Bun.argv.slice(2));
const definitions = officialHarnesses().filter((definition) => !runtime || definition.id === runtime);
if (!definitions.length) throw new Error(`unknown official Harness: ${runtime}`);
const results = (await Promise.all(definitions.map(probe))).filter((result): result is ProbeResult => Boolean(result));
for (const result of results) console.log(JSON.stringify(result));
if (!results.length) throw new Error("no requested official Harness executable was found");
if (results.some((result) => result.error)) process.exitCode = 1;
