/**
 * Engine-neutral sandbox contracts and a Podman CLI implementation.
 *
 * The control plane supplies an argv array and receives structured results.
 * This module never invokes a shell and never talks to a model provider.
 */

import { appendFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import type { EgressPlan } from "../src/legacy/egress";
import { spawnLoggedProcess, type LogPaths } from "./process-logs";

export type NetworkMode = "none" | "egress";

export type Mount = {
  source: string;
  target: string;
  readonly: boolean;
};

export type SandboxSpec = {
  name: string;
  image: string;
  mounts: Mount[];
  workdir?: string;
  env?: Record<string, string>;
  network?: NetworkMode;
  egressPlan?: EgressPlan;
  cpus?: number;
  memoryMb?: number;
  workspaceVolume?: string;
  shmMb?: number;
  tmpMb?: number;
  pids?: number;
  writable?: string[];
  command?: string[];
};

export type ExecResult = {
  exitCode: number | null;
  timedOut: boolean;
  durationMs: number;
};

export type CommandResult = {
  exitCode: number;
  stdout: Uint8Array;
  stderr: Uint8Array;
  logsWritten?: true;
};

export type CommandOptions = {
  timeoutMs?: number;
  env?: Record<string, string>;
  logs?: LogPaths;
};

export type CommandRunner = (
  argv: string[],
  options?: CommandOptions,
) => Promise<CommandResult>;

export type Sandbox = {
  readonly name: string;
  exec(
    argv: string[],
    options: {
      timeoutMs: number;
      stdout: string;
      stderr: string;
      env?: Record<string, string>;
      workdir?: string;
    },
  ): Promise<ExecResult>;
  close(): Promise<void>;
};

export type RuntimeHealth = {
  available: boolean;
  platform: string;
  executable: string;
  machine: string | null;
  rootless: boolean | null;
  reason?: string;
};

export type ContainerProvider = {
  readonly name: string;
  buildRunArgv(spec: SandboxSpec): string[];
  health(): Promise<RuntimeHealth>;
  ensureReady(): Promise<RuntimeHealth>;
  resolveImage(image: string): Promise<string>;
  prepareInjectVolume(image: string, volume: string): Promise<void>;
  open(spec: SandboxSpec): Promise<Sandbox>;
  removeVolume(name: string): Promise<boolean>;
  reap(jobId: string, attempt: number): Promise<number>;
};

const IMAGE_REF = /^(?:.+@)?sha256:[0-9a-f]{64}$/;
const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/;
const VOLUME = /^[A-Za-z0-9][A-Za-z0-9_.-]+$/;
const NETWORK = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/;
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
const EGRESS_ENV = new Set(["ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"]);
const FIXED_ENV: Array<[string, string]> = [
  ["FACTORY_IN", "/factory/in"],
  ["FACTORY_OUT", "/factory/out"],
  ["FACTORY_CTL", "/factory/ctl"],
  ["FACTORY_WORKSPACE", "/workspace"],
  ["FACTORYCTL", "/opt/factory/bin/factoryctl"],
  ["HOME", "/tmp/home"],
  ["PATH", "/opt/factory/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"],
];

export class PodmanError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly detail: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "PodmanError";
  }
}

function platformName(): string {
  const processLike = (globalThis as { process?: { platform?: string } }).process;
  return processLike?.platform ?? "unknown";
}

function environment(): Record<string, string | undefined> {
  return ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {});
}

/** Resolve the user-local Windows installation when the parent process has a stale PATH. */
export function resolvePodmanExecutable(configured?: string): string {
  if (configured && configured !== "podman") return configured;
  const env = environment();
  if (env.MAO_PODMAN_EXECUTABLE) return env.MAO_PODMAN_EXECUTABLE;
  if (platformName() !== "win32") return configured ?? "podman";
  const localAppData = env.LOCALAPPDATA;
  if (!localAppData) return configured ?? "podman";
  const root = join(localAppData, "Programs", "Podman");
  for (const version of ["6.2.0", "5.8.10"]) {
    const candidate = join(root, `podman-${version}`, "usr", "bin", "podman.exe");
    if (existsSync(candidate)) return candidate;
  }
  return configured ?? "podman";
}

