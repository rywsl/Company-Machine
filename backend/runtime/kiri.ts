/** Kiri's Linux engine runs as a headless service on the host kernel. */

import { existsSync } from "node:fs";
import { join } from "node:path";

import { PodmanProvider, runCommand, type CommandRunner, type RuntimeHealth, type SandboxSpec } from "./podman";

type KiriMode = "linux-socket" | "kiri" | "docker-context";
type KiriClientOptions = {
  dockerExecutable?: string;
  kiriExecutable?: string;
  context?: string;
  mode?: KiriMode;
  platform?: string;
  socket?: string;
};

export type KiriProviderOptions = KiriClientOptions & {
  autoStart?: boolean;
  runner?: CommandRunner;
  egressNetwork?: string;
  egressEnv?: Record<string, string>;
};

const SERVICE_SOCKET = "/run/kiri/docker.sock";
const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/;

export function resolveKiriExecutable(configured?: string): string {
  if (configured) return configured;
  const { MAO_KIRI_EXECUTABLE, LOCALAPPDATA, ProgramFiles } = process.env;
  if (MAO_KIRI_EXECUTABLE) return MAO_KIRI_EXECUTABLE;
  const candidates = [
    LOCALAPPDATA ? join(LOCALAPPDATA, "Kiri", "bin", "kiri.exe") : "",
    ProgramFiles ? join(ProgramFiles, "Kiri", "bin", "kiri.exe") : "",
  ].filter(Boolean);
  return candidates.find((candidate) => existsSync(candidate)) ?? "kiri";
}

export function resolveKiriClient(options: KiriClientOptions = {}) {
  const platform = options.platform ?? process.platform;
  const mode = options.mode ?? process.env.MAO_KIRI_MODE ?? (platform === "linux" ? "linux-socket" : "kiri");
  if (mode !== "linux-socket" && mode !== "kiri" && mode !== "docker-context") {
    throw new Error("Invalid Kiri client mode");
  }
  const socket = options.socket ?? process.env.MAO_KIRI_SOCKET ?? process.env.KIRI_SOCKET ?? (platform === "linux" ? SERVICE_SOCKET : undefined);
  const docker = options.dockerExecutable ?? process.env.MAO_DOCKER_EXECUTABLE
    ?? (platform === "linux" && existsSync("/usr/libexec/kiri/docker") ? "/usr/libexec/kiri/docker" : "docker");
  const env: Record<string, string> = {};
  if (mode === "docker-context") {
    const context = options.context ?? process.env.MAO_KIRI_CONTEXT ?? "kiri";
    if (!NAME.test(context)) throw new Error("Invalid Kiri Docker context");
    return { mode, platform, machine: context, argv: [docker, "--context", context], env, managedService: false };
  }
  if (socket !== undefined && (!socket || socket.includes("\u0000") || (platform === "linux" && !socket.startsWith("/")))) {
    throw new Error("Invalid Kiri socket path");
  }
  const machine = platform === "linux" ? "linux-host" : null;
  const managedService = platform === "linux" && socket === SERVICE_SOCKET;
  if (mode === "linux-socket") {
    if (platform !== "linux" || !socket) throw new Error("Kiri Linux socket mode requires a Linux host and an absolute socket path");
    env.DOCKER_CONTEXT = "";
    env.DOCKER_TLS_VERIFY = "";
    env.DOCKER_TLS = "";
    return { mode, platform, machine, argv: [docker, "--host", `unix://${socket}`], env, managedService };
  }
  if (socket) env.KIRI_SOCKET = socket;
  return { mode, platform, machine, argv: [resolveKiriExecutable(options.kiriExecutable)], env, managedService };
}

export function buildKiriHarnessCommand(
  image: string,
  argv: string[],
  network: "none" | "egress" = "none",
  options: KiriClientOptions & { egressNetwork?: string; envNames?: string[] } = {},
) {
  const client = resolveKiriClient(options);
  const networkName = network === "none" ? "none" : options.egressNetwork;
  if (!networkName || !NAME.test(networkName) || networkName === "host") throw new Error("Managed Kiri egress network is unavailable");
  return {
    argv: [
      ...client.argv, "run", "--rm", "-i", "--init", "--pids-limit", "256",
      "--tmpfs", "/tmp:rw,nosuid,size=256m", "--network", networkName,
      "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
      ...(options.envNames ?? []).flatMap((name) => ["--env", name]),
      image, ...argv,
    ],
    env: client.env,
  };
}

