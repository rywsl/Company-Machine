import { FactoryError } from "./errors";
import type { Snapshot } from "./snapshot";

const tiers = ["providers", "web_read", "publish", "ingress"] as const;
export type EgressTier = typeof tiers[number];
type PolicyRecord = Record<string, unknown>;

export interface EgressRule {
  readonly tier: EgressTier;
  readonly host: string;
  readonly methods: readonly string[];
}

export interface EgressPlan {
  readonly station: string;
  readonly policy: string;
  readonly network: "none" | "egress";
  readonly allow: readonly EgressRule[];
  readonly onUnlisted: "deny" | "ask";
  readonly monitored: boolean;
}

const defaultMethods: Record<EgressTier, readonly string[]> = {
  providers: ["*"],
  web_read: ["GET", "HEAD"],
  publish: ["DELETE", "GET", "HEAD", "POST", "PUT"],
  ingress: ["GET", "HEAD"],
};
const policyKeys = new Set<string>(["none", "on_unlisted", ...tiers]);
const dnsLabel = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const profileName = /^[a-z][a-z0-9-]*$/;
const ipv4 = /^\d{1,3}(?:\.\d{1,3}){3}$/;
const originScheme = /^[a-z][a-z0-9+.-]*:\/\//i;
const httpToken = /^[!#$%&'*+.^_\x60|~0-9A-Za-z-]+$/;
const invalidUrlText = /[\s\\\u0000-\u001f\u007f]/;

function object(value: unknown): value is PolicyRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hostname(url: URL): string | undefined {
  if ((url.protocol !== "https:" && url.protocol !== "http:") || url.username || url.password) return undefined;
  const raw = url.hostname.toLowerCase();
  if (raw.startsWith("[") && raw.endsWith("]")) return raw.slice(1, -1);
  return raw.endsWith(".") ? raw.slice(0, -1) : raw;
}

function policyHost(source: string): string | undefined {
  if (!source || invalidUrlText.test(source) || source.startsWith("/")) return undefined;
  const hasScheme = originScheme.test(source);
  const unbracketedIp = !hasScheme && !source.startsWith("[") && source.indexOf(":") !== source.lastIndexOf(":");
  const address = unbracketedIp ? "[" + source + "]" : source;
  try {
    const parsed = hostname(new URL(hasScheme ? address : "http://" + address));
    if (!parsed || parsed.includes(":")) return parsed;
    const wildcard = parsed.startsWith("*.");
    const name = wildcard ? parsed.slice(2) : parsed;
    const labels = name.split(".");
    if (wildcard && ipv4.test(name)) return undefined;
    return name.length <= 253 && labels.length >= 2 && labels.every((label) => dnsLabel.test(label)) ? parsed : undefined;
  } catch { return undefined; }
}

function matchesHost(pattern: string, host: string): boolean {
  return pattern === "*" || pattern === host
    || (pattern.startsWith("*.") && host.endsWith(pattern.slice(1)));
}

export function compileEgress(
  snapshot: Pick<Snapshot, "key" | "spec">,
  stationRef: string,
  profiles: Readonly<Record<string, unknown>> = {},
): EgressPlan {
  const station = snapshot.key("Station", stationRef);
  const spec = snapshot.spec(station);
  const policy = snapshot.key("Policy", spec.policy);
  const rawSettings: unknown = snapshot.spec(policy).egress;
  const settings = rawSettings === undefined ? {} : rawSettings;
  const invalid = (field: string): never => {
    throw new FactoryError("EGRESS_CONFIG_INVALID", "出网配置不合法", { station, policy, field });
  };
  if (!object(settings)) return invalid("egress");
  if (Object.keys(settings).some((key) => !policyKeys.has(key))) return invalid("egress");
  if (Object.hasOwn(settings, "none") && (settings.none !== true || Object.keys(settings).length !== 1)) return invalid("egress.none");
  const onUnlisted = Object.hasOwn(settings, "on_unlisted") ? settings.on_unlisted : "deny";
  if (onUnlisted !== "deny" && onUnlisted !== "ask") return invalid("egress.on_unlisted");

  const allow: EgressRule[] = [];
  let machine: PolicyRecord | undefined;
  for (const tier of tiers) {
    if (!Object.hasOwn(settings, tier)) continue;
    const declaration = settings[tier];
    const field = "egress." + tier;
    let hosts: unknown;
    let methods = defaultMethods[tier];
    const anyHost = declaration === true && tier === "web_read";
    if (anyHost) hosts = ["*"];
    else if (declaration === "from-machine") {
      machine ??= snapshot.spec(snapshot.key("Machine", spec.machine)) as PolicyRecord;
      const needs = machine.needs;
      if (needs !== undefined && !object(needs)) return invalid("machine.needs");
      const egress = needs?.egress;
      if (egress !== undefined && !object(egress)) return invalid("machine.needs.egress");
      hosts = egress && Object.hasOwn(egress, tier) ? egress[tier] : [];
    } else if (object(declaration)) {
      if (Object.keys(declaration).some((key) => key !== "hosts" && key !== "methods")) return invalid(field);
      hosts = declaration.hosts;
      if (Object.hasOwn(declaration, "methods")) {
        const raw = declaration.methods;
        if (!Array.isArray(raw) || raw.some((method) => typeof method !== "string" || !httpToken.test(method))) {
          return invalid(field + ".methods");
        }
        const normalized = [...new Set((raw as string[]).map((method) => method.toUpperCase()))].sort();
        methods = normalized.includes("*") ? ["*"] : normalized;
      }
    } else hosts = declaration;
    if (!Array.isArray(hosts)) return invalid(field + ".hosts");

    const uniqueHosts = new Set<string>();
    for (const entry of hosts) {
      if (typeof entry !== "string") return invalid(field + ".hosts");
      if (anyHost) { uniqueHosts.add("*"); continue; }
      const profile = entry.toLowerCase();
      let names: unknown[] = [entry];
      if (profileName.test(profile)) {
        if (!Object.hasOwn(profiles, profile)) {
          throw new FactoryError("EGRESS_PROFILE_UNKNOWN", "找不到出网配置档案", { station, profile });
        }
        const expansion = profiles[profile];
        if (!Array.isArray(expansion)) return invalid(field + ".hosts");
        names = expansion;
      }
      for (const name of names) {
        const normalized = typeof name === "string" ? policyHost(name) : undefined;
        if (!normalized) return invalid(field + ".hosts");
        uniqueHosts.add(normalized);
      }
    }
    const frozenMethods = Object.freeze([...methods]);
    for (const host of [...uniqueHosts].sort()) allow.push(Object.freeze({ tier, host, methods: frozenMethods }));
  }
  const monitored = object(spec.monitors) && Object.values(spec.monitors)
    .some((monitor) => object(monitor) && monitor.on === "egress");
  return Object.freeze({ station, policy, network: allow.length ? "egress" : "none", allow: Object.freeze(allow), onUnlisted, monitored });
}

/** Tests the static allowlist. An opaque CONNECT requires a rule allowing every inner method. */
export function matchesEgressAllowlist(plan: EgressPlan, target: string | URL, method: string): boolean {
  if (plan.network === "none" || !httpToken.test(method)) return false;
  const normalizedMethod = method.toUpperCase();
  let host: string | undefined;
  try {
    if (typeof target === "string" && (invalidUrlText.test(target) || !/^https?:\/\//i.test(target))) return false;
    host = hostname(typeof target === "string" ? new URL(target) : target);
  } catch { return false; }
  if (!host || (!host.includes(":") && (host.length > 253 || !host.split(".").every((label) => dnsLabel.test(label))))) return false;
  return plan.allow.some((rule) => matchesHost(rule.host, host)
    && (rule.methods.includes("*") || (normalizedMethod !== "CONNECT" && rule.methods.includes(normalizedMethod))));
}