export async function runCommand(argv: string[], options: CommandOptions = {}): Promise<CommandResult> {
  const bun = (globalThis as { Bun?: { spawn: (argv: string[], options: Record<string, unknown>) => any } }).Bun;
  if (!bun) throw new PodmanError("RUNTIME_UNAVAILABLE", "Bun runtime is required");
  const env = options.env === undefined ? undefined : {
    ...Object.fromEntries(Object.entries(environment()).filter((entry): entry is [string, string] => typeof entry[1] === "string")),
    ...options.env,
  };
  const timeoutMs = options.timeoutMs ?? 60_000;
  if (options.logs) {
    const child = spawnLoggedProcess(argv, options.logs, env === undefined ? {} : { env });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill(9);
    }, timeoutMs);
    try {
      const exitCode = await child.exited;
      if (timedOut) throw new PodmanError("PODMAN_TIMEOUT", "Container runtime command timed out", { executable: argv[0], logsWritten: true });
      return { exitCode, stdout: new Uint8Array(), stderr: new Uint8Array(), logsWritten: true };
    } finally {
      clearTimeout(timer);
    }
  }
  const process = bun.spawn(argv, { stdout: "pipe", stderr: "pipe", ...(env === undefined ? {} : { env }) });
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const result = await Promise.race([
      Promise.all([
        process.exited as Promise<number>,
        new Response(process.stdout).arrayBuffer(),
        new Response(process.stderr).arrayBuffer(),
      ]).then(([exitCode, stdout, stderr]) => ({
        exitCode,
        stdout: new Uint8Array(stdout),
        stderr: new Uint8Array(stderr),
      })),
      new Promise<CommandResult>((_, reject) => {
        timer = setTimeout(() => {
          process.kill();
          reject(new PodmanError("PODMAN_TIMEOUT", "Container runtime command timed out", { executable: argv[0] }));
        }, timeoutMs);
      }),
    ]);
    return result;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function text(bytes: Uint8Array): string {
  return new TextDecoder().decode(bytes).trim();
}

export function parseImageId(output: string): string | undefined {
  const matched = /^(?:sha256:)?([0-9a-f]{64})$/.exec(output.trim());
  return matched ? `sha256:${matched[1]}` : undefined;
}

function assertAbsolutePath(value: string, field: string): void {
  if (!value.startsWith("/") || value.includes("\u0000") || value.toLowerCase().includes("docker.sock")) {
    throw new PodmanError("SANDBOX_SPEC_INVALID", `${field} must be an absolute sandbox path`, { field });
  }
}

function assertWritablePath(value: string): void {
  assertAbsolutePath(value, "writable");
  const banned = ["/factory", "/workspace", "/opt/factory", "/tmp"];
  if (banned.some((root) => value === root || value.startsWith(`${root}/`))) {
    throw new PodmanError("RESOURCES_INVALID", "Writable path overlaps a managed sandbox path", { field: "writable" });
  }
}

