import { existsSync, readdirSync, statSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

function env(): Record<string, string | undefined> {
  return ((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {});
}

/** Resolve the repository's normative MRL sources without depending on Python. */
export function specRoot(): string {
  const configured = env().MRL_SPEC_DIR;
  if (configured) return resolve(configured);
  const candidate = resolve(import.meta.dir, "../../../spec");
  if (existsSync(join(candidate, "schemas"))) return candidate;
  throw new Error("Cannot find spec directory. Set MRL_SPEC_DIR.");
}

export function schemasDir(): string { return join(specRoot(), "schemas"); }
export function kernelDir(): string { return join(specRoot(), "mrl", "kernel"); }
export function referenceConstitutionDir(): string { return join(specRoot(), "mrl", "constitution"); }

export function kernelSources(): Array<{ name: string; source: string }> {
  const directory = kernelDir();
  if (!existsSync(directory)) return [];
  return readdirSync(directory)
    .filter((name) => name.endsWith(".mrl"))
    .sort()
    .map((name) => ({ name: `kernel/${name}`, source: readFileSync(join(directory, name), "utf8") }));
}

export function listFiles(root: string): string[] {
  const output: string[] = [];
  const visit = (directory: string) => {
    for (const entry of readdirSync(directory)) {
      const path = join(directory, entry);
      const stat = statSync(path);
      if (stat.isDirectory()) visit(path);
      else output.push(path);
    }
  };
  if (existsSync(root)) visit(root);
  return output.sort();
}
