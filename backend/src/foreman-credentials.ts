import { resolveOfficialHarness } from "./harness";
import type { HarnessProfile } from "./models";

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
    if (/^https?:$/.test(parsed.protocol) && !parsed.username && !parsed.password && !parsed.hash) return parsed;
  } catch { return undefined; }
}

export function foremanCredential(profile: Pick<HarnessProfile, "role" | "runtime" | "baseUrl">): { variable: string; value: string; baseUrl: string } | undefined {
  if (profile.role !== "foreman") return;
  const id = resolveOfficialHarness(profile.runtime)?.id;
  if (id !== "claude-code" && id !== "codex") return;
  const config = deploymentCredentials[id];
  const value = process.env[config.key];
  if (!value?.trim()) return;
  const endpoint = url(profile.baseUrl || config.base);
  const configured = process.env[config.endpoint];
  const expected = url(configured || config.base);
  if (!endpoint || !expected || endpoint.origin !== expected.origin) return;
  if (configured && (endpoint.pathname.replace(/\/+$/, "") !== expected.pathname.replace(/\/+$/, "") || endpoint.search !== expected.search)) return;
  return { variable: config.variable, value, baseUrl: profile.baseUrl || config.base };
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
