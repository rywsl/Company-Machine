import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

export function readGovernedRegistry(root: string): { registry: string; gitTimeoutSeconds?: number } | undefined {
  let config: { registry?: unknown; git_timeout_s?: unknown } | undefined;
  try {
    config = JSON.parse(readFileSync(join(root, "factory.json"), "utf8"))?.governance;
  } catch (error) {
    if ((error as { code?: string }).code === "ENOENT" || error instanceof SyntaxError) return undefined;
    throw error;
  }
  if (typeof config?.registry !== "string" || !config.registry.trim()) return undefined;
  return { registry: resolve(root, config.registry), gitTimeoutSeconds: typeof config.git_timeout_s === "number" ? config.git_timeout_s : undefined };
}