function assertSpec(spec: SandboxSpec, allowEgress: boolean): void {
  if (!NAME.test(spec.name)) throw new PodmanError("SANDBOX_SPEC_INVALID", "Invalid sandbox name", { field: "name" });
  if (!IMAGE_REF.test(spec.image)) throw new PodmanError("IMAGE_REF_INVALID", "Invalid image reference", { image: spec.image });
  const network = spec.network ?? "none";
  if (network !== "none" && network !== "egress") throw new PodmanError("SANDBOX_SPEC_INVALID", "Invalid network", { field: "network" });
  if (network === "egress" && !allowEgress) throw new PodmanError("EGRESS_UNSUPPORTED", "Egress requires the managed proxy", { network });
  assertAbsolutePath(spec.workdir ?? "/workspace", "workdir");
  if (spec.cpus !== undefined && (!Number.isFinite(spec.cpus) || spec.cpus <= 0)) throw new PodmanError("SANDBOX_SPEC_INVALID", "Invalid CPU limit", { field: "cpus" });
  if (spec.memoryMb !== undefined && (!Number.isInteger(spec.memoryMb) || spec.memoryMb < 1)) throw new PodmanError("SANDBOX_SPEC_INVALID", "Invalid memory limit", { field: "memoryMb" });
  if (spec.pids !== undefined && (!Number.isInteger(spec.pids) || spec.pids < 1)) throw new PodmanError("SANDBOX_SPEC_INVALID", "Invalid pids limit", { field: "pids" });
  if (spec.tmpMb !== undefined && (!Number.isInteger(spec.tmpMb) || spec.tmpMb < 1)) throw new PodmanError("SANDBOX_SPEC_INVALID", "Invalid tmpfs limit", { field: "tmpMb" });
  const targets = new Set<string>();
  for (const mount of spec.mounts) {
    assertAbsolutePath(mount.target, "mount.target");
    if (mount.source.toLowerCase().includes("docker.sock") || mount.source.includes("\u0000")) {
      throw new PodmanError("SANDBOX_SPEC_INVALID", "Unsafe mount source", { field: "mount.source" });
    }
    if (targets.has(mount.target)) throw new PodmanError("SANDBOX_SPEC_INVALID", "Duplicate mount target", { field: "mount.target" });
    targets.add(mount.target);
    if (mount.source.startsWith("volume:")) {
      if (!VOLUME.test(mount.source.slice("volume:".length))) throw new PodmanError("SANDBOX_SPEC_INVALID", "Invalid volume name", { field: "mount.source" });
    } else if (mount.source.includes(",")) {
      throw new PodmanError("SANDBOX_SPEC_INVALID", "Mount source cannot contain commas", { field: "mount.source" });
    }
  }
  if (spec.workspaceVolume !== undefined) {
    if (!VOLUME.test(spec.workspaceVolume)) throw new PodmanError("SANDBOX_SPEC_INVALID", "Invalid workspace volume", { field: "workspaceVolume" });
    if (targets.has("/workspace")) throw new PodmanError("SANDBOX_SPEC_INVALID", "Workspace mount is duplicated", { field: "workspaceVolume" });
  }
  for (const [key, value] of Object.entries(spec.env ?? {})) {
    if (!ENV_NAME.test(key) || value.includes("\u0000")) throw new PodmanError("SANDBOX_SPEC_INVALID", "Invalid environment variable", { field: key });
  }
}

export class PodmanProvider implements ContainerProvider {
  readonly name: string = "podman";
  private readonly run: CommandRunner;
  private readonly allowUnproxiedEgress: boolean;
  private readonly egressNetwork: string | null;
  private readonly egressEnv: Record<string, string>;
  private readonly executable: string;
  private readonly machine: string | null;
  private readonly platformOverride: string | undefined;

  constructor(options: {
    executable?: string;
    allowUnproxiedEgress?: boolean;
    /** Podman network created and owned by the MAO egress proxy. */
    egressNetwork?: string;
    /** Proxy variables supplied by the managed egress service. */
    egressEnv?: Record<string, string>;
    machine?: string;
    platform?: string;
    runner?: CommandRunner;
  } = {}) {
    this.executable = resolvePodmanExecutable(options.executable);
    this.allowUnproxiedEgress = options.allowUnproxiedEgress ?? false;
    this.egressNetwork = options.egressNetwork ?? null;
    this.egressEnv = { ...(options.egressEnv ?? {}) };
    for (const [key, value] of Object.entries(this.egressEnv)) {
      if (!EGRESS_ENV.has(key) || value.includes("\u0000")) {
        throw new PodmanError("SANDBOX_SPEC_INVALID", "Only managed proxy environment variables may be configured", { field: `egressEnv.${key}` });
      }
    }
    if (this.egressNetwork !== null && (!NETWORK.test(this.egressNetwork) || this.egressNetwork === "host")) {
      throw new PodmanError("SANDBOX_SPEC_INVALID", "Invalid managed egress network", { field: "egressNetwork" });
    }
    this.machine = options.machine ?? null;
    this.platformOverride = options.platform;
    this.run = options.runner ?? runCommand;
  }

