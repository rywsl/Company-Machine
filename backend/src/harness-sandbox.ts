import { randomUUID } from "node:crypto";
import { launchEgressRelay } from "../runtime/egress-relay";
import { resolveKiriClient } from "../runtime/kiri";
import { resolvePodmanExecutable, runCommand } from "../runtime/podman";
import { selectedContainerProvider } from "../runtime/selected";
import type { EgressPlan } from "./legacy/egress";
import type { startEgressForwarder } from "./legacy/egress-forwarder";

const domain = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;

export function harnessEgressPlan(baseUrl: string, configured: string): EgressPlan {
  const allowed = new Set<string>();
  if (baseUrl) {
    const base = new URL(baseUrl);
    if ((base.protocol !== "https:" && base.protocol !== "http:") || base.username || base.password || base.port) {
      throw new Error("Harness egress Base URL must use a standard HTTP(S) port without user information");
    }
    allowed.add(base.hostname.replace(/\.$/, ""));
  }
  for (const value of configured.split(",")) {
    const host = value.trim().toLowerCase().replace(/\.$/, "");
    if (host) allowed.add(host);
  }
  if (allowed.size === 0 || [...allowed].some((host) => host.length > 253 || !domain.test(host))) {
    throw new Error("Harness egress requires an exact Base URL host or MAO_HARNESS_EGRESS_HOSTS");
  }
  return {
    station: "harness", policy: "harness", network: "egress", monitored: false, onUnlisted: "deny",
    allow: [...allowed].sort().map((host) => ({ tier: "providers", host, methods: ["*"] })),
  };
}

export async function prepareHarnessEgress(image: string, baseUrl = "", startForwarder?: typeof startEgressForwarder) {
  const env = process.env;
  const kind = env.MAO_CONTAINER_RUNTIME?.toLowerCase() ?? "podman";
  const kiri = kind === "kiri" ? resolveKiriClient() : undefined;
  const relayImage = env.MAO_EGRESS_RELAY_IMAGE;
  if (process.platform !== "linux" || !relayImage
      || (kiri ? kiri.mode !== "linux-socket" : kind !== "podman" || env.CONTAINER_HOST || env.CONTAINER_CONNECTION)) {
    throw new Error("Harness egress requires a local Linux runtime and a pinned MAO_EGRESS_RELAY_IMAGE");
  }
  const plan = harnessEgressPlan(baseUrl, env.MAO_HARNESS_EGRESS_HOSTS ?? "");
  const provider = selectedContainerProvider(kind);
  const ready = await provider.ensureReady();
  if (!ready.available) throw new Error(ready.reason ?? "Harness container runtime unavailable");
  const [resolved, resolvedRelay] = await Promise.all([provider.resolveImage(image), provider.resolveImage(relayImage)]);
  const prefix = kiri?.argv ?? [resolvePodmanExecutable()];
  const command = (...args: string[]) => [...prefix, ...args];
  const run = (argv: string[], options?: Parameters<typeof runCommand>[1]) =>
    runCommand(argv, { ...options, env: { ...options?.env, ...kiri?.env } });
  const name = "mao-harness-" + randomUUID();
  const relay = await launchEgressRelay({ name, image: resolvedRelay, plan, command, run, startForwarder });
  let closing: Promise<void> | undefined;
  return {
    ...relay, name, image: resolved,
    close: () => closing ??= (async () => {
      try { await run(command("rm", "-f", name), { timeoutMs: 15_000 }); }
      finally { await relay.close(); }
    })(),
  };
}
