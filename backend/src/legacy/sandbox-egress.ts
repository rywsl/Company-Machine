import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { SandboxSpec } from "../../runtime/podman";
import type { EgressForwarder } from "./egress-forwarder";
import { compileEgress, type EgressPlan } from "./egress";
import { FactoryError } from "./errors";
import type { Snapshot } from "./snapshot";

export type StartSandboxForwarder = (plan: EgressPlan) => Promise<EgressForwarder>;

export type SandboxEgress = {
  env: (environment: Record<string, string>) => Record<string, string>;
  close: () => Promise<void>;
};

export function stationNetwork(root: string, snapshot: Snapshot, station: string): Pick<SandboxSpec, "network" | "egressPlan"> {
  let profiles: Record<string, unknown> = {};
  try {
    const configured: unknown = JSON.parse(readFileSync(join(root, "egress", "profiles.json"), "utf8"));
    if (!configured || typeof configured !== "object" || Array.isArray(configured)) throw new Error("Expected a profile mapping");
    profiles = configured as Record<string, unknown>;
  } catch (error) {
    if ((error as { code?: unknown }).code !== "ENOENT") {
      throw new FactoryError("EGRESS_CONFIG_INVALID", "出网档案配置不合法", { field: "egress/profiles.json" });
    }
  }
  const plan = compileEgress(snapshot, station, profiles);
  return plan.network === "none" ? { network: "none" } : { network: "egress", egressPlan: plan };
}

export function egressPlanFor(spec: Pick<SandboxSpec, "name" | "network" | "egressPlan">): EgressPlan | undefined {
  if (spec.network !== "egress" && !spec.egressPlan) return;
  if (spec.network !== "egress" || spec.egressPlan?.network !== "egress") {
    throw new FactoryError("EGRESS_CONFIG_INVALID", "沙箱的出网模式与策略不一致", { name: spec.name });
  }
  return spec.egressPlan;
}

export function bindSandboxEgress(forwarder: EgressForwarder, port: number): SandboxEgress {
  const encoded = /^Basic ([A-Za-z0-9+/]+={0,2})$/.exec(forwarder.authorization)?.[1];
  const identity = encoded && /^mao:([0-9a-f]{64})$/.exec(Buffer.from(encoded, "base64").toString("utf8"));
  if (!identity || !Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Invalid managed proxy endpoint");
  const proxy = "http://mao:" + identity[1] + "@127.0.0.1:" + port;
  const managed = { HTTP_PROXY: proxy, HTTPS_PROXY: proxy, ALL_PROXY: proxy, NO_PROXY: "" };
  const variants = Object.fromEntries(Object.entries(managed).map(([key, value]) => [key.toLowerCase(), value]));
  let closing: Promise<void> | undefined;
  return {
    env(environment) {
      const supplied = Object.entries(environment).filter(([key]) => !Object.hasOwn(managed, key.toUpperCase()));
      return { ...Object.fromEntries(supplied), ...managed, ...variants };
    },
    close: () => closing ??= forwarder.close(),
  };
}

export async function startLocalSandboxEgress(
  spec: Pick<SandboxSpec, "name" | "network" | "egressPlan">,
  start?: StartSandboxForwarder,
): Promise<SandboxEgress | undefined> {
  const plan = egressPlanFor(spec);
  if (!plan) return;
  if (!start) throw new FactoryError("EGRESS_UNSUPPORTED", "本地测试沙箱需要显式的出网代理", { name: spec.name });
  let forwarder: EgressForwarder | undefined;
  try {
    forwarder = await start(plan);
    if (!("port" in forwarder.endpoint) || forwarder.endpoint.host !== "127.0.0.1") throw new Error("Missing loopback listener");
    return bindSandboxEgress(forwarder, forwarder.endpoint.port);
  } catch {
    await forwarder?.close().catch(() => {});
    throw new FactoryError("EGRESS_PROXY_UNAVAILABLE", "本地出网代理不可用", { name: spec.name });
  }
}
