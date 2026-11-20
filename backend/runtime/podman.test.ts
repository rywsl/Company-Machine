import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { mkdir, rm } from "node:fs/promises";
import { FakeClock } from "../src/legacy/foundation";
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
    const flag = (name: string) => argv[argv.indexOf(name) + 1];
    expect(flag("--user")).toBe("10001:10001");
    expect(flag("--cap-drop")).toBe("ALL");
    expect(argv).toContain("--read-only");
    expect(flag("--security-opt")).toBe("no-new-privileges");
    expect(flag("--pids-limit")).toBe("256");
    expect(flag("--network")).toBe("bridge");
    expect(argv.filter((arg) => arg === "--network")).toHaveLength(1);
    expect(argv.join(" ").toLowerCase()).not.toContain("docker.sock");
    for (const absent of ["host", "--privileged", "--cap-add", "-v", "--volume", "--rm"]) expect(argv).not.toContain(absent);
  });

  test("emits the ordered Podman run contract and protects fixed environment entries", () => {
    const provider = new PodmanProvider({ executable: "podman-test" });
    const local = { ...spec,
      mounts: [{ source: "/tmp/fixture/in", target: "/factory/in", readonly: true }, { source: "/tmp/fixture/out", target: "/factory/out", readonly: false }],
      env: { FACTORY_JOB_ID: "job-1", B_EXTRA: "2", A_EXTRA: "1" }, memoryMb: 256,
    };
    expect(provider.buildRunArgv(local)).toEqual([
      "podman-test", "run", "--name", "mao-job-1-1", "--detach", "--init", "--user", "10001:10001",
      "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--pids-limit", "256",
      "--network", "none", "--tmpfs", "/tmp:rw,nosuid,size=256m", "--cpus", "1", "--memory", "256m",
      "--mount", "type=bind,source=/tmp/fixture/in,target=/factory/in,readonly",
      "--mount", "type=bind,source=/tmp/fixture/out,target=/factory/out",
      "--mount", "type=volume,source=workspace-1,target=/workspace",
      "--env", "FACTORY_IN=/factory/in", "--env", "FACTORY_OUT=/factory/out",
      "--env", "FACTORY_CTL=/factory/ctl", "--env", "FACTORY_WORKSPACE=/workspace",
      "--env", "FACTORYCTL=/opt/factory/bin/factoryctl", "--env", "HOME=/tmp/home",
      "--env", "PATH=/opt/factory/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
      "--env", "A_EXTRA=1", "--env", "B_EXTRA=2", "--env", "FACTORY_JOB_ID=job-1",
      "--workdir", "/workspace", spec.image, "/bin/sh", "-c", "mkdir -p /tmp/home && exec sleep 2147483647",
    ]);
    const attempted = provider.buildRunArgv({ ...local, env: { FACTORY_IN: "/etc" } });
    expect(attempted).not.toContain("FACTORY_IN=/etc");
    expect(attempted.filter((arg) => arg === "FACTORY_IN=/factory/in")).toHaveLength(1);
  });

  test("rejects egress until the managed proxy is enabled", () => {
    const provider = new PodmanProvider();
    expectPodmanError(() => provider.buildRunArgv({ ...spec, network: "egress" }), "EGRESS_UNSUPPORTED", { network: "egress" });
  });

  test("rejects Registry egress before querying an engine with only a configured bridge", async () => {
    const provider = new PodmanProvider({
      egressNetwork: "mao-egress", relayImage: "", runner: async () => { throw new Error("unexpected engine call"); },
    });
    await expect(provider.open({
      ...spec, network: "egress",
      egressPlan: {
        station: "Station:runtime-fixture@1", policy: "Policy:runtime-fixture@1", network: "egress",
        allow: [{ host: "allowed.example", tier: "providers", methods: ["*"] }], monitored: false, onUnlisted: "deny",
      },
    })).rejects.toMatchObject({ code: "EGRESS_UNSUPPORTED" });
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

  test("reuses a locally available digest-pinned image without pulling", async () => {
    const calls: string[][] = [];
    const image = `registry.example/worker@sha256:${"c".repeat(64)}`;
    const provider = new PodmanProvider({ executable: "podman-test", runner: async (argv) => {
      calls.push(argv);
      return ok(`sha256:${"d".repeat(64)}`);
    } });

    await expect(provider.ensureImage(image)).resolves.toBe(image);
    expect(calls).toEqual([["podman-test", "image", "inspect", "--format", "{{.Id}}", image]]);
  });

  test("pulls a missing digest-pinned image and verifies it is inspectable", async () => {
    const calls: string[][] = [];
    const image = `registry.example/worker@sha256:${"e".repeat(64)}`;
    const provider = new PodmanProvider({ executable: "podman-test", runner: async (argv) => {
      calls.push(argv);
      if (argv[1] === "image" && calls.filter((entry) => entry[1] === "image").length <= 2) return { ...ok("", "missing"), exitCode: 1 };
      if (argv[1] === "pull") return ok("pulled");
      return ok(`sha256:${"e".repeat(64)}`);
    } });

    await expect(provider.ensureImage(image)).resolves.toBe(image);
    expect(calls.map((argv) => argv.slice(1, 3))).toEqual([["image", "inspect"], ["image", "inspect"], ["pull", image], ["image", "inspect"]]);
  });

  test("exec writes stdout and stderr through the provider", async () => {
    const calls: string[][] = [];
    const clock = new FakeClock();
    const provider = new PodmanProvider({
      clock,
      runner: async (argv) => {
        calls.push(argv);
        if (argv.includes("info")) return ok(JSON.stringify({ host: { security: { rootless: true } } }));
        if (argv.includes("inspect")) return ok(`sha256:${"b".repeat(64)}`);
        if (argv.includes("exec")) clock.advance(0.125);
        return ok("out", "err");
      },
    });
    const root = `tmp/podman-test-${Date.now()}`;
    await mkdir(root, { recursive: true });
    const sandbox = await provider.open({ ...spec, image: `registry.example/worker@sha256:${"b".repeat(64)}` });
    const result = await sandbox.exec(["echo", "ok"], { timeoutMs: 1000, stdout: `${root}/stdout`, stderr: `${root}/stderr` });
    expect(result.exitCode).toBe(0);
    expect(result.durationMs).toBe(125);
    expect(await Bun.file(`${root}/stdout`).text()).toBe("out");
    expect(await Bun.file(`${root}/stderr`).text()).toBe("err");
    expect(calls.some((argv) => argv.includes("run"))).toBe(true);
    await sandbox.close();
    await rm(root, { recursive: true, force: true });
  });

  test("streams subprocess logs and removes the sandbox before returning a timeout", { timeout: 30_000 }, async () => {
    const root = `tmp/podman-stream-${randomUUID()}`;
    const paths = { stdout: `${root}/logs/stdout`, stderr: `${root}/logs/stderr` };
    await mkdir(`${root}/logs`, { recursive: true });
    await Bun.write(paths.stdout, "previous run output that must be truncated");
    const calls: string[][] = [];
    const provider = new PodmanProvider({ executable: "podman-test", runner: async (argv, options) => {
      calls.push(argv.slice(1));
      if (argv[1] === "info") return ok(JSON.stringify({ host: { security: { rootless: true } } }));
      if (argv[1] === "image") return ok(`sha256:${"a".repeat(64)}`);
      if (argv[1] === "exec") return runCommand([Bun.argv[0], "-e", "console.log('stdout ready'); console.error('stderr ready'); setInterval(() => {}, 1000)"], options);
      return ok();
    } });
    const sandbox = await provider.open(spec);
    calls.length = 0;
    let completed = false;
    const execution = sandbox.exec(["test"], { ...paths, timeoutMs: 12_000 })
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
      await expect(sandbox.exec(["test"], { ...paths, timeoutMs: 100 })).rejects.toMatchObject({ code: "SANDBOX_CLOSED" });
      await sandbox.close();
      expect(calls).toEqual([["exec", spec.name, "test"], ["rm", "-f", spec.name]]);
    } finally {
      await Promise.allSettled([execution]);
      await sandbox.close();
      await rm(root, { recursive: true, force: true });
    }
  });

  test("keeps a sandbox closed when timeout cleanup has to be retried", async () => {
    const root = `tmp/podman-cleanup-${randomUUID()}`;
    const paths = { stdout: `${root}/stdout`, stderr: `${root}/stderr`, timeoutMs: 100 };
    await mkdir(root, { recursive: true });
    let cleanupAttempts = 0;
    const provider = new PodmanProvider({ executable: "podman-test", runner: async (argv) => {
      if (argv[1] === "info") return ok(JSON.stringify({ host: { security: { rootless: true } } }));
      if (argv[1] === "image") return ok(`sha256:${"a".repeat(64)}`);
      if (argv[1] === "exec") throw new PodmanError("PODMAN_TIMEOUT", "Command timed out");
      if (argv[1] === "rm" && ++cleanupAttempts === 2) return { ...ok("", "engine unavailable"), exitCode: 1 };
      return ok();
    } });
    const sandbox = await provider.open(spec);
    try {
      expect(await sandbox.exec(["test"], paths)).toMatchObject({ exitCode: null, timedOut: true });
      expect(await Bun.file(paths.stderr).text()).toContain("Sandbox cleanup failed");
      await expect(sandbox.exec(["test"], paths)).rejects.toMatchObject({ code: "SANDBOX_CLOSED" });
      await Promise.all([sandbox.close(), sandbox.close()]);
      expect(cleanupAttempts).toBe(3);
    } finally {
      await sandbox.close();
      await rm(root, { recursive: true, force: true });
    }
  });
});
