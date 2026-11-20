import { existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import type { ContainerProvider, Sandbox, SandboxSpec } from "../../runtime/podman";
import { FactoryError } from "./errors";

export type LocalCommandMap = Record<string, string[]>;
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
  private readonly children = new Set<any>();
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
    mkdirSync(dirname(options.stdout), { recursive: true });
    mkdirSync(dirname(options.stderr), { recursive: true });
    const started = Date.now();
    const bun = (globalThis as { Bun?: { spawn: (argv: string[], options: Record<string, unknown>) => any } }).Bun;
    if (!bun) throw new FactoryError("RUNTIME_UNAVAILABLE", "Bun runtime is required");
    const child = bun.spawn(command, { cwd: workdir, env: environment, stdout: "pipe", stderr: "pipe" });
    this.children.add(child);
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; child.kill(9); }, options.timeoutMs);
    try {
      const [exitCode, stdout, stderr] = await Promise.all([
        child.exited as Promise<number>,
        new Response(child.stdout).arrayBuffer(),
        new Response(child.stderr).arrayBuffer(),
      ]);
      await Promise.all([Bun.write(options.stdout, stdout), Bun.write(options.stderr, stderr)]);
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
