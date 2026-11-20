import { createHash } from "node:crypto";
import { existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import { runCommand, type CommandRunner } from "../runtime/podman";
import { resolveGitExecutable } from "../runtime/git";
import { repositoryReference } from "./project-sources";

export type SourceFetchResult = {
  checkout: string;
  revision: string;
  digest: string;
  tool: "git" | "github-archive";
};

export type SourceFetcherOptions = {
  cacheRoot: string;
  runner?: CommandRunner;
  fetcher?: typeof fetch;
  gitExecutable?: string;
  archiveLimitBytes?: number;
};

export class SourceToolUnavailableError extends Error {
  readonly code = "SOURCE_TOOL_UNAVAILABLE";
  readonly tool: string;
  readonly source: string;

  constructor(tool: string, source: string) {
    super(`source tool ${tool} is unavailable for ${source}`);
    this.name = "SourceToolUnavailableError";
    this.tool = tool;
    this.source = source;
  }
}

type CachedSource = SourceFetchResult & { url: string };
const DEFAULT_ARCHIVE_LIMIT = 512 * 1024 * 1024;
const SAFE_ARCHIVE_ENTRY = /^(?!\/)(?![A-Za-z]:[\\/])(?!.*(?:^|\/)\.\.(?:\/|$))[\w.][^\u0000]*$/;

function digest(bytes: Uint8Array): string {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

function cacheKey(url: string): string {
  return createHash("sha256").update(url).digest("hex");
}

function githubRepository(url: string): { owner: string; repository: string } | undefined {
  try {
    const parsed = new URL(url);
    if (parsed.hostname.toLowerCase() !== "github.com") return undefined;
    const parts = parsed.pathname.split("/").filter(Boolean).map((part) => decodeURIComponent(part));
    if (parts.length !== 2) return undefined;
    const repository = parts[1].replace(/\.git$/i, "");
    if (!/^[A-Za-z0-9_.-]+$/.test(parts[0]) || !/^[A-Za-z0-9_.-]+$/.test(repository)) return undefined;
    return { owner: parts[0], repository };
  } catch {
    return undefined;
  }
}

export function githubArchiveApiUrl(url: string): string | undefined {
  const repository = githubRepository(url);
  return repository ? `https://api.github.com/repos/${repository.owner}/${repository.repository}` : undefined;
}

function readCache(path: string, url: string): SourceFetchResult | undefined {
  const metadataPath = join(path, ".mao-source.json");
  if (!existsSync(path) || !existsSync(metadataPath)) return undefined;
  try {
    const cached = JSON.parse(readFileSync(metadataPath, "utf8")) as CachedSource;
    if (cached.url !== url || !cached.checkout || cached.checkout !== path || !cached.revision || !cached.digest || !cached.tool) return undefined;
    if (!lstatSync(path).isDirectory()) return undefined;
    return { checkout: cached.checkout, revision: cached.revision, digest: cached.digest, tool: cached.tool };
  } catch {
    return undefined;
  }
}

function validateArchiveListing(listing: string): void {
  for (const entry of listing.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)) {
    if (!SAFE_ARCHIVE_ENTRY.test(entry)) throw new Error("source archive contains an unsafe path");
  }
}

export class SourceFetcher {
  private readonly runner: CommandRunner;
  private readonly fetcher: typeof fetch;
  private readonly archiveLimitBytes: number;
  private readonly inFlight = new Map<string, Promise<SourceFetchResult>>();

  constructor(private readonly options: SourceFetcherOptions) {
    this.runner = options.runner ?? runCommand;
    this.fetcher = options.fetcher ?? fetch;
    this.archiveLimitBytes = options.archiveLimitBytes ?? DEFAULT_ARCHIVE_LIMIT;
    mkdirSync(resolve(options.cacheRoot), { recursive: true });
  }

  fetch(url: string): Promise<SourceFetchResult> {
    const reference = repositoryReference(url, "intake");
    if (!reference) return Promise.reject(new Error("project-repository-invalid"));
    const existing = this.inFlight.get(reference);
    if (existing) return existing;
    const pending = this.fetchInternal(reference).finally(() => {
      if (this.inFlight.get(reference) === pending) this.inFlight.delete(reference);
    });
    this.inFlight.set(reference, pending);
    return pending;
  }

  private async fetchInternal(url: string): Promise<SourceFetchResult> {
    const root = join(resolve(this.options.cacheRoot), cacheKey(url));
    const checkout = join(root, "checkout");
    const cached = readCache(checkout, url);
    if (cached) return cached;
    const git = this.options.gitExecutable !== undefined
      ? this.options.gitExecutable || undefined
      : Bun.which("git") ? resolveGitExecutable() : undefined;
    if (git) return this.clone(url, root, checkout, git);
    const github = githubRepository(url);
    if (!github) throw new SourceToolUnavailableError("git", url);
    return this.githubArchive(url, github.owner, github.repository, root, checkout);
  }

  private async clone(url: string, root: string, checkout: string, git: string): Promise<SourceFetchResult> {
    const temporary = mkdtempSync(join(resolve(this.options.cacheRoot), ".source-fetch-"));
    const temporaryCheckout = join(temporary, "checkout");
    try {
      const cloned = await this.runner([git, "clone", "--depth", "1", "--no-tags", "--", url, temporaryCheckout], {
        timeoutMs: 180_000,
        env: { GIT_TERMINAL_PROMPT: "0", GIT_CONFIG_NOSYSTEM: "1" },
      });
      if (cloned.exitCode !== 0) throw new Error("project repository clone failed");
      const revision = await this.runner([git, "-C", temporaryCheckout, "rev-parse", "HEAD"], {
        timeoutMs: 30_000,
        env: { GIT_TERMINAL_PROMPT: "0", GIT_CONFIG_NOSYSTEM: "1" },
      });
      const commit = new TextDecoder().decode(revision.stdout).trim();
      if (revision.exitCode !== 0 || !/^[0-9a-f]{40}$/i.test(commit)) throw new Error("project repository revision is invalid");
      const result: SourceFetchResult = { checkout, revision: commit, digest: `sha256:${commit}`, tool: "git" };
      this.commitCache(temporary, root, temporaryCheckout, result, url);
      return result;
    } finally {
      if (existsSync(temporary)) rmSync(temporary, { recursive: true, force: true, maxRetries: 3 });
    }
  }

  private async githubArchive(url: string, owner: string, repository: string, root: string, checkout: string): Promise<SourceFetchResult> {
    const apiUrl = `https://api.github.com/repos/${owner}/${repository}`;
    const metadata = await this.fetcher(apiUrl, { headers: { accept: "application/vnd.github+json", "user-agent": "mao-source-fetcher" } });
    if (!metadata.ok) throw new Error(`source repository metadata unavailable (${metadata.status})`);
    const data = await metadata.json() as { default_branch?: unknown };
    const branch = typeof data.default_branch === "string" && /^[A-Za-z0-9._/-]+$/.test(data.default_branch) ? data.default_branch : "main";
    const archiveResponse = await this.fetcher(`https://codeload.github.com/${owner}/${repository}/tar.gz/refs/heads/${encodeURIComponent(branch)}`, { headers: { accept: "application/octet-stream", "user-agent": "mao-source-fetcher" } });
    if (!archiveResponse.ok) throw new Error(`source repository archive unavailable (${archiveResponse.status})`);
    const bytes = new Uint8Array(await archiveResponse.arrayBuffer());
    if (bytes.byteLength > this.archiveLimitBytes) throw new Error("source repository archive exceeds the configured limit");
    const temporary = mkdtempSync(join(resolve(this.options.cacheRoot), ".source-fetch-"));
    const archive = join(temporary, "source.tar.gz");
    try {
      await Bun.write(archive, bytes);
      const listing = await this.runner(["tar", "-tzf", archive], { timeoutMs: 30_000 });
      if (listing.exitCode !== 0) throw new Error("source archive listing failed");
      validateArchiveListing(new TextDecoder().decode(listing.stdout));
      const temporaryCheckout = join(temporary, "checkout");
      mkdirSync(temporaryCheckout, { recursive: true });
      const extracted = await this.runner(["tar", "-xzf", archive, "-C", temporaryCheckout, "--strip-components", "1", "--no-same-owner", "--no-same-permissions"], { timeoutMs: 180_000 });
      if (extracted.exitCode !== 0) throw new Error("source archive extraction failed");
      const sourceDigest = digest(bytes);
      const result: SourceFetchResult = { checkout, revision: `archive:${sourceDigest}`, digest: sourceDigest, tool: "github-archive" };
      this.commitCache(temporary, root, temporaryCheckout, result, url);
      return result;
    } finally {
      if (existsSync(temporary)) rmSync(temporary, { recursive: true, force: true, maxRetries: 3 });
    }
  }

  private commitCache(temporary: string, root: string, temporaryCheckout: string, result: SourceFetchResult, url: string): void {
    mkdirSync(resolve(this.options.cacheRoot), { recursive: true });
    mkdirSync(root, { recursive: true });
    const metadataPath = join(temporaryCheckout, ".mao-source.json");
    writeFileSync(metadataPath, JSON.stringify({ ...result, url }, null, 2) + "\n", "utf8");
    if (existsSync(root)) {
      const current = readCache(result.checkout, url);
      if (current) return;
      rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    }
    renameSync(temporary, root);
  }
}
