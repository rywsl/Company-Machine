import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { createConnection, createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { SandboxSpec } from "../runtime/podman";
import { compileEgress, matchesEgressAllowlist } from "../src/legacy/egress";
import { startEgressForwarder, type EgressForwarder } from "../src/legacy/egress-forwarder";
import { FactoryError } from "../src/legacy/errors";
import { LocalProvider } from "../src/legacy/sandbox";
import { loadSnapshot } from "../src/legacy/snapshot";

const station = "upper@1";
const stationKey = "Station:" + station;
const policyKey = "Policy:tools@1";
const machineKey = "Machine:toolbox@1";

function registry(egress: unknown, machineEgress: unknown = {}) {
  const specs: Record<string, unknown> = {
    [stationKey]: { policy: "tools@1", machine: "toolbox@1", monitors: { requests: { on: "egress" } } },
    [policyKey]: { egress },
    [machineKey]: { needs: { egress: machineEgress } },
  };
  return {
    key(kind: string, ref: string) { return ref.includes(":") ? ref : kind + ":" + ref; },
    spec(key: string) { return specs[key]; },
  };
}

function expectError(run: () => unknown, code: string) {
  try { run(); }
  catch (error) {
    expect(error).toBeInstanceOf(FactoryError);
    expect((error as FactoryError).code).toBe(code);
    return;
  }
  throw new Error("expected " + code);
}

describe("Bun Registry egress", () => {
  test("compiles the example Registry and matches exact HTTP(S) hosts", async () => {
    const snapshot = await loadSnapshot("spec/examples/registry");
    const plan = compileEgress(snapshot, "explainer@3");
    expect(plan).toMatchObject({ station: "Station:explainer@3", policy: "Policy:video-default@2", network: "egress" });
    expect(plan.allow).toEqual(["api.elevenlabs.io", "api.pexels.com", "fal.run"]
      .map((host) => ({ host, tier: "providers", methods: ["*"] })));
    expect(matchesEgressAllowlist(plan, "https://FAL.RUN.:8443/path", "POST")).toBe(true);
    expect(matchesEgressAllowlist(plan, "https://api.fal.run", "GET")).toBe(false);
    expect(matchesEgressAllowlist(plan, "https://user@fal.run", "GET")).toBe(false);
    expect(matchesEgressAllowlist(plan, "file://fal.run/path", "GET")).toBe(false);
  });

  test("leaves none isolated and expands only explicitly delegated Machine tiers", () => {
    const needs = { providers: ["allowed.example"], publish: ["unlisted.example"] };
    const none = compileEgress(registry({ none: true }, needs), station);
    expect(none).toMatchObject({ network: "none", allow: [] });
    expect(matchesEgressAllowlist(none, "https://allowed.example", "GET")).toBe(false);
    const delegated = compileEgress(registry({ providers: "from-machine" }, needs), station);
    expect(delegated.allow).toEqual([{ host: "allowed.example", tier: "providers", methods: ["*"] }]);
    expect(matchesEgressAllowlist(delegated, "https://allowed.example", "CONNECT")).toBe(true);
    expect(matchesEgressAllowlist(delegated, "https://unlisted.example", "POST")).toBe(false);
    expect(compileEgress(registry({ ingress: "from-machine" }, needs), station).network).toBe("none");
  });

  test("applies tier methods and explicit wildcard and ingress profile rules", () => {
    const config = {
      web_read: { hosts: ["https://Read.Example./path", "*.docs.example"], methods: ["head", "GET", "get"] },
      ingress: ["webhook"],
      publish: { hosts: ["upload.example"], methods: [] },
      on_unlisted: "ask",
    };
    const plan = compileEgress(registry(config), station, { webhook: ["hooks.example"] });
    expect(plan).toMatchObject({ monitored: true, onUnlisted: "ask" });
    expect(plan.allow).toContainEqual({ host: "read.example", tier: "web_read", methods: ["GET", "HEAD"] });
    expect(matchesEgressAllowlist(plan, "https://a.b.docs.example", "HEAD")).toBe(true);
    expect(matchesEgressAllowlist(plan, "https://docs.example", "HEAD")).toBe(false);
    expect(matchesEgressAllowlist(plan, "https://fakedocs.example", "HEAD")).toBe(false);
    expect(matchesEgressAllowlist(plan, "https://read.example", "CONNECT")).toBe(false);
    expect(matchesEgressAllowlist(plan, "https://hooks.example", "GET")).toBe(true);
    expect(matchesEgressAllowlist(plan, "https://hooks.example", "PATCH")).toBe(false);
    expect(matchesEgressAllowlist(plan, "https://upload.example", "PUT")).toBe(false);
    expect(matchesEgressAllowlist(plan, "https://unlisted.example", "GET")).toBe(false);
    const publicReads = compileEgress(registry({ web_read: true }), station);
    expect(matchesEgressAllowlist(publicReads, "https://unlisted.example", "GET")).toBe(true);
    expect(matchesEgressAllowlist(publicReads, "https://unlisted.example", "POST")).toBe(false);
  });

  test("rejects malformed policy, wildcard, method and profile declarations", () => {
    for (const config of [
      { none: true, publish: [] }, { on_unlisted: "allow" }, { providers: true },
      { publish: ["*.com"] }, { publish: ["a.*.example"] }, { publish: ["*.127.0.0.1"] },
      { publish: ["ftp://uploads.example"] }, { publish: ["user@uploads.example"] },
      { publish: { hosts: ["uploads.example"], methods: ["GET POST"] } },
    ]) {
      expectError(() => compileEgress(registry(config), station), "EGRESS_CONFIG_INVALID");
    }
    expectError(() => compileEgress(registry({ ingress: ["webhook"] }), station), "EGRESS_PROFILE_UNKNOWN");
    expectError(() => compileEgress(registry({ ingress: ["webhook"] }), station, { webhook: ["another-profile"] }), "EGRESS_CONFIG_INVALID");
  });
});

const localSpec = { name: "local-egress", image: "local", mounts: [] };

test("fails closed when LocalProvider has no matching plan or usable injected listener", async () => {
  const plan = { ...compileEgress(registry({ providers: ["allowed.example"] }), station), monitored: false };
  const spec = { ...localSpec, network: "egress", egressPlan: plan } satisfies SandboxSpec;
  await expect(new LocalProvider().open({ ...localSpec, network: "egress" })).rejects.toMatchObject({ code: "EGRESS_CONFIG_INVALID" });
  await expect(new LocalProvider().open(spec)).rejects.toMatchObject({ code: "EGRESS_UNSUPPORTED" });
  let started = 0;
  let closed = 0;
  const provider = new LocalProvider({}, async () => {
    started++;
    return { authorization: "", endpoint: { host: "127.0.0.1", port: 22 }, close: async () => { closed++; } };
  });
  await (await provider.open({ ...localSpec, network: "none" })).close();
  expect(started).toBe(0);
  await expect(provider.open(spec)).rejects.toMatchObject({ code: "EGRESS_PROXY_UNAVAILABLE" });
  expect(started).toBe(1);
  expect(closed).toBe(1);
});

test("injects the managed local proxy into a process and revokes the listener on close", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mao-local-egress-"));
  const upstream = createServer((socket) => socket.end("upstream-witness"));
  try {
    await new Promise<void>((done) => upstream.listen({ host: "127.0.0.1", port: 0 }, done));
    const address = upstream.address();
    if (!address || typeof address === "string") throw new Error("Missing fixture address");
    let forwarder: EgressForwarder | undefined;
    const provider = new LocalProvider({}, async (plan) => {
      forwarder = await startEgressForwarder({
        plan, resolve: async () => ["1.1.1.1"],
        route: () => ({ address: "127.0.0.1", port: address.port }),
      });
      return forwarder;
    });
    const plan = { ...compileEgress(registry({ providers: ["allowed.example"] }), station), monitored: false };
    const sandbox = await provider.open({
      ...localSpec, network: "egress", egressPlan: plan, env: { HTTP_PROXY: "configured", NO_PROXY: "*" },
    });
    try {
      const stdout = join(directory, "stdout");
      const stderr = join(directory, "stderr");
      const result = await sandbox.exec([Bun.argv[0], "-e", [
        'import { createConnection } from "node:net";',
        'const proxy = new URL(process.env.HTTPS_PROXY);',
        'if (process.env.http_proxy !== process.env.HTTPS_PROXY || process.env.NO_PROXY !== "") process.exit(2);',
        'const socket = createConnection({ host: proxy.hostname, port: Number(proxy.port) });',
        'const authority = "allowed.example:443";',
        'const auth = Buffer.from(proxy.username + ":" + proxy.password).toString("base64");',
        'socket.once("connect", () => socket.write("CONNECT " + authority + " HTTP/1.1\\r\\nHost: " + authority + "\\r\\nProxy-Authorization: Basic " + auth + "\\r\\n\\r\\n"));',
        'let result = ""; socket.on("data", (data) => { result += data.toString(); });',
        'socket.once("end", () => { if (result.includes("200 Connection Established") && result.includes("upstream-witness")) console.log("allowed"); else process.exit(1); });',
      ].join("\n")], { stdout, stderr, timeoutMs: 3_000, env: { https_proxy: "override", no_proxy: "*" } });
      expect(result.exitCode).toBe(0);
      expect((await Bun.file(stdout).text()).trim()).toBe("allowed");
      expect(await Bun.file(stderr).text()).toBe("");
    } finally { await Promise.all([sandbox.close(), sandbox.close()]); }
    const endpoint = forwarder?.endpoint;
    if (!endpoint || !("port" in endpoint)) throw new Error("Missing forwarder endpoint");
    await expect(new Promise((resolve, reject) => {
      const connection = createConnection(endpoint);
      connection.once("error", reject);
      connection.once("connect", () => { connection.destroy(); resolve(undefined); });
    })).rejects.toBeDefined();
  } finally {
    upstream.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
