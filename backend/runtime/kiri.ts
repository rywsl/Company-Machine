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
  /** Runtime platform override used by tests and cross-platform bootstrap. */
  platform?: string;
  autoStart?: boolean;
  /** Unix socket or named pipe used by the headless Kiri daemon. */
  socket?: string;
  /** Compatibility alias for daemonHome when the adapter starts a daemon. */
  dataDir?: string;
  /** Linux socket group used by the official systemd service. */
  socketGroup?: string;
  /** HOME used only when this adapter starts the official Linux daemon. */
  daemonHome?: string;
  /** systemd unit used by the official Linux package. */
  systemdService?: string;
  systemctlExecutable?: string;
  runner?: CommandRunner;
  egressNetwork?: string;
  egressEnv?: Record<string, string>;
};

const CONTEXT = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/;
const SERVICE = /^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$/;

function processPlatform(): string {
  return ((globalThis as { process?: { platform?: string } }).process?.platform ?? "unknown");
}

function resolveMode(configured: "kiri" | "docker-context" | undefined, env: Record<string, string | undefined>, platform: string): "kiri" | "docker-context" {
  if (configured) return configured;
  if (env.MAO_KIRI_MODE === "kiri" || env.MAO_KIRI_MODE === "docker-context") return env.MAO_KIRI_MODE;
  // The official Linux package owns a systemd daemon and creates the `kiri`
  // Docker context. Prefer that boundary over the CLI's nested docker exec.
  return platform === "linux" ? "docker-context" : "kiri";
}

