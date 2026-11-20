import { existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import type { SandboxSpec } from "../../runtime/podman";
import { FactoryError } from "./errors";

export interface LocalExecResult { exitCode: number | null; timedOut: boolean; durationMs: number; }
export type LocalCommandMap = Record<string, string[]>;
export interface LocalSandbox { exec(argv: string[], options: { timeoutMs: number; stdout: string; stderr: string; env?: Record<string, string>; workdir?: string }): Promise<LocalExecResult>; close(): Promise<void>; }

function replacePath(value: string, mappings: Array<[string, string]>): string {
  for (const [virtual, host] of mappings) if (value === virtual || value.startsWith(`${virtual}/`)) return `${host}${value.slice(virtual.length).replaceAll("/", process.platform === "win32" ? "\\" : "/")}`;
  return value;
}
function mapping(spec: SandboxSpec): Array<[string, string]> {
  return spec.mounts.filter((mount) => !mount.source.startsWith("volume:")).map((mount) => [mount.target, resolve(mount.source)]);
}

export class LocalProvider {
  readonly name = "local";
  constructor(readonly commands: LocalCommandMap = {}) {}
  buildRunArgv(spec: SandboxSpec): string[] { return this.commands[spec.image] ?? []; }
  async ensureReady(): Promise<{ available: true; platform: string; executable: string; machine: null; rootless: null }> { return { available: true, platform: process.platform, executable: "local", machine: null, rootless: null }; }
  async health(): Promise<{ available: true; platform: string; executable: string; machine: null; rootless: null }> { return this.ensureReady(); }
  async open(spec: SandboxSpec): Promise<LocalSandbox> { return new LocalSandboxImpl(spec, this.commands); }
  async resolveImage(image: string): Promise<string> { return image; }
  async prepareInjectVolume(_image: string, _volume: string): Promise<void> {}
  async removeVolume(_name: string): Promise<boolean> { return false; }
  async reap(_jobId: string, _attempt: number): Promise<number> { return 0; }
}

class LocalSandboxImpl implements LocalSandbox {
  private readonly children = new Set<any>();
  constructor(private readonly spec: SandboxSpec, private readonly commands: LocalCommandMap) {}
  async exec(argv: string[], options: { timeoutMs: number; stdout: string; stderr: string; env?: Record<string, string>; workdir?: string }): Promise<LocalExecResult> {
    if (!argv.length || options.timeoutMs <= 0) throw new FactoryError("SANDBOX_SPEC_INVALID", "Invalid local exec request");
    const mappings = mapping(this.spec);
    const command = this.commands[argv[0]] ? [...this.commands[argv[0]], ...argv.slice(1).map((item) => replacePath(item, mappings))] : argv.map((item) => replacePath(item, mappings));
    const environment: Record<string, string> = { ...process.env as Record<string, string>, ...(options.env ?? {}) };
    for (const [key, value] of Object.entries(environment)) if (value === undefined) delete environment[key];
    for (const [key, value] of Object.entries({ FACTORY_IN: "/factory/in", FACTORY_OUT: "/factory/out", FACTORY_CTL: "/factory/ctl", FACTORY_WORKSPACE: "/workspace" })) if (environment[key]) environment[key] = replacePath(value, mappings);
    const mappedWorkdir = replacePath(options.workdir ?? this.spec.workdir ?? "/workspace", mappings);
    const workdir = existsSync(mappedWorkdir) ? mappedWorkdir : process.cwd();
    mkdirSync(dirname(options.stdout), { recursive: true });
    mkdirSync(dirname(options.stderr), { recursive: true });
    const started = Date.now();
    const bun = (globalThis as { Bun?: { spawn: (argv: string[], options: Record<string, unknown>) => any } }).Bun;
    if (!bun) throw new FactoryError("RUNTIME_UNAVAILABLE", "Bun runtime is required");
    const child = bun.spawn(command, { cwd: workdir, env: environment, stdout: "pipe", stderr: "pipe" });
    this.children.add(child);
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const result = await Promise.race([
        child.exited.then(async (exitCode: number) => ({ exitCode, timedOut: false, stdout: await new Response(child.stdout).arrayBuffer(), stderr: await new Response(child.stderr).arrayBuffer() })),
        new Promise<{ exitCode: null; timedOut: true; stdout: ArrayBuffer; stderr: ArrayBuffer }>((resolve) => { timer = setTimeout(() => { child.kill(); resolve({ exitCode: null, timedOut: true, stdout: new ArrayBuffer(0), stderr: new ArrayBuffer(0) }); }, options.timeoutMs); }),
      ]);
      await Bun.write(options.stdout, result.stdout);
      await Bun.write(options.stderr, result.stderr);
      return { exitCode: result.exitCode, timedOut: result.timedOut, durationMs: Date.now() - started };
    } finally { if (timer) clearTimeout(timer); this.children.delete(child); }
  }
  async close(): Promise<void> { for (const child of this.children) child.kill(); this.children.clear(); }
}
