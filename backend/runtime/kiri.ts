/**
 * Kiri container runtime adapter.
 *
 * Kiri's official CLI speaks to its Docker Engine API without opening the UI.
 * On Linux it runs directly on the host kernel, while the optional
 * docker-context mode targets a named context. The sandbox contract stays
 * identical to Podman and never mounts a host socket.
 */

import {
  PodmanProvider,
  resolvePodmanExecutable,
  runCommand,
  type CommandRunner,
  type RuntimeHealth,
  type SandboxSpec,
} from "./podman";
import { existsSync } from "node:fs";
import { join } from "node:path";

export type KiriProviderOptions = {
  dockerExecutable?: string;
  kiriExecutable?: string;
  context?: string;
  mode?: "kiri" | "docker-context";
  autoStart?: boolean;
  /** Unix socket or named pipe used by the headless Kiri daemon. */
  socket?: string;
  /** Optional Kiri state directory, useful for a dedicated Linux service user. */
  dataDir?: string;
  runner?: CommandRunner;
  egressNetwork?: string;
  egressEnv?: Record<string, string>;
};

const CONTEXT = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/;

export function resolveKiriEnvironment(options: { socket?: string; dataDir?: string } = {}): Record<string, string> {
  const env = ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {});
  const values: Record<string, string> = {};
  const socket = options.socket ?? env.MAO_KIRI_SOCKET ?? env.KIRI_SOCKET;
  const dataDir = options.dataDir ?? env.MAO_KIRI_DATA_DIR ?? env.KIRI_DATA_DIR;
  if (socket !== undefined) {
    if (socket.length === 0 || socket.includes("\u0000")) throw new Error("Invalid Kiri socket");
    values.KIRI_SOCKET = socket;
  }
  if (dataDir !== undefined) {
    if (dataDir.length === 0 || dataDir.includes("\u0000")) throw new Error("Invalid Kiri data directory");
    values.KIRI_DATA_DIR = dataDir;
  }
  return values;
}

export function resolveKiriExecutable(configured?: string): string {
  if (configured) return configured;
  const env = ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {});
  if (env.MAO_KIRI_EXECUTABLE) return env.MAO_KIRI_EXECUTABLE;
  const localAppData = env.LOCALAPPDATA;
  const programFiles = env.ProgramFiles;
  const candidates = [
    localAppData ? join(localAppData, "Kiri", "bin", "kiri.exe") : "",
    programFiles ? join(programFiles, "Kiri", "bin", "kiri.exe") : "",
  ].filter(Boolean);
  return candidates.find((candidate) => existsSync(candidate)) ?? "kiri";
}

export function buildKiriHarnessArgv(
  image: string,
  argv: string[],
  network: "none" | "egress" = "none",
  options: { dockerExecutable?: string; kiriExecutable?: string; context?: string; mode?: "kiri" | "docker-context"; egressNetwork?: string } = {},
): string[] {
  const mode = options.mode ?? "kiri";
  const context = options.context ?? "kiri";
  if (!CONTEXT.test(context)) throw new Error("Invalid Kiri Docker context");
  const egressNetwork = options.egressNetwork ?? "";
  if (network === "egress" && !CONTEXT.test(egressNetwork)) throw new Error("Managed Kiri egress network is unavailable");
  const podmanNetwork = network === "egress" ? egressNetwork : "none";
  if (mode === "kiri") return [resolveKiriExecutable(options.kiriExecutable), "run", "--rm", "-i", "--network", podmanNetwork, "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", image, ...argv];
  return [options.dockerExecutable ?? "docker", "--context", context, "run", "--rm", "-i", "--network", podmanNetwork, "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", image, ...argv];
}

function commandFor(argv: string[], executable: string, context: string): string[] {
  if (argv[0] === executable && argv[1] === "--context" && argv[2] === context) return argv;
  return [executable, "--context", context, ...argv.slice(1)];
}

export class KiriProvider extends PodmanProvider {
  readonly name = "kiri";
  readonly context: string;
  readonly kiriExecutable: string;
  private readonly dockerExecutable: string;
  private readonly commandRunner: CommandRunner;
  readonly mode: "kiri" | "docker-context";
  readonly autoStart: boolean;
  readonly kiriEnvironment: Record<string, string>;