  protected command(...args: string[]): string[] {
    return [this.executable, ...args];
  }

  buildRunArgv(spec: SandboxSpec): string[] {
    this.requireSupportedNetwork(spec);
    const managedEgress = spec.network === "egress" && this.egressNetwork !== null;
    assertSpec(spec, this.allowUnproxiedEgress || managedEgress);
    const network = spec.network === "egress" ? (this.egressNetwork ?? "bridge") : "none";
    const tmpMb = spec.tmpMb ?? 256;
    const argv = [
      ...this.command("run"), "--name", spec.name, "--detach", "--init", "--user", "10001:10001",
      "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--pids-limit", String(spec.pids ?? 256),
      "--network", network, "--tmpfs", `/tmp:rw,nosuid,size=${tmpMb}m`,
    ];
    for (const path of spec.writable ?? []) {
      assertWritablePath(path);
      argv.push("--tmpfs", `${path}:rw,nosuid,size=${tmpMb}m,mode=1777`);
    }
    if (spec.shmMb !== undefined) argv.push("--shm-size", `${spec.shmMb}m`);
    if (spec.cpus !== undefined) argv.push("--cpus", String(spec.cpus));
    if (spec.memoryMb !== undefined) argv.push("--memory", `${spec.memoryMb}m`);
    for (const mount of spec.mounts) {
      const source = mount.source.startsWith("volume:") ? mount.source.slice("volume:".length) : mount.source;
      const type = mount.source.startsWith("volume:") ? "volume" : "bind";
      argv.push("--mount", `type=${type},source=${source},target=${mount.target}${mount.readonly ? ",readonly" : ""}`);
    }
    if (spec.workspaceVolume) argv.push("--mount", `type=volume,source=${spec.workspaceVolume},target=/workspace`);
    for (const [key, value] of FIXED_ENV) argv.push("--env", `${key}=${value}`);
    for (const key of Object.keys(spec.env ?? {}).sort()) {
      if (FIXED_ENV.some(([fixed]) => fixed === key)) continue;
      argv.push("--env", `${key}=${spec.env?.[key]}`);
    }
    if (spec.network === "egress") {
      for (const key of Object.keys(this.egressEnv).sort()) {
        if (FIXED_ENV.some(([fixed]) => fixed === key) || Object.prototype.hasOwnProperty.call(spec.env ?? {}, key)) continue;
        argv.push("--env", `${key}=${this.egressEnv[key]}`);
      }
    }
    argv.push("--workdir", spec.workdir ?? "/workspace", spec.image);
    if (spec.command?.length) argv.push("/bin/sh", "-c", 'mkdir -p /tmp/home && exec "$@"', "sh", ...spec.command);
    else argv.push("/bin/sh", "-c", "mkdir -p /tmp/home && exec sleep 2147483647");
    return argv;
  }

  async health(): Promise<RuntimeHealth> {
    const platform = this.platformOverride ?? platformName();
    try {
      const result = await this.run([this.executable, "info", "--format", "json"], { timeoutMs: 10_000 });
      if (result.exitCode !== 0) return { available: false, platform, executable: this.executable, machine: this.machine, rootless: null, reason: text(result.stderr) || "podman-info-failed" };
      let rootless: boolean | null = null;
      try {
        const info = JSON.parse(text(result.stdout)) as { host?: { security?: { rootless?: boolean } } };
        rootless = typeof info.host?.security?.rootless === "boolean" ? info.host.security.rootless : null;
      } catch { /* older Podman can return a non-JSON info response */ }
      if (platform === "linux" && rootless !== true) {
        return { available: false, platform, executable: this.executable, machine: this.machine, rootless, reason: rootless === false ? "podman-rootless-required" : "podman-rootless-unknown" };
      }
      return { available: true, platform, executable: this.executable, machine: this.machine, rootless };
    } catch (error) {
      return { available: false, platform, executable: this.executable, machine: this.machine, rootless: null, reason: error instanceof Error ? error.message : "podman-unavailable" };
    }
  }

