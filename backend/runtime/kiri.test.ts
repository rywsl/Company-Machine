import { describe, expect, test } from "bun:test";
import { KiriProvider, resolveKiriEnvironment } from "./kiri";
import type { CommandResult, SandboxSpec } from "./podman";

const ok = (stdout = "", stderr = ""): CommandResult => ({
  exitCode: 0,
  stdout: new TextEncoder().encode(stdout),
  stderr: new TextEncoder().encode(stderr),
});

const spec: SandboxSpec = {
  name: "mao-kiri-1",
  image: `registry.example/worker@sha256:${"a".repeat(64)}`,
  mounts: [],
  network: "none",
};

describe("KiriProvider", () => {
  test("routes hardened container argv through the embedded Kiri CLI", () => {
    const provider = new KiriProvider({ kiriExecutable: "kiri-test" });
    const argv = provider.buildRunArgv(spec);
    expect(argv.slice(0, 2)).toEqual(["kiri-test", "run"]);
    expect(argv).toContain("--cap-drop");
    expect(argv).toContain("ALL");
    expect(argv).toContain("--init");
    expect(argv).toContain("--pids-limit");
    expect(argv[argv.indexOf("--pids-limit") + 1]).toBe("256");
    expect(argv).toContain("--tmpfs");
    expect(argv[argv.indexOf("--tmpfs") + 1]).toBe("/tmp:rw,nosuid,size=256m");
    expect(argv[argv.indexOf("--network") + 1]).toBe("none");
    expect(argv.join(" ")).not.toContain("docker.sock");
  });

  test("supports Kiri installations exposed as a Docker context", () => {
    const provider = new KiriProvider({ dockerExecutable: "docker-test", context: "kiri-test", mode: "docker-context" });
    expect(provider.buildRunArgv(spec).slice(0, 4)).toEqual(["docker-test", "--context", "kiri-test", "run"]);
  });

  test("probes the embedded Kiri CLI directly", async () => {
    const calls: string[][] = [];
    const provider = new KiriProvider({
      kiriExecutable: "kiri-test",
      runner: async (argv) => { calls.push(argv); return ok("[]"); },
    });
    const health = await provider.health();
    expect(health.available).toBe(true);
    expect(calls[0]).toEqual(["kiri-test", "ps", "--format", "json"]);
  });

  test("passes the Linux headless socket and data directory to Kiri", async () => {
    let commandOptions: { env?: Record<string, string> } | undefined;
    const provider = new KiriProvider({
      kiriExecutable: "kiri-test",
      socket: "/run/kiri/docker.sock",
      dataDir: "/var/lib/kiri-daemon",
      runner: async (_argv, options) => { commandOptions = options; return ok("[]"); },
    });
    await provider.health();
    expect(commandOptions?.env).toEqual({ KIRI_SOCKET: "/run/kiri/docker.sock", KIRI_DATA_DIR: "/var/lib/kiri-daemon" });
    expect(provider.kiriEnvironment).toEqual(commandOptions?.env);
  });

  test("rejects empty or nul-containing headless paths", () => {
    expect(() => resolveKiriEnvironment({ socket: "" })).toThrow("Invalid Kiri socket");
    expect(() => resolveKiriEnvironment({ dataDir: "bad\u0000path" })).toThrow("Invalid Kiri data directory");
  });

  test("reports the Kiri context while using the Docker Engine API", async () => {
    const calls: string[][] = [];
    const provider = new KiriProvider({
      dockerExecutable: "docker-test",
      context: "kiri-test",
      mode: "docker-context",
      runner: async (argv) => {
        calls.push(argv);
        return ok(JSON.stringify({ ServerVersion: "kiri-test", SecurityOptions: [] }));
      },
    });
    const health = await provider.health();
    expect(health.available).toBe(true);
    expect(health.machine).toBe("kiri-test");
    expect(health.executable).toBe("docker-test --context kiri-test");
    expect(calls[0]).toEqual(["docker-test", "--context", "kiri-test", "info", "--format", "json"]);
  });

  test("rejects malformed context names", () => {
    expect(() => new KiriProvider({ context: "kiri;host" })).toThrow("Invalid Kiri Docker context");
  });
});

