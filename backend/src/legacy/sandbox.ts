import { existsSync } from "node:fs";
import { resolve } from "node:path";
import type { ContainerProvider, Sandbox, SandboxSpec } from "../../runtime/podman";
import { spawnLoggedProcess, type LoggedProcess } from "../../runtime/process-logs";
import { FactoryError } from "./errors";

export type LocalCommandMap = Record<string, string[]>;

export function sandboxResources(value: unknown): Pick<SandboxSpec, "cpus" | "memoryMb" | "shmMb" | "tmpMb" | "pids" | "writable"> {
  const invalid = (field: string): never => { throw new FactoryError("RESOURCES_INVALID", "资源声明不合法", { field }); };
  if (value === undefined || value === null) return {};
  if (typeof value !== "object" || Array.isArray(value)) return invalid("resources");
  const resources = value as Record<string, unknown>;
  const positiveInteger = (field: string): number | undefined => {
    const amount = resources[field];
    if (amount === undefined || amount === null) return undefined;
    if (typeof amount !== "number" || !Number.isSafeInteger(amount) || amount < 1) return invalid(field);
    return amount;
  };
  let cpus: number | undefined;
  if (resources.cpus !== undefined && resources.cpus !== null) {
    if (typeof resources.cpus !== "number" && typeof resources.cpus !== "string") return invalid("cpus");
    cpus = Number(resources.cpus);
    if (!Number.isFinite(cpus) || cpus <= 0) return invalid("cpus");
  }
  let memoryMb: number | undefined;
  if (resources.memory !== undefined && resources.memory !== null) {
    const memory = /^([0-9]+)(Mi|M|MB|Gi|G|GB)$/.exec(String(resources.memory));
    if (!memory) return invalid("memory");
    memoryMb = Number(memory[1]) * (memory[2].startsWith("G") ? 1024 : 1);
    if (!Number.isSafeInteger(memoryMb) || memoryMb < 1) return invalid("memory");
  }
  const writable = resources.writable;
  if (writable !== undefined && (!Array.isArray(writable) || writable.some((entry) => typeof entry !== "string") || new Set(writable).size !== writable.length)) {
    return invalid("writable");
  }
  return { cpus, memoryMb, shmMb: positiveInteger("shm_mb"), tmpMb: positiveInteger("tmp_mb"), pids: positiveInteger("pids"), writable };
}

const inheritedKeys = ["PATH", "SystemRoot", "WINDIR", "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL"];
const virtualEnvironment = {
  FACTORY_IN: "/factory/in",
  FACTORY_OUT: "/factory/out",
  FACTORY_CTL: "/factory/ctl",
  FACTORY_WORKSPACE: "/workspace",
};

function replacePath(value: string, mappings: Array<[string, string]>): string {
  for (const [virtual, host] of mappings) if (value === virtual || value.startsWith(`${virtual}/`)) return `${host}${value.slice(virtual.length).replaceAll("/", process.platform === "win32" ? "\\" : "/")}`;
  return value;
}
function mapping(spec: SandboxSpec): Array<[string, string]> {
  return spec.mounts.filter((mount) => !mount.source.startsWith("volume:")).map((mount) => [mount.target, resolve(mount.source)]);
}

export class LocalProvider implements ContainerProvider {
  readonly name = "local";
  constructor(readonly commands: LocalCommandMap = {}) {}
  buildRunArgv(spec: SandboxSpec): string[] { return this.commands[spec.image] ?? []; }
  async ensureReady(): Promise<{ available: true; platform: string; executable: string; machine: null; rootless: null }> { return { available: true, platform: process.platform, executable: "local", machine: null, rootless: null }; }
  async health(): Promise<{ available: true; platform: string; executable: string; machine: null; rootless: null }> { return this.ensureReady(); }
  async open(spec: SandboxSpec): Promise<Sandbox> { return new LocalSandboxImpl(spec, this.commands); }
  async resolveImage(image: string): Promise<string> { return image; }
  async prepareInjectVolume(_image: string, _volume: string): Promise<void> {}
  async removeVolume(_name: string): Promise<boolean> { return false; }
  async reap(_jobId: string, _attempt: number): Promise<number> { return 0; }
}

class LocalSandboxImpl implements Sandbox {
  private readonly children = new Set<LoggedProcess>();
  private closed = false;
  constructor(private readonly spec: SandboxSpec, private readonly commands: LocalCommandMap) {}
  get name(): string { return this.spec.name; }
  async exec(argv: string[], options: Parameters<Sandbox["exec"]>[1]) {
    if (this.closed) throw new FactoryError("SANDBOX_CLOSED", "Local sandbox is closed", { name: this.name });
    if (!argv.length || options.timeoutMs <= 0) throw new FactoryError("SANDBOX_SPEC_INVALID", "Invalid local exec request");
    const mappings = mapping(this.spec);
    const command = this.commands[argv[0]] ? [...this.commands[argv[0]], ...argv.slice(1).map((item) => replacePath(item, mappings))] : argv.map((item) => replacePath(item, mappings));
    const environment: Record<string, string> = {};
    for (const key of inheritedKeys) {
      const value = process.env[key];
      if (typeof value === "string") environment[key] = value;
    }
    Object.assign(environment, this.spec.env, options.env);
    for (const [key, value] of Object.entries(virtualEnvironment)) environment[key] = replacePath(value, mappings);
    const factoryctl = replacePath("/opt/factory/bin/factoryctl", mappings);
    if (factoryctl !== "/opt/factory/bin/factoryctl") environment.FACTORYCTL = factoryctl;
    const mappedWorkdir = replacePath(options.workdir ?? this.spec.workdir ?? "/workspace", mappings);
    const workdir = existsSync(mappedWorkdir) ? mappedWorkdir : process.cwd();
    const started = Date.now();
    const child = spawnLoggedProcess(command, options, { cwd: workdir, env: environment });
    this.children.add(child);
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; child.kill(9); }, options.timeoutMs);
    try {
      const exitCode = await child.exited;
      return { exitCode: timedOut ? null : exitCode, timedOut, durationMs: Date.now() - started };
    } finally { clearTimeout(timer); this.children.delete(child); }
  }
  async close(): Promise<void> {
    this.closed = true;
    const children = [...this.children];
    for (const child of children) child.kill(9);
    await Promise.allSettled(children.map((child) => child.exited));
  }
}
