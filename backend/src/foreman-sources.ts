import { chmodSync, lstatSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { copyFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, isAbsolute, join, relative, resolve, sep } from "node:path";
import type { Project } from "./models";
import { projectSourceFacts } from "./project-sources";

export type ForemanSourceMount = { source: string; index: number };
export const foremanSourceRoot = "/mao/sources";

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

export async function prepareForemanSources(project: Project, uploadedBlob: LookupUpload, protectedPaths: readonly string[]) {
  const protectedRoots = [...protectedPaths.filter((path) => path !== ":memory:").map((path) => {
    try { return realpathSync(path); } catch { return resolve(path); }
  }), ...(process.platform === "linux" ? ["/dev", "/proc", "/sys"] : [])];
  const facts = projectSourceFacts(project, false);
  const mounts: ForemanSourceMount[] = [];
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
      } else continue;
      if (hostPath) mounts.push({ source: hostPath, index });
    }
    const mounted = new Set(mounts.map(({ index }) => index));
    return {
      mounts, close,
      facts: facts.map((fact, offset) => ({
        ...fact, ...(mounted.has(offset + 1) ? { containerPath: foremanSourceRoot + "/" + (offset + 1) } : {}),
      })),
    };
  } catch (error) {
    close();
    throw error;
  }
}
