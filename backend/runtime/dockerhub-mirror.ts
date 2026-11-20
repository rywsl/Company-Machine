import { SystemClock, type Clock } from "../src/legacy/foundation";

const DEFAULT_GEOIP_URLS = [
  "https://ipapi.co/json/",
  "https://ipinfo.io/json",
  "https://api.country.is/",
];
const DEFAULT_MIRRORS = [
  "docker.m.daocloud.io",
  "dockerproxy.net",
  "docker.1panel.live",
  "hub.rat.dev",
];
const MANIFEST_ACCEPT = [
  "application/vnd.oci.image.index.v1+json",
  "application/vnd.docker.distribution.manifest.list.v2+json",
  "application/vnd.oci.image.manifest.v1+json",
  "application/vnd.docker.distribution.manifest.v2+json",
].join(", ");

export type DockerHubMirrorProbe = {
  mirror: string;
  image: string;
  reachable: boolean;
  status?: number;
  bytes: number;
  durationMs?: number;
  bytesPerSecond?: number;
  reason?: string;
};

export type DockerHubMirrorSelection = {
  countryCode?: string;
  mainlandChina: boolean;
  image: string;
  mirror?: string;
  probes: DockerHubMirrorProbe[];
};

export type DockerHubMirrorFetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
export type DockerHubMirrorOptions = {
  fetcher?: DockerHubMirrorFetcher;
  clock?: Pick<Clock, "monotonic">;
  geoIpUrl?: string;
  geoIpUrls?: string[];
  mirrors?: string[];
  timeoutMs?: number;
};

export type DockerHubImage = { path: string; reference: string };

function env(): Record<string, string | undefined> {
  return ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {});
}

function validHttpUrl(value: string, field: string): URL {
  let url: URL;
  try { url = new URL(value); } catch { throw new Error(`${field} must be an HTTP(S) URL`); }
  if (!/^https?:$/.test(url.protocol) || url.username || url.password) throw new Error(`${field} must be an HTTP(S) URL without credentials`);
  return url;
}

function mirrorHost(value: string): string | undefined {
  const raw = value.trim();
  if (!raw) return undefined;
  try {
    const url = validHttpUrl(raw.includes("://") ? raw : `https://${raw}`, "Docker Hub mirror");
    if (url.pathname !== "/" || url.search || url.hash || url.port) return undefined;
    return url.hostname.toLowerCase();
  } catch { return undefined; }
}

function configuredMirrors(options: DockerHubMirrorOptions): string[] {
  const source = options.mirrors ?? (env().MAO_DOCKERHUB_MIRRORS ?? DEFAULT_MIRRORS.join(","));
  const values = Array.isArray(source) ? source : [source];
  return [...new Set(values.flatMap((value) => value.split(",")).map(mirrorHost).filter((value): value is string => Boolean(value)))];
}

export function parseDockerHubImage(image: string): DockerHubImage | undefined {
  const value = image.trim();
  if (!value || /\s/.test(value) || value.startsWith("sha256:")) return undefined;
  const at = value.indexOf("@");
  const namePart = at >= 0 ? value.slice(0, at) : value;
  const reference = at >= 0 ? value.slice(at + 1) : undefined;
  const slash = namePart.indexOf("/");
  const first = slash < 0 ? namePart : namePart.slice(0, slash);
  const hasRegistry = slash >= 0 && (first.includes(".") || first.includes(":") || first === "localhost");
  const registry = hasRegistry ? first.toLowerCase() : "docker.io";
  if (registry !== "docker.io" && registry !== "index.docker.io" && registry !== "registry-1.docker.io") return undefined;
  const repository = hasRegistry ? namePart.slice(first.length + 1) : namePart;
  if (!repository || repository.startsWith("/") || repository.endsWith("/")) return undefined;
  if (at >= 0 && !reference) return undefined;
  const lastSlash = repository.lastIndexOf("/");
  const lastColon = repository.lastIndexOf(":");
  const tag = reference ?? (lastColon > lastSlash ? repository.slice(lastColon + 1) : "latest");
  const path = reference === undefined && lastColon > lastSlash ? repository.slice(0, lastColon) : repository;
  if (!path || !tag || /[^A-Za-z0-9_.:@/-]/.test(path) || /[^A-Za-z0-9_.:@-]/.test(tag)) return undefined;
  return { path: path.includes("/") ? path : `library/${path}`, reference: tag };
}

export function mirrorImageReference(image: string, mirror: string): string | undefined {
  const parsed = parseDockerHubImage(image);
  const host = mirrorHost(mirror);
  if (!parsed || !host) return undefined;
  return `${host}/${parsed.path}${parsed.reference.startsWith("sha256:") ? `@${parsed.reference}` : `:${parsed.reference}`}`;
}

function manifestUrl(image: string, mirror: string): string | undefined {
  const reference = mirrorImageReference(image, mirror);
  if (!reference) return undefined;
  const parsed = parseDockerHubImage(image);
  if (!parsed) return undefined;
  return `https://${mirrorHost(mirror)}/v2/${parsed.path}/manifests/${encodeURIComponent(parsed.reference)}`;
}

function countryCode(value: unknown): string | undefined {
  if (typeof value === "string" && /^[A-Za-z]{2}$/.test(value.trim())) return value.trim().toUpperCase();
  if (!value || typeof value !== "object") return undefined;
  const record = value as Record<string, unknown>;
  for (const key of ["country_code", "countryCode", "country", "iso_code", "isoCode"]) {
    const result = countryCode(record[key]);
    if (result) return result;
  }
  return undefined;
}

