import { describe, expect, test } from "bun:test";
import { KiriProvider, buildKiriHarnessCommand, resolveKiriClient } from "./kiri";
import type { CommandResult, SandboxSpec } from "./podman";

function result(stdout = "", stderr = "", exitCode = 0): CommandResult {
  const encoder = new TextEncoder();
  return { exitCode, stdout: encoder.encode(stdout), stderr: encoder.encode(stderr) };
}

const spec: SandboxSpec = {
  name: "mao-kiri-1",
  image: `registry.example/worker@sha256:${"a".repeat(64)}`,
  mounts: [],
  network: "none",
};

describe("KiriProvider", () => {
  test("defaults to the packaged service socket on Linux", () => {
    const keys = ["MAO_KIRI_MODE", "MAO_KIRI_SOCKET", "KIRI_SOCKET"];
    const saved = keys.map((key) => process.env[key]);
    try {
      for (const key of keys) delete process.env[key];
      const client = resolveKiriClient({ platform: "linux", dockerExecutable: "docker-test" });
      expect(client.argv).toEqual(["docker-test", "--host", "unix:///run/kiri/docker.sock"]);
      expect(client.managedService).toBe(true);
    } finally {
      keys.forEach((key, index) => {
        const value = saved[index];
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      });
    }
  });

  test("routes Linux sandbox and Harness commands to the official Unix socket", async () => {
    const options = { platform: "linux", mode: "linux-socket", socket: "/run/kiri/docker.sock", dockerExecutable: "docker-test" } as const;
    const prefix = ["docker-test", "--host", "unix:///run/kiri/docker.sock"];
    const calls: string[][] = [];
    const provider = new KiriProvider({ ...options, runner: async (argv) => {
      calls.push(argv);
      return argv[3] === "info" ? result("Kiri\n") : result(`sha256:${"a".repeat(64)}`);
    } });

    expect(await provider.health()).toMatchObject({ available: true, platform: "linux", machine: "linux-host" });
    expect(calls[0]).toEqual([...prefix, "info", "--format", "{{.OperatingSystem}}"]);
    expect(await provider.resolveImage(spec.image)).toBe(spec.image);
    expect(calls[1]).toEqual([...prefix, "image", "inspect", "--format", "{{.Id}}", spec.image]);

    const run = provider.buildRunArgv(spec);
    const harness = buildKiriHarnessCommand(spec.image, ["codex"], "none", options);
    for (const argv of [run, harness.argv]) {
      expect(argv.slice(0, 4)).toEqual([...prefix, "run"]);
      expect(argv[argv.indexOf("--user") + 1]).toBe("10001:10001");
      expect(argv[argv.indexOf("--cap-drop") + 1]).toBe("ALL");
      expect(argv[argv.indexOf("--pids-limit") + 1]).toBe("256");
      expect(argv[argv.indexOf("--network") + 1]).toBe("none");
      expect(argv).toContain("--init");
      expect(argv).toContain("--read-only");
      expect(argv).not.toContain("--mount");
    }
    expect(harness.env.DOCKER_CONTEXT).toBe("");
  });

  test("reuses the running service and shares one systemd start for concurrent readiness", async () => {
    const calls: string[][] = [];
    let healthy = false;
    const provider = new KiriProvider({
      platform: "linux",
      mode: "linux-socket",
      socket: "/run/kiri/docker.sock",
      dockerExecutable: "docker-test",
      runner: async (argv) => {
        calls.push(argv);
        if (argv[0] === "systemctl") { healthy = true; return result(); }
        return healthy ? result("Kiri") : result("", "offline", 1);
      },
    });
    const probes = await Promise.all([provider.ensureReady(), provider.ensureReady()]);
    expect(probes.every((health) => health.available)).toBe(true);
    expect((await provider.ensureReady()).available).toBe(true);
    expect(calls.filter((argv) => argv[0] === "systemctl")).toEqual([
      ["systemctl", "--no-ask-password", "start", "kiri.service"],
    ]);
  });

  test("reports systemd failures and leaves custom sockets to their supervisor", async () => {
    const calls: string[][] = [];
    const runner = async (argv: string[]) => {
      calls.push(argv);
      return argv[0] === "systemctl" ? result("", "Interactive authentication required", 1) : result("", "connection refused", 1);
    };
    const managed = new KiriProvider({ platform: "linux", mode: "linux-socket", socket: "/run/kiri/docker.sock", runner });
    expect((await managed.ensureReady()).reason).toBe("connection refused. Interactive authentication required");
    const custom = new KiriProvider({ platform: "linux", mode: "linux-socket", socket: "/tmp/kiri.sock", runner });
    expect((await custom.ensureReady()).reason).toBe("connection refused");
    expect(calls.filter((argv) => argv[0] === "systemctl")).toHaveLength(1);
    expect(calls.at(-1)?.slice(1, 3)).toEqual(["--host", "unix:///tmp/kiri.sock"]);
  });

  test("reports packaged socket access failure without requesting a systemd start", async () => {
    const calls: string[][] = [];
    const provider = new KiriProvider({
      platform: "linux", mode: "linux-socket", socket: "/run/kiri/docker.sock",
      runner: async (argv) => {
        calls.push(argv);
        return result("", "permission denied while trying to connect to the docker API", 1);
      },
    });
    const health = await provider.health();
    expect(health.available).toBe(false);
    expect(health.reason).toContain('sudo kiri host-access "$USER"');
    expect(await provider.ensureReady()).toEqual(health);
    expect(calls.map((argv) => argv[3])).toEqual(["info", "info"]);
  });

  test("does not request a systemd start when the default endpoint identifies another engine", async () => {
    const calls: string[][] = [];
    const provider = new KiriProvider({
      platform: "linux", mode: "linux-socket", socket: "/run/kiri/docker.sock",
      runner: async (argv) => { calls.push(argv); return result("Docker"); },
    });
    expect(await provider.ensureReady()).toMatchObject({ available: false, reason: "The configured endpoint did not identify itself as Kiri" });
    expect(calls.map((argv) => argv[3])).toEqual(["info"]);
  });

  test("retains the official CLI and named-context modes", async () => {
    const cli = resolveKiriClient({ platform: "linux", mode: "kiri", kiriExecutable: "kiri-test", socket: "/tmp/kiri.sock" });
    expect(cli.argv).toEqual(["kiri-test"]);
    expect(cli.env.KIRI_SOCKET).toBe("/tmp/kiri.sock");
    const provider = new KiriProvider({ mode: "docker-context", context: "kiri-test", dockerExecutable: "docker-test", runner: async () => result("Docker Desktop") });
    expect(provider.buildRunArgv(spec).slice(0, 4)).toEqual(["docker-test", "--context", "kiri-test", "run"]);
    expect((await provider.health()).available).toBe(false);
  });

  test("rejects invalid endpoints and the host network", () => {
    expect(() => resolveKiriClient({ platform: "linux", mode: "linux-socket", socket: "relative.sock" })).toThrow("Invalid Kiri socket");
    expect(() => resolveKiriClient({ mode: "docker-context", context: "kiri;host" })).toThrow("Invalid Kiri Docker context");
    expect(() => buildKiriHarnessCommand(spec.image, ["codex"], "egress", { egressNetwork: "host" })).toThrow("Managed Kiri egress network");
    expect(() => new KiriProvider({ egressNetwork: "host" })).toThrow("Invalid managed egress network");
  });
});
