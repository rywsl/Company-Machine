import { chmodSync, lstatSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { copyFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, isAbsolute, join, relative, resolve, sep } from "node:path";
import type { Project } from "./models";
import { projectSourceFacts, repositoryReference } from "./project-sources";
import { runCommand } from "../runtime/podman";
import { resolveGitExecutable } from "../runtime/git";
import { SourceFetcher } from "./source-fetcher";

export type ProjectSourceMount = { source: string; index: number };
export const projectSourceRoot = "/mao/sources";

type UploadedBlob = { path: string; size: number };
type LookupUpload = (digest: string) => UploadedBlob | undefined;

function canonicalPath(path: string): string | undefined {
  if (!isAbsolute(path) || /[,\u0000-\u001f\u007f]/.test(path) || path.split(/[\\/]/).some((part) => part === "..")) return;
  try {
    const resolved = resolve(path);
    if (realpathSync(resolved) === resolved) return resolved;
  } catch { return; }
}

function inside(directory: string, path: string): boolean {
  const entry = relative(directory, path);
  return entry !== ".." && !entry.startsWith(".." + sep) && !isAbsolute(entry);
}

function sourceFolder(folder: string, protectedPaths: readonly string[]): string | undefined {
  const root = canonicalPath(folder);
  if (!root || resolve(root, "..") === root) return;
  if (protectedPaths.some((path) => inside(root, path) || inside(path, root))) return;
  try { return lstatSync(root).isDirectory() ? root : undefined; }
  catch { return; }
}

export interface PrepareProjectSourcesOptions {
  /**
   * Machine activation only validates the registered source. The Operator
   * preparation path keeps its historical checkout behavior by default.
   */
  cloneRepositories?: boolean;
  /** Fail when a registered source cannot be resolved on this host. */
  strict?: boolean;
  /** Persistent source cache used by the platform-owned source fetcher. */
  sourceCacheRoot?: string;
  /** Injected source provider for tests or a deployment-specific fetcher. */
  sourceFetcher?: SourceFetcher;
}

export async function prepareProjectSources(
  project: Project,
  uploadedBlob: LookupUpload,
  protectedPaths: readonly string[],
  options: PrepareProjectSourcesOptions = {},
) {
  const cloneRepositories = options.cloneRepositories ?? true;
  const strict = options.strict ?? false;
  const sourceFetcher = options.sourceFetcher ?? (options.sourceCacheRoot ? new SourceFetcher({ cacheRoot: options.sourceCacheRoot }) : undefined);
  const protectedRoots = [...protectedPaths.filter((path) => path !== ":memory:").map((path) => {
    try { return realpathSync(path); } catch { return resolve(path); }
  }), ...(process.platform === "linux" ? ["/dev", "/proc", "/sys"] : [])];
  const facts = projectSourceFacts(project, false);
  const git = resolveGitExecutable();
  const mounts: ProjectSourceMount[] = [];
  let staging: string | undefined;
  const close = () => {
    if (staging) rmSync(staging, { recursive: true, force: true, maxRetries: 3 });
  };
  try {
    for (const [offset, source] of project.sources.entries()) {
      const index = offset + 1;
      let hostPath: string | undefined;
      if (source.kind === "local_folder") {
        hostPath = source.localFolder && sourceFolder(source.localFolder, protectedRoots);
        if (!facts[offset].name && source.localFolder) facts[offset].name = basename(source.localFolder) || undefined;
      } else if (source.kind === "uploaded_file") {
        const digest = /^cas:\/\/(sha256:[0-9a-f]{64})$/.exec(source.fileRef ?? "")?.[1];
        const blob = digest && uploadedBlob(digest);
        if (blob) {
          const candidate = canonicalPath(blob.path);
          if (candidate) {
            const info = lstatSync(candidate);
            if (info.isFile() && info.size === blob.size) {
              hostPath = candidate;
              if ((info.mode & 0o004) === 0) {
                staging ??= mkdtempSync(join(tmpdir(), "mao-source-"));
                hostPath = join(staging, String(index));
                await copyFile(candidate, hostPath);
                chmodSync(hostPath, 0o644);
              }
            }
          }
        }
      } else if (source.kind === "repo_url" && source.repoUrl && cloneRepositories) {
        if (sourceFetcher) {
          const fetched = await sourceFetcher.fetch(source.repoUrl);
          facts[offset] = {
            ...facts[offset],
            ...(fetched.tool === "git" ? { commit: fetched.revision } : { source_revision: fetched.revision }),
            source_digest: fetched.digest,
            source_tool: fetched.tool,
            checkout: `intake://${project.id}/${source.id}`,
          };
          hostPath = fetched.checkout;
        } else {
          staging ??= mkdtempSync(join(tmpdir(), "mao-source-"));
          const checkout = join(staging, String(index));
          const cloned = await runCommand([git, "clone", "--depth", "1", "--no-tags", "--", source.repoUrl, checkout], {
            timeoutMs: 180_000,
            env: { GIT_TERMINAL_PROMPT: "0", GIT_CONFIG_NOSYSTEM: "1" },
          });
          if (cloned.exitCode !== 0) throw new Error("project repository clone failed");
          const revision = await runCommand([git, "-C", checkout, "rev-parse", "HEAD"], {
            timeoutMs: 30_000,
            env: { GIT_TERMINAL_PROMPT: "0", GIT_CONFIG_NOSYSTEM: "1" },
          });
          if (revision.exitCode !== 0) throw new Error("project repository revision lookup failed");
          const commit = new TextDecoder().decode(revision.stdout).trim();
          if (!/^[0-9a-f]{40}$/i.test(commit)) throw new Error("project repository revision is invalid");
          facts[offset] = { ...facts[offset], commit, checkout: `intake://${project.id}/${source.id}` };
          hostPath = checkout;
        }
      } else if (source.kind === "repo_url" && source.repoUrl && !cloneRepositories) {
        if (!repositoryReference(source.repoUrl, "intake") && strict) throw new Error("project-repository-invalid");
      } else if (strict) {
        throw new Error("project-source-unavailable");
      } else continue;
      if (strict && source.kind !== "repo_url" && !hostPath) throw new Error("project-source-unavailable");
      if (hostPath) mounts.push({ source: hostPath, index });
    }
    const mounted = new Set(mounts.map(({ index }) => index));
    return {
      mounts, close,
      facts: facts.map((fact, offset) => ({
        ...fact, ...(mounted.has(offset + 1) ? { containerPath: projectSourceRoot + "/" + (offset + 1) } : {}),
      })),
    };
  } catch (error) {
    close();
    throw error;
  }
}

export type PreparedProjectSources = Awaited<ReturnType<typeof prepareProjectSources>>;
export type PrepareProjectSources = (project: Project, options?: PrepareProjectSourcesOptions) => Promise<PreparedProjectSources | undefined>;