export function resolveKiriEnvironment(options: { socket?: string; dataDir?: string; socketGroup?: string; platform?: string } = {}): Record<string, string> {
  const env = ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {});
  const platform = options.platform ?? processPlatform();
  const values: Record<string, string> = {};
  const socket = options.socket ?? env.MAO_KIRI_SOCKET ?? env.KIRI_SOCKET ?? (platform === "linux" ? "/run/kiri/docker.sock" : undefined);
  const dataDir = options.dataDir ?? env.MAO_KIRI_DATA_DIR ?? env.KIRI_DATA_DIR;
  const socketGroup = options.socketGroup ?? env.MAO_KIRI_SOCKET_GROUP ?? env.KIRI_SOCKET_GROUP ?? (platform === "linux" ? "kiri" : undefined);
  if (socket !== undefined) {
    if (socket.length === 0 || socket.includes("\u0000")) throw new Error("Invalid Kiri socket");
    values.KIRI_SOCKET = socket;
  }
  if (dataDir !== undefined) {
    if (dataDir.length === 0 || dataDir.includes("\u0000")) throw new Error("Invalid Kiri data directory");
    values.KIRI_DATA_DIR = dataDir;
  }
  if (socketGroup !== undefined) {
    if (socketGroup.length === 0 || socketGroup.includes("\u0000")) throw new Error("Invalid Kiri socket group");
    values.KIRI_SOCKET_GROUP = socketGroup;
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
  options: { dockerExecutable?: string; kiriExecutable?: string; context?: string; mode?: "kiri" | "docker-context"; egressNetwork?: string; platform?: string } = {},
): string[] {
  const mode = resolveMode(options.mode, ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {}), options.platform ?? processPlatform());
  const context = options.context ?? "kiri";
  if (!CONTEXT.test(context)) throw new Error("Invalid Kiri Docker context");
  const egressNetwork = options.egressNetwork ?? "";
  if (network === "egress" && !CONTEXT.test(egressNetwork)) throw new Error("Managed Kiri egress network is unavailable");
  const podmanNetwork = network === "egress" ? egressNetwork : "none";
  const security = ["--init", "--pids-limit", "256", "--tmpfs", "/tmp:rw,nosuid,size=256m", "--network", podmanNetwork, "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only"];
  if (mode === "kiri") return [resolveKiriExecutable(options.kiriExecutable), "run", "--rm", "-i", ...security, image, ...argv];
  return [options.dockerExecutable ?? "docker", "--context", context, "run", "--rm", "-i", ...security, image, ...argv];
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
  readonly platform: string;
  readonly daemonHome: string;
  readonly systemdService: string;
  readonly systemctlExecutable: string;

  constructor(options: KiriProviderOptions = {}) {
    const hostEnv = ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {});
    const platform = options.platform ?? processPlatform();
    // Kiri's Docker-context mode must use the Docker-compatible client. Do
    // not run Podman's Windows path resolver against this executable.
    const dockerExecutable = options.dockerExecutable ?? hostEnv.MAO_DOCKER_EXECUTABLE ?? "docker";
    const context = options.context ?? hostEnv.MAO_KIRI_CONTEXT ?? "kiri";
    const mode = resolveMode(options.mode, hostEnv, platform);
    if (!CONTEXT.test(context)) throw new Error("Invalid Kiri Docker context");
    const runner = options.runner ?? runCommand;
    const kiriExecutable = resolveKiriExecutable(options.kiriExecutable ?? hostEnv.MAO_KIRI_EXECUTABLE);
    // `dataDir` is retained as a compatibility alias for daemon HOME. It is
    // deliberately not sent as KIRI_DATA_DIR, which selects Kiri's image
    // store and is not the state directory used by the Linux service.
    const kiriEnvironment = resolveKiriEnvironment({ socket: options.socket, socketGroup: options.socketGroup, platform });
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
      platform,
    });
    this.dockerExecutable = dockerExecutable;
    this.context = context;
    this.kiriExecutable = kiriExecutable;
    this.mode = mode;
    this.autoStart = options.autoStart ?? true;
    this.commandRunner = runner;
    this.kiriEnvironment = kiriEnvironment;
    this.platform = platform;
    this.daemonHome = options.daemonHome ?? options.dataDir ?? hostEnv.MAO_KIRI_DAEMON_HOME ?? "/var/lib/kiri-daemon";
    if (this.daemonHome.length === 0 || this.daemonHome.includes("\u0000")) throw new Error("Invalid Kiri daemon home");
    this.systemdService = options.systemdService ?? hostEnv.MAO_KIRI_SYSTEMD_SERVICE ?? "kiri.service";
    this.systemctlExecutable = options.systemctlExecutable ?? hostEnv.MAO_KIRI_SYSTEMCTL ?? "systemctl";
    if (!SERVICE.test(this.systemdService)) throw new Error("Invalid Kiri systemd service");
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
        return {
          available: result.exitCode === 0,
          platform: this.platform,
          executable: this.kiriExecutable,
          machine: this.platform === "linux" ? "linux-host" : null,
          rootless: null,
          reason: result.exitCode === 0 ? undefined : new TextDecoder().decode(result.stderr).trim() || "kiri-unavailable",
        };
      } catch (error) {
        return {
          available: false,
          platform: this.platform,
          executable: this.kiriExecutable,
          machine: this.platform === "linux" ? "linux-host" : null,
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
      return {
        available: result.exitCode === 0,
        platform: this.platform,
        executable: `${this.dockerExecutable} --context ${this.context}`,
        machine: this.context,
        rootless: null,
        reason: result.exitCode === 0 ? undefined : new TextDecoder().decode(result.stderr).trim() || "kiri-unavailable",
      };
    } catch (error) {
      return {
        available: false,
        platform: this.platform,
        executable: `${this.dockerExecutable} --context ${this.context}`,
        machine: this.context,
        rootless: null,
        reason: error instanceof Error ? error.message : "kiri-unavailable",
      };
    }
  }

  async ensureReady(): Promise<RuntimeHealth> {
    const current = await this.health();
    if (current.available || !this.autoStart || this.platform !== "linux") return current;
    // The packaged Linux daemon is a root-owned systemd service. Starting it
    // through systemd lets the distribution apply Delegate, RuntimeDirectory,
    // socket ACL and state-directory policy instead of spawning an unprivileged
    // daemon from the Bun process.
    try {
      const started = await this.commandRunner([this.systemctlExecutable, "start", this.systemdService], { timeoutMs: 30_000 });
      if (started.exitCode === 0) {
        for (let attempt = 0; attempt < 50; attempt += 1) {
          await new Promise((resolve) => setTimeout(resolve, 100));
          const ready = await this.health();
          if (ready.available) return ready;
        }
      }
    } catch {
      // A user service or a container image may not have systemd. Continue to
      // the explicitly permitted root fallback below.
    }
    const bun = (globalThis as { Bun?: { spawn: (argv: string[], options: Record<string, unknown>) => any } }).Bun;
    const processLike = (globalThis as { process?: { getuid?: () => number } }).process;
    if (!bun || processLike?.getuid?.() !== 0) return current;
    try {
      const hostEnv = ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {});
      const daemonEnv = Object.fromEntries(Object.entries(hostEnv).filter(([, value]) => typeof value === "string")) as Record<string, string>;
      Object.assign(daemonEnv, this.kiriEnvironment);
      daemonEnv.HOME = this.daemonHome;
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

