import { resolveOfficialHarness } from "./harness";
import type { HarnessProfile } from "./models";

type ExplicitCredential = { variable: string; source?: string; base_url?: string };
const SENSITIVE_ENDPOINT_QUERY = /(?:api[_-]?key|secret|token|password|authorization|credential)/i;

const deploymentCredentials = {
  "claude-code": {
    key: "MAO_FOREMAN_ANTHROPIC_API_KEY", endpoint: "MAO_FOREMAN_ANTHROPIC_BASE_URL",
    variable: "ANTHROPIC_API_KEY", base: "https://api.anthropic.com",
  },
  codex: {
    key: "MAO_FOREMAN_CODEX_API_KEY", endpoint: "MAO_FOREMAN_CODEX_BASE_URL",
    variable: "CODEX_API_KEY", base: "https://api.openai.com/v1",
  },
};

function url(value: string): URL | undefined {
  try {
    const parsed = new URL(value);
    if (/^https?:$/.test(parsed.protocol) && !parsed.username && !parsed.password && !parsed.hash
      && ![...parsed.searchParams.keys()].some((key) => SENSITIVE_ENDPOINT_QUERY.test(key))) return parsed;
  } catch { return undefined; }
}

function explicitCredentials(): Record<string, ExplicitCredential> {
  const raw = process.env.MAO_FOREMAN_CREDENTIALS_JSON;
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(Object.entries(parsed).flatMap(([runtime, value]) => {
      if (!value || typeof value !== "object" || Array.isArray(value)) return [];
      const item = value as Record<string, unknown>;
      if (typeof item.variable !== "string" || !/^[A-Z][A-Z0-9_]*$/.test(item.variable)) return [];
      if (item.source !== undefined && (typeof item.source !== "string" || !/^[A-Z][A-Z0-9_]*$/.test(item.source))) return [];
      if (item.base_url !== undefined && typeof item.base_url !== "string") return [];
      return [[runtime, { variable: item.variable, source: item.source, base_url: item.base_url } as ExplicitCredential]];
    }));
  } catch { return {}; }
}

export function foremanCredential(
  profile: Pick<HarnessProfile, "role" | "runtime" | "baseUrl" | "secretRef">,
  readSecret?: (ref: string) => string | undefined,
): { variable: string; value: string; baseUrl: string } | undefined {
  return resolveCredential(profile, profile.secretRef ? readSecret?.(profile.secretRef) : undefined);
}

function resolveCredential(
  profile: Pick<HarnessProfile, "role" | "runtime" | "baseUrl" | "secretRef">,
  secretValue?: string,
): { variable: string; value: string; baseUrl: string } | undefined {
  if (profile.role !== "foreman") return;
  const id = resolveOfficialHarness(profile.runtime)?.id;
  const explicit = id ? explicitCredentials()[id] : undefined;
  if (explicit) {
    const value = profile.secretRef ? secretValue : explicit.source ? process.env[explicit.source] : undefined;
    if (!value?.trim()) return;
    const baseUrl = profile.baseUrl || explicit.base_url || "";
    if (baseUrl && !url(baseUrl)) return;
    return { variable: explicit.variable, value, baseUrl };
  }
  if (id !== "claude-code" && id !== "codex") return;
  const config = deploymentCredentials[id];
  const value = (profile.secretRef ? secretValue : undefined) ?? process.env[config.key];
  if (!value?.trim()) return;
  const endpoint = url(profile.baseUrl || config.base);
  const configured = process.env[config.endpoint];
  const expected = url(configured || config.base);
  if (!endpoint || !expected || endpoint.origin !== expected.origin) return;
  if (configured && (endpoint.pathname.replace(/\/+$/, "") !== expected.pathname.replace(/\/+$/, "") || endpoint.search !== expected.search)) return;
  return { variable: config.variable, value, baseUrl: profile.baseUrl || config.base };
}

export async function foremanCredentialAsync(
  profile: Pick<HarnessProfile, "role" | "runtime" | "baseUrl" | "secretRef">,
  readSecret?: (ref: string) => Promise<string | undefined>,
): Promise<{ variable: string; value: string; baseUrl: string } | undefined> {
  const secretValue = profile.secretRef && readSecret ? await readSecret(profile.secretRef) : undefined;
  return resolveCredential(profile, secretValue);
}

export function redactForemanText(text: string, value?: string): string {
  return (value ? text.replaceAll(value, "[REDACTED]") : text)
    .replace(/\b(api[_-]?key|secret|token|password|authorization|cookie)\s*[:=]\s*\S+/gi, "$1=[REDACTED]");
}

/** Carry only a possible credential prefix between transcript deltas. */
export class ForemanReplyRedactor {
  private pending = "";
  constructor(private readonly credential?: string) {}

  write(delta: string): string {
    const key = this.credential;
    if (!key) return redactForemanText(delta);
    const text = this.pending + delta;
    const replacedThrough = text.lastIndexOf(key);
    const tailStart = replacedThrough < 0 ? 0 : replacedThrough + key.length;
    let withheld = Math.min(key.length - 1, text.length - tailStart);
    while (withheld > 0 && !text.endsWith(key.slice(0, withheld))) withheld--;
    this.pending = withheld ? text.slice(-withheld) : "";
    return redactForemanText(text.slice(0, text.length - withheld), key);
  }

  finish(): string {
    return redactForemanText(this.pending, this.credential);
  }
}
