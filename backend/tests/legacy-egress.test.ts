import { describe, expect, test } from "bun:test";
import { compileEgress, matchesEgressAllowlist } from "../src/legacy/egress";
import { FactoryError } from "../src/legacy/errors";
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