  constructor(options: KiriProviderOptions = {}) {
    const hostEnv = ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {});
    const dockerExecutable = resolvePodmanExecutable(options.dockerExecutable ?? hostEnv.MAO_DOCKER_EXECUTABLE ?? "docker");
    const context = options.context ?? hostEnv.MAO_KIRI_CONTEXT ?? "kiri";
    const mode = options.mode ?? (hostEnv.MAO_KIRI_MODE === "docker-context" ? "docker-context" : "kiri");
    if (!CONTEXT.test(context)) throw new Error("Invalid Kiri Docker context");
    const runner = options.runner ?? runCommand;
    const kiriExecutable = resolveKiriExecutable(options.kiriExecutable ?? hostEnv.MAO_KIRI_EXECUTABLE);
    const kiriEnvironment = resolveKiriEnvironment(options);
    const executable = mode === "kiri" ? kiriExecutable : dockerExecutable;
    super({
      executable,
      runner: async (argv, commandOptions) => {
        const mergedOptions = Object.keys(kiriEnvironment).length === 0
          ? commandOptions
          : { ...(commandOptions ?? {}), env: { ...kiriEnvironment, ...(commandOptions?.env ?? {}) } };
        return mode === "kiri"
          ? runner(argv, mergedOptions)
          : runner(commandFor(argv, dockerExecutable, context), mergedOptions);
      },
      egressNetwork: options.egressNetwork,
      egressEnv: options.egressEnv,
    });
    this.dockerExecutable = dockerExecutable;
    this.context = context;
    this.kiriExecutable = kiriExecutable;
    this.mode = mode;
    this.autoStart = options.autoStart ?? true;
    this.commandRunner = runner;
    this.kiriEnvironment = kiriEnvironment;
  }

  buildRunArgv(spec: SandboxSpec): string[] {
    const argv = super.buildRunArgv(spec);
    return this.mode === "kiri" ? argv : commandFor(argv, this.dockerExecutable, this.context);
  }

  async health(): Promise<RuntimeHealth> {
    if (this.mode === "kiri") {
      try {
        const commandOptions = Object.keys(this.kiriEnvironment).length > 0
          ? { timeoutMs: 10_000, env: this.kiriEnvironment }
          : { timeoutMs: 10_000 };
        const result = await this.commandRunner([this.kiriExecutable, "ps", "--format", "json"], commandOptions);
        const platform = ((globalThis as { process?: { platform?: string } }).process?.platform ?? "unknown");
        return {
          available: result.exitCode === 0,
          platform,
          executable: this.kiriExecutable,
          machine: platform === "linux" ? "linux-host" : null,
          rootless: null,
          reason: result.exitCode === 0 ? undefined : new TextDecoder().decode(result.stderr).trim() || "kiri-unavailable",
        };
      } catch (error) {
        return {
          available: false,
          platform: ((globalThis as { process?: { platform?: string } }).process?.platform ?? "unknown"),
          executable: this.kiriExecutable,
          machine: ((globalThis as { process?: { platform?: string } }).process?.platform ?? "unknown") === "linux" ? "linux-host" : null,
          rootless: null,
          reason: error instanceof Error ? error.message : "kiri-unavailable",
        };
      }
    }
    try {
      const commandOptions = Object.keys(this.kiriEnvironment).length > 0
        ? { timeoutMs: 10_000, env: this.kiriEnvironment }
        : { timeoutMs: 10_000 };
      const result = await this.commandRunner(commandFor([this.dockerExecutable, "info", "--format", "json"], this.dockerExecutable, this.context), commandOptions);
      const platform = ((globalThis as { process?: { platform?: string } }).process?.platform ?? "unknown");
      return {
        available: result.exitCode === 0,
        platform,
        executable: `${this.dockerExecutable} --context ${this.context}`,
        machine: this.context,
        rootless: null,
        reason: result.exitCode === 0 ? undefined : new TextDecoder().decode(result.stderr).trim() || "kiri-unavailable",
      };
    } catch (error) {
      return {
        available: false,
        platform: ((globalThis as { process?: { platform?: string } }).process?.platform ?? "unknown"),
        executable: `${this.dockerExecutable} --context ${this.context}`,
        machine: this.context,
        rootless: null,
        reason: error instanceof Error ? error.message : "kiri-unavailable",
      };
    }
  }

  async ensureReady(): Promise<RuntimeHealth> {
    const current = await this.health();
    if (current.available || !this.autoStart || this.mode !== "kiri") return current;
    const processLike = (globalThis as { process?: { platform?: string } }).process;
    if (processLike?.platform !== "linux") return current;
    const bun = (globalThis as { Bun?: { spawn: (argv: string[], options: Record<string, unknown>) => any } }).Bun;
    if (!bun) return current;
    try {
      const hostEnv = ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {});
      const daemonEnv = Object.fromEntries(Object.entries(hostEnv).filter(([, value]) => typeof value === "string")) as Record<string, string>;
      Object.assign(daemonEnv, this.kiriEnvironment);
      const daemon = bun.spawn([this.kiriExecutable, "daemon"], { stdin: "ignore", stdout: "ignore", stderr: "ignore", detached: true, env: daemonEnv });
      daemon.unref?.();
    } catch {
      return current;
    }
    for (let attempt = 0; attempt < 50; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      const ready = await this.health();
      if (ready.available) return ready;
    }
    return this.health();
  }
}

