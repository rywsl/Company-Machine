/**
 * Kiri container runtime adapter.
 *
 * Kiri exposes the Docker Engine API through a named Docker context. The
 * sandbox contract stays identical to Podman, while every command is routed
 * through `docker --context <name>` and never through a host socket.
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
  runner?: CommandRunner;
  egressNetwork?: string;
  egressEnv?: Record<string, string>;
};

const CONTEXT = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/;

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

  constructor(options: KiriProviderOptions = {}) {
    const hostEnv = ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {});
    const dockerExecutable = resolvePodmanExecutable(options.dockerExecutable ?? hostEnv.MAO_DOCKER_EXECUTABLE ?? "docker");
    const context = options.context ?? hostEnv.MAO_KIRI_CONTEXT ?? "kiri";
    const mode = options.mode ?? (hostEnv.MAO_KIRI_MODE === "docker-context" ? "docker-context" : "kiri");
    if (!CONTEXT.test(context)) throw new Error("Invalid Kiri Docker context");
    const runner = options.runner ?? runCommand;
    const kiriExecutable = resolveKiriExecutable(options.kiriExecutable ?? hostEnv.MAO_KIRI_EXECUTABLE);
    const executable = mode === "kiri" ? kiriExecutable : dockerExecutable;
    super({
      executable,
      runner: async (argv, commandOptions) => mode === "kiri"
        ? runner(argv, commandOptions)
        : runner(commandFor(argv, dockerExecutable, context), commandOptions),
      egressNetwork: options.egressNetwork,
      egressEnv: options.egressEnv,
    });
    this.dockerExecutable = dockerExecutable;
    this.context = context;
    this.kiriExecutable = kiriExecutable;
    this.mode = mode;
    this.autoStart = options.autoStart ?? true;
    this.commandRunner = runner;
  }

  buildRunArgv(spec: SandboxSpec): string[] {
    const argv = super.buildRunArgv(spec);
    return this.mode === "kiri" ? argv : commandFor(argv, this.dockerExecutable, this.context);
  }

  async health(): Promise<RuntimeHealth> {
    if (this.mode === "kiri") {
      try {
        const result = await this.commandRunner([this.kiriExecutable, "ps", "--format", "json"], { timeoutMs: 10_000 });
        return {
          available: result.exitCode === 0,
          platform: ((globalThis as { process?: { platform?: string } }).process?.platform ?? "unknown"),
          executable: this.kiriExecutable,
          machine: null,
          rootless: null,
          reason: result.exitCode === 0 ? undefined : new TextDecoder().decode(result.stderr).trim() || "kiri-unavailable",
        };
      } catch (error) {
        return {
          available: false,
          platform: ((globalThis as { process?: { platform?: string } }).process?.platform ?? "unknown"),
          executable: this.kiriExecutable,
          machine: null,
          rootless: null,
          reason: error instanceof Error ? error.message : "kiri-unavailable",
        };
      }
    }
    const health = await super.health();
    return { ...health, executable: `${this.dockerExecutable} --context ${this.context}`, machine: this.context };
  }

  async ensureReady(): Promise<RuntimeHealth> {
    const current = await this.health();
    if (current.available || !this.autoStart || this.mode !== "kiri") return current;
    const processLike = (globalThis as { process?: { platform?: string } }).process;
    if (processLike?.platform !== "linux") return current;
    const bun = (globalThis as { Bun?: { spawn: (argv: string[], options: Record<string, unknown>) => any } }).Bun;
    if (!bun) return current;
    try {
      const daemon = bun.spawn([this.kiriExecutable, "daemon"], { stdin: "ignore", stdout: "ignore", stderr: "ignore", detached: true });
      daemon.unref?.();
    } catch {
      return current;
    }
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      const ready = await this.health();
      if (ready.available) return ready;
    }
    return this.health();
  }
}