  async ensureReady(): Promise<RuntimeHealth> {
    const current = await this.health();
    if (current.available) return current;
    if (platformName() !== "win32") return current;
    const listed = await this.run([this.executable, "machine", "list", "--format", "json"], { timeoutMs: 15_000 });
    if (listed.exitCode !== 0) return { ...current, reason: text(listed.stderr) || current.reason };
    let machine = this.machine;
    try {
      const rows = JSON.parse(text(listed.stdout)) as Array<{ Name?: string; name?: string }>;
      machine ??= rows[0]?.Name ?? rows[0]?.name ?? null;
    } catch { /* preserve the configured machine when listing is not JSON */ }
    if (!machine) return { ...current, reason: "podman-machine-missing" };
    const started = await this.run([this.executable, "machine", "start", machine], { timeoutMs: 120_000 });
    if (started.exitCode !== 0) return { ...current, machine, reason: text(started.stderr) || "podman-machine-start-failed" };
    return this.health();
  }

  async resolveImage(image: string): Promise<string> {
    if (!IMAGE_REF.test(image)) throw new PodmanError("IMAGE_REF_INVALID", "Invalid image reference", { image });
    const direct = await this.run(this.command("image", "inspect", "--format", "{{.Id}}", image), { timeoutMs: 60_000 });
    if (direct.exitCode === 0 && parseImageId(text(direct.stdout))) return image;
    if (image.includes("@")) {
      const digest = image.slice(image.indexOf("@") + 1);
      const fallback = await this.run(this.command("image", "inspect", "--format", "{{.Id}}", digest), { timeoutMs: 60_000 });
      const id = parseImageId(text(fallback.stdout));
      if (fallback.exitCode === 0 && id) return id;
    }
    throw new PodmanError("IMAGE_NOT_FOUND", `Image was not found in ${this.name}`, { image });
  }

  async prepareInjectVolume(image: string, volume: string): Promise<void> {
    if (!VOLUME.test(volume)) throw new PodmanError("SANDBOX_SPEC_INVALID", "Invalid injection volume", { field: "volume" });
    await this.requireReady();
    const resolved = await this.resolveImage(image);
    const created = await this.run(this.command("volume", "create", volume), { timeoutMs: 60_000 });
    if (created.exitCode !== 0) throw new PodmanError("PODMAN_FAILED", `${this.name} volume create failed`, { stderr: text(created.stderr) });
    const init = await this.run([
      ...this.command("run"), "--rm", "--network", "none", "--user", "10001:10001", "--cap-drop", "ALL",
      "--security-opt", "no-new-privileges", "--mount", `type=volume,source=${volume},target=/opt/operator`, resolved, "/bin/true",
    ], { timeoutMs: 120_000 });
    if (init.exitCode !== 0) {
      await this.removeVolume(volume).catch(() => {});
      throw new PodmanError("PODMAN_FAILED", `${this.name} injection volume initialization failed`, { stderr: text(init.stderr) });
    }
  }

  async open(spec: SandboxSpec): Promise<Sandbox> {
    this.requireSupportedNetwork(spec);
    await this.requireReady();
    const resolved = await this.resolveImage(spec.image);
    if (spec.workspaceVolume) {
      const volume = await this.run(this.command("volume", "create", spec.workspaceVolume), { timeoutMs: 60_000 });
      if (volume.exitCode !== 0) throw new PodmanError("PODMAN_FAILED", `${this.name} workspace volume creation failed`, { stderr: text(volume.stderr) });
    }
    await this.run(this.command("rm", "-f", spec.name), { timeoutMs: 60_000 });
    const result = await this.run(this.buildRunArgv({ ...spec, image: resolved }), { timeoutMs: 120_000 });
    if (result.exitCode !== 0) throw new PodmanError("PODMAN_FAILED", `${this.name} run failed`, { stderr: text(result.stderr) });
    return new PodmanSandbox(this, spec.name);
  }

  private requireSupportedNetwork(spec: SandboxSpec): void {
    if (spec.egressPlan) throw new PodmanError("EGRESS_UNSUPPORTED", "Registry egress requires an isolated proxy transport", { network: spec.network });
  }

