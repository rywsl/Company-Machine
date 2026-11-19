import { describe, expect, test } from "bun:test";
import { mkdir, rm } from "node:fs/promises";
import { PodmanError, PodmanProvider, type CommandResult, type SandboxSpec } from "./podman";

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
    expect(() => provider.buildRunArgv({ ...spec, network: "egress" })).toThrow(PodmanError);
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

  test("probes health without throwing when Podman is missing", async () => {
    const provider = new PodmanProvider({ runner: async () => ({ exitCode: 127, stdout: new Uint8Array(), stderr: new TextEncoder().encode("missing") }) });
    await expect(provider.health()).resolves.toMatchObject({ available: false, reason: "missing" });
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
});
