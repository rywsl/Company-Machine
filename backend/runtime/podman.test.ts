import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { mkdir, rm } from "node:fs/promises";
import { PodmanError, PodmanProvider, runCommand, type CommandResult, type SandboxSpec } from "./podman";

const ok = (stdout = "", stderr = ""): CommandResult => ({
  exitCode: 0,
  stdout: new TextEncoder().encode(stdout),
  stderr: new TextEncoder().encode(stderr),
});

const spec: SandboxSpec = {
  name: "mao-job-1-1",
  image: `registry.example/worker@sha256:${"a".repeat(64)}`,
  mounts: [{ source: "volume:input", target: "/factory/in", readonly: true }],
  workspaceVolume: "workspace-1",
  env: { FACTORY_JOB_ID: "job-1", ZED: "1" },
  network: "none",
  cpus: 1,
  memoryMb: 512,
};

function expectPodmanError(run: () => unknown, code: string, detail?: Record<string, unknown>): void {
  try { run(); }
  catch (error) { expect(error).toMatchObject({ code, ...(detail ? { detail } : {}) }); return; }
  throw new Error(`Expected ${code}`);
}

describe("PodmanProvider", () => {
  test("builds a deterministic hardened argv", () => {
    const provider = new PodmanProvider({ executable: "podman-test", allowUnproxiedEgress: true });
    const argv = provider.buildRunArgv({ ...spec, network: "egress" });
    expect(argv).toContain("--cap-drop");
    expect(argv).toContain("ALL");
    expect(argv).toContain("--read-only");
    expect(argv).toContain("--security-opt");
    expect(argv).toContain("no-new-privileges");
    expect(argv).toContain("--network");
    expect(argv[argv.indexOf("--network") + 1]).toBe("bridge");
    expect(argv.join(" ").toLowerCase()).not.toContain("docker.sock");
    expect(argv).not.toContain("--privileged");
    expect(argv).not.toContain("-v");
  });

  test("rejects egress until the managed proxy is enabled", () => {
    const provider = new PodmanProvider();
    expectPodmanError(() => provider.buildRunArgv({ ...spec, network: "egress" }), "EGRESS_UNSUPPORTED", { network: "egress" });
  });

  test("rejects mounted engine sockets, invalid names and mutable image tags", async () => {
    const provider = new PodmanProvider({ executable: "nonexistent-podman-fixture" });
    for (const [source, target] of [["/var/run/docker.sock", "/var/run/docker.sock"], ["/tmp/Docker.Sock", "/x"], ["/tmp/in", "/run/docker.sock"]]) {
      expectPodmanError(() => provider.buildRunArgv({ ...spec, mounts: [{ source, target, readonly: true }] }), "SANDBOX_SPEC_INVALID");
    }
    expectPodmanError(() => provider.buildRunArgv({ ...spec, name: "bad name" }), "SANDBOX_SPEC_INVALID", { field: "name" });
    for (const image of ["python:latest", "mao-test/tool:dev", `repo@sha256:${"AB".repeat(32)}`]) {
      expectPodmanError(() => provider.buildRunArgv({ ...spec, image }), "IMAGE_REF_INVALID", { image });
    }
    await expect(provider.resolveImage("python:latest")).rejects.toMatchObject({ code: "IMAGE_REF_INVALID" });
    expect(provider.buildRunArgv({ ...spec, image: `sha256:${"cd".repeat(32)}` })).toContain(`sha256:${"cd".repeat(32)}`);
  });

  test("uses the managed egress network and proxy environment", () => {
    const provider = new PodmanProvider({
      egressNetwork: "mao-egress",
      egressEnv: { HTTPS_PROXY: "http://mao-egress:8080", NO_PROXY: "localhost" },
    });
    const argv = provider.buildRunArgv({ ...spec, network: "egress" });
    expect(argv[argv.indexOf("--network") + 1]).toBe("mao-egress");
    expect(argv).toContain("--env");
    expect(argv).toContain("HTTPS_PROXY=http://mao-egress:8080");
    expect(argv).toContain("NO_PROXY=localhost");
  });

  test("rejects unsafe managed network names", () => {
    expect(() => new PodmanProvider({ egressNetwork: "mao-egress;bad" })).toThrow(PodmanError);
  });

  test("rejects arbitrary egress environment injection", () => {
    expect(() => new PodmanProvider({ egressNetwork: "mao-egress", egressEnv: { FACTORY_SECRET: "value" } })).toThrow(PodmanError);
  });

  test("probes health without throwing when Podman is missing", async () => {
    const provider = new PodmanProvider({ runner: async () => ({ exitCode: 127, stdout: new Uint8Array(), stderr: new TextEncoder().encode("missing") }) });
    await expect(provider.health()).resolves.toMatchObject({ available: false, reason: "missing" });
  });

  test("requires a rootless Podman service on Linux", async () => {
    const rootful = new PodmanProvider({
      platform: "linux",
      runner: async () => ok(JSON.stringify({ host: { security: { rootless: false } } })),
    });
    await expect(rootful.health()).resolves.toMatchObject({ available: false, rootless: false, reason: "podman-rootless-required" });

    const unknown = new PodmanProvider({
      platform: "linux",
      runner: async () => ok(JSON.stringify({ host: {} })),
    });
    await expect(unknown.health()).resolves.toMatchObject({ available: false, rootless: null, reason: "podman-rootless-unknown" });
  });

  test("exec writes stdout and stderr through the provider", async () => {
    const calls: string[][] = [];
    const provider = new PodmanProvider({
      runner: async (argv) => {
        calls.push(argv);
        if (argv.includes("info")) return ok(JSON.stringify({ host: { security: { rootless: true } } }));
        if (argv.includes("inspect")) return ok(`sha256:${"b".repeat(64)}`);
        return ok("out", "err");
      },
    });
    const root = `tmp/podman-test-${Date.now()}`;
    await mkdir(root, { recursive: true });
    const sandbox = await provider.open({ ...spec, image: `registry.example/worker@sha256:${"b".repeat(64)}` });
    const result = await sandbox.exec(["echo", "ok"], { timeoutMs: 1000, stdout: `${root}/stdout`, stderr: `${root}/stderr` });
    expect(result.exitCode).toBe(0);
    expect(await Bun.file(`${root}/stdout`).text()).toBe("out");
    expect(await Bun.file(`${root}/stderr`).text()).toBe("err");
    expect(calls.some((argv) => argv.includes("run"))).toBe(true);
    await sandbox.close();
    await rm(root, { recursive: true, force: true });
  });

  test("real subprocess logs are readable during exec and survive a timeout", { timeout: 30_000 }, async () => {
    const root = `tmp/podman-stream-${randomUUID()}`;
    const paths = { stdout: `${root}/logs/stdout`, stderr: `${root}/logs/stderr` };
    await mkdir(`${root}/logs`, { recursive: true });
    await Bun.write(paths.stdout, "previous run output that must be truncated");
    const provider = new PodmanProvider({
      runner: (_argv, options) => runCommand([Bun.argv[0], "-e", "console.log('stdout ready'); console.error('stderr ready'); setInterval(() => {}, 1000)"], options),
    });
    let completed = false;
    const execution = provider.exec("test-sandbox", ["test"], { ...paths, timeoutMs: 12_000 })
      .finally(() => { completed = true; });
    try {
      const errorLog = Bun.file(paths.stderr);
      const deadline = performance.now() + 10_000;
      while (performance.now() < deadline && !(await errorLog.text()).includes("stderr ready")) {
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
      expect(completed).toBe(false);
      expect((await Bun.file(paths.stdout).text()).trim()).toBe("stdout ready");
      expect((await errorLog.text()).trim()).toBe("stderr ready");
      expect(await execution).toMatchObject({ exitCode: null, timedOut: true });
      expect((await Bun.file(paths.stdout).text()).trim()).toBe("stdout ready");
      expect(await errorLog.text()).toContain("stderr ready");
      expect(await errorLog.text()).toContain("Container runtime command timed out");
    } finally {
      await Promise.allSettled([execution]);
      await rm(root, { recursive: true, force: true });
    }
  });
});