  private async requireReady(): Promise<void> {
    const ready = await this.ensureReady();
    if (!ready.available) throw new PodmanError("PODMAN_UNAVAILABLE", `${this.name} runtime is unavailable`, { reason: ready.reason });
  }

  async close(name: string): Promise<void> {
    const result = await this.run(this.command("rm", "-f", name), { timeoutMs: 60_000 });
    if (result.exitCode !== 0 && !text(result.stderr).toLowerCase().includes("no such container")) {
      throw new PodmanError("PODMAN_FAILED", `${this.name} container removal failed`, { name, stderr: text(result.stderr) });
    }
  }

  async removeVolume(name: string): Promise<boolean> {
    if (!VOLUME.test(name)) throw new PodmanError("SANDBOX_SPEC_INVALID", "Invalid volume name", { name });
    const result = await this.run(this.command("volume", "rm", name), { timeoutMs: 60_000 });
    if (result.exitCode === 0) return true;
    if (text(result.stderr).toLowerCase().includes("no such volume")) return false;
    throw new PodmanError("PODMAN_FAILED", `${this.name} volume removal failed`, { name, stderr: text(result.stderr) });
  }

  async reap(jobId: string, attempt: number): Promise<number> {
    const name = `mao-${jobId}-${attempt}`;
    let removed = 0;
    for (const argv of [this.command("rm", "-f", name), this.command("network", "rm", name)]) {
      const result = await this.run(argv, { timeoutMs: 60_000 });
      if (result.exitCode === 0) removed += 1;
    }
    if (await this.removeVolume(`${name}-operator`)) removed += 1;
    return removed;
  }

  async exec(name: string, argv: string[], options: { timeoutMs: number; stdout: string; stderr: string; env?: Record<string, string>; workdir?: string }): Promise<ExecResult> {
    if (!argv.length || options.timeoutMs <= 0) throw new PodmanError("SANDBOX_SPEC_INVALID", "Invalid exec request");
    const command = this.command("exec");
    if (options.workdir) command.push("--workdir", options.workdir);
    for (const [key, value] of Object.entries(options.env ?? {})) command.push("--env", `${key}=${value}`);
    command.push(name, ...argv);
    const started = Date.now();
    try {
      const result = await this.run(command, { timeoutMs: options.timeoutMs, logs: { stdout: options.stdout, stderr: options.stderr } });
      if (!result.logsWritten) {
        await writeBytes(options.stdout, result.stdout);
        await writeBytes(options.stderr, result.stderr);
      }
      return { exitCode: result.exitCode, timedOut: false, durationMs: Math.max(0, Date.now() - started) };
    } catch (error) {
      const diagnostic = error instanceof Error ? error.message : `${this.name} exec failed`;
      if (error instanceof PodmanError && error.detail.logsWritten === true) {
        appendFileSync(options.stderr, `\n${diagnostic}\n`);
      } else {
        await writeBytes(options.stdout, new Uint8Array());
        await writeBytes(options.stderr, new TextEncoder().encode(diagnostic));
      }
      if (error instanceof PodmanError && error.code === "PODMAN_TIMEOUT") return { exitCode: null, timedOut: true, durationMs: Math.max(0, Date.now() - started) };
      throw error;
    }
  }
}

class PodmanSandbox implements Sandbox {
  constructor(private readonly provider: PodmanProvider, public readonly name: string) {}
  exec(argv: string[], options: { timeoutMs: number; stdout: string; stderr: string; env?: Record<string, string>; workdir?: string }): Promise<ExecResult> {
    return this.provider.exec(this.name, argv, options);
  }
  async close(): Promise<void> {
    await this.provider.close(this.name);
  }
}

async function writeBytes(path: string, bytes: Uint8Array): Promise<void> {
  const bun = (globalThis as { Bun?: { write: (path: string, bytes: Uint8Array) => Promise<number> } }).Bun;
  if (!bun) throw new PodmanError("RUNTIME_UNAVAILABLE", "Bun runtime is required");
  await bun.write(path, bytes);
}