export class KiriProvider extends PodmanProvider {
  readonly name = "kiri";
  private readonly client: ReturnType<typeof resolveKiriClient>;
  private readonly commandRunner: CommandRunner;
  private readonly autoStart: boolean;
  private readiness?: Promise<RuntimeHealth>;

  constructor(options: KiriProviderOptions = {}) {
    const client = resolveKiriClient(options);
    const runner = options.runner ?? runCommand;
    super({
      executable: client.argv[0],
      runner: (argv, commandOptions) => runner(argv, {
        ...commandOptions,
        env: { ...commandOptions?.env, ...client.env },
      }),
      egressNetwork: options.egressNetwork,
      egressEnv: options.egressEnv,
    });
    this.client = client;
    this.commandRunner = runner;
    this.autoStart = options.autoStart ?? true;
  }

  protected command(...args: string[]): string[] {
    return [...this.client.argv, ...args];
  }

  async health(timeoutMs = 5_000): Promise<RuntimeHealth> {
    return (await this.probe(timeoutMs)).health;
  }

  private async probe(timeoutMs = 5_000): Promise<{ health: RuntimeHealth; canStartService: boolean }> {
    const { platform, machine, env } = this.client;
    const status = { available: false, platform, machine, executable: this.client.argv.join(" "), rootless: null };
    try {
      const result = await this.commandRunner(this.command("info", "--format", "{{.OperatingSystem}}"), { timeoutMs, env });
      if (result.exitCode === 0) {
        if (new TextDecoder().decode(result.stdout).trim() === "Kiri") {
          return { health: { ...status, available: true }, canStartService: false };
        }
        return { health: { ...status, reason: "The configured endpoint did not identify itself as Kiri" }, canStartService: false };
      }
      const reason = new TextDecoder().decode(result.stderr).trim() || "Kiri is unavailable";
      const permissionDenied = /permission denied while trying to connect|connect: permission denied/i.test(reason);
      const accessHint = permissionDenied && this.client.managedService
        ? '. Run sudo kiri host-access "$USER" for the MAO account, then start a new login session'
        : "";
      return { health: { ...status, reason: reason + accessHint }, canStartService: !permissionDenied };
    } catch (error) {
      const reason = error instanceof Error ? error.message : "Kiri is unavailable";
      const code = (error as { code?: string } | null)?.code;
      return { health: { ...status, reason }, canStartService: code !== "ENOENT" && code !== "EACCES" };
    }
  }

  async ensureReady(): Promise<RuntimeHealth> {
    this.readiness ??= this.startService().finally(() => { this.readiness = undefined; });
    return this.readiness;
  }

  private async startService(): Promise<RuntimeHealth> {
    let { health, canStartService } = await this.probe();
    if (!canStartService || !this.autoStart || !this.client.managedService) return health;
    try {
      const started = await this.commandRunner(["systemctl", "--no-ask-password", "start", "kiri.service"], { timeoutMs: 15_000 });
      if (started.exitCode !== 0) {
        const reason = new TextDecoder().decode(started.stderr).trim();
        return { ...health, reason: [health.reason, reason || "Could not start kiri.service"].filter(Boolean).join(". ") };
      }
    } catch (error) {
      return { ...health, reason: [health.reason, error instanceof Error ? error.message : "Could not start kiri.service"].filter(Boolean).join(". ") };
    }

    // The packaged service is Type=simple and can report started before the API binds.
    const deadline = performance.now() + 5_000;
    while (performance.now() < deadline) {
      ({ health, canStartService } = await this.probe(Math.max(1, Math.ceil(deadline - performance.now()))));
      if (!canStartService) return health;
      await new Promise((resolve) => setTimeout(resolve, Math.min(200, Math.max(0, deadline - performance.now()))));
    }
    return health;
  }
}