function timeoutValue(value: number | string | undefined, fallback: number): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function configuredGeoIpUrls(options: Pick<DockerHubMirrorOptions, "geoIpUrl" | "geoIpUrls">): string[] {
  const source = options.geoIpUrl
    ? [options.geoIpUrl]
    : options.geoIpUrls?.length
      ? options.geoIpUrls
      : (env().MAO_GEOIP_URL ?? env().MAO_GEOIP_URLS)?.split(",") ?? DEFAULT_GEOIP_URLS;
  const configured = [...new Set(source.map((value) => value.trim()).filter(Boolean))].slice(0, 8);
  return configured.length ? configured : DEFAULT_GEOIP_URLS;
}

async function fetchWithTimeout(fetcher: DockerHubMirrorFetcher, input: RequestInfo | URL, init: RequestInit, timeoutMs: number): Promise<Response | undefined> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      fetcher(input, { ...init, signal: controller.signal }),
      new Promise<undefined>((resolve) => { timer = setTimeout(() => { controller.abort(); resolve(undefined); }, timeoutMs); }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function queryGeoIpCountry(endpoint: string, fetcher: DockerHubMirrorFetcher, timeoutMs: number): Promise<string | undefined> {
  let url: URL;
  try { url = validHttpUrl(endpoint, "GeoIP URL"); } catch { return undefined; }
  let response: Response | undefined;
  try {
    response = await fetchWithTimeout(fetcher, url, { headers: { accept: "application/json, text/plain" } }, timeoutMs);
  } catch { return undefined; }
  if (!response || !response.ok) return undefined;
  try {
    const text = (await response.text()).trim();
    if (/^[A-Za-z]{2}$/.test(text)) return text.toUpperCase();
    return countryCode(JSON.parse(text));
  } catch { return undefined; }
}

export async function detectGeoIpCountry(options: Pick<DockerHubMirrorOptions, "fetcher" | "geoIpUrl" | "geoIpUrls" | "timeoutMs"> = {}): Promise<string | undefined> {
  const configured = countryCode(env().MAO_GEOIP_COUNTRY);
  if (configured) return configured;
  const fetcher = options.fetcher ?? fetch;
  const timeoutMs = timeoutValue(options.timeoutMs ?? env().MAO_GEOIP_TIMEOUT_MS, 3_000);
  const results = await Promise.all(configuredGeoIpUrls(options).map((endpoint) => queryGeoIpCountry(endpoint, fetcher, timeoutMs)));
  return results.find((value): value is string => Boolean(value));
}

function score(probe: DockerHubMirrorProbe): number {
  return probe.reachable && probe.bytes > 0 && probe.durationMs !== undefined && probe.durationMs > 0
    ? probe.bytes / (probe.durationMs / 1_000) : 0;
}

export function chooseFastestMirror(probes: readonly DockerHubMirrorProbe[]): DockerHubMirrorProbe | undefined {
  return probes.filter((probe) => score(probe) > 0).sort((a, b) => score(b) - score(a) || a.mirror.localeCompare(b.mirror))[0];
}

async function probeMirror(image: string, mirror: string, fetcher: DockerHubMirrorFetcher, clock: Pick<Clock, "monotonic">, timeoutMs: number): Promise<DockerHubMirrorProbe> {
  const url = manifestUrl(image, mirror);
  const mirroredImage = mirrorImageReference(image, mirror) ?? image;
  if (!url) return { mirror, image: mirroredImage, reachable: false, bytes: 0, reason: "invalid-mirror" };
  const started = clock.monotonic();
  try {
    const response = await fetchWithTimeout(fetcher, url, { headers: { accept: MANIFEST_ACCEPT } }, timeoutMs);
    if (!response) return { mirror, image: mirroredImage, reachable: false, bytes: 0, durationMs: Math.max(0, (clock.monotonic() - started) * 1_000), reason: "timeout" };
    const body = response.ok ? new Uint8Array(await response.arrayBuffer()) : new Uint8Array();
    const durationMs = Math.max(0.1, (clock.monotonic() - started) * 1_000);
    return { mirror, image: mirroredImage, reachable: response.ok, status: response.status, bytes: body.byteLength, durationMs, bytesPerSecond: body.byteLength / (durationMs / 1_000), ...(response.ok ? {} : { reason: `http-${response.status}` }) };
  } catch (error) {
    return { mirror, image: mirroredImage, reachable: false, bytes: 0, durationMs: Math.max(0, (clock.monotonic() - started) * 1_000), reason: error instanceof Error ? error.message : "probe-failed" };
  }
}

export async function selectDockerHubMirror(image: string, options: DockerHubMirrorOptions = {}): Promise<DockerHubMirrorSelection> {
  const parsed = parseDockerHubImage(image);
  if (!parsed) return { mainlandChina: false, image, probes: [] };
  const country = await detectGeoIpCountry(options);
  const mainlandChina = country === "CN";
  if (!mainlandChina) return { countryCode: country, mainlandChina, image, probes: [] };
  const clock = options.clock ?? new SystemClock();
  const fetcher = options.fetcher ?? fetch;
  const timeoutMs = timeoutValue(options.timeoutMs ?? env().MAO_DOCKERHUB_MIRROR_TIMEOUT_MS, 5_000);
  const probes = await Promise.all(configuredMirrors(options).map((mirror) => probeMirror(image, mirror, fetcher, clock, timeoutMs)));
  const fastest = chooseFastestMirror(probes);
  return { countryCode: country, mainlandChina, image: fastest?.image ?? image, mirror: fastest?.mirror, probes };
}

export async function resolveDockerHubImage(image: string, options: DockerHubMirrorOptions = {}): Promise<string> {
  return (await selectDockerHubMirror(image, options)).image;
}

