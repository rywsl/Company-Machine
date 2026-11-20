import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import type { FactoryResolution } from "../../mrl/factory/references";
import type { Context } from "./context";
import { ConversionStore } from "./conversion-requests";
import { FactoryError } from "./errors";
import { canonicalJson, randomUuid } from "./foundation";
import type { Snapshot } from "./snapshot";

type Contract = { from: string; guarantees: string[]; to: string; requires: string[] };
type RuntimeResolution = Contract & { chain: string[]; origin: "runtime" };

function names(value: unknown): string[] | null {
  return Array.isArray(value) && value.every((item) => typeof item === "string") ? value : null;
}

function contract(value: Record<string, unknown>): Contract | null {
  const { from, to } = value;
  const guarantees = names(value.guarantees);
  const requires = names(value.requires);
  if (typeof from !== "string" || typeof to !== "string" || !guarantees || !requires) return null;
  return { from, to, guarantees: [...new Set(guarantees)].sort(), requires: [...new Set(requires)].sort() };
}

export function recordPlanResolutions(context: Context, snapshot: Snapshot, rows: readonly FactoryResolution[]): void {
  const epoch = snapshot.epoch;
  if (!/^[a-z0-9-]+$/i.test(epoch)) throw new FactoryError("RUN_PLAN_INVALID", "Planner epoch 标识不合法", { epoch });
  const invalid = (reason: string): never => { throw new FactoryError("RUN_PLAN_INVALID", "Planner 转换解析表不一致", { epoch, reason }); };
  const precomputed = new Map<string, string>();
  for (const row of snapshot.result.resolutions) {
    const binding = snapshot.docs[row.line]?.spec?.steps?.[row.step]?.in?.[row.port];
    if (binding?.via !== undefined) continue;
    const typed = contract(row);
    const chain = names(row.chain);
    if (typed && chain) precomputed.set(canonicalJson(typed), canonicalJson(chain));
  }
  const path = join(context.root, "epochs", epoch, "resolution-runtime.json");
  const readSidecar = (): RuntimeResolution[] => {
    let data: unknown;
    try { data = JSON.parse(readFileSync(path, "utf8")); } catch (error) {
      if ((error as { code?: string }).code === "ENOENT") return [];
      if (error instanceof SyntaxError) return invalid("resolution-runtime");
      throw error;
    }
    if (!Array.isArray(data)) return invalid("resolution-runtime");
    return data.map((row) => {
      if (!row || typeof row !== "object" || Array.isArray(row)) return invalid("resolution-runtime");
      const typed = contract(row);
      const chain = names(row.chain);
      if (!typed || !chain || row.origin !== "runtime") return invalid("resolution-runtime");
      return { ...typed, chain, origin: "runtime" };
    });
  };
  context.db.transaction(() => {
    const existing = readSidecar();
    const known = new Map<string, string>();
    for (const { chain, origin: _origin, ...typed } of existing) {
      const key = canonicalJson(typed);
      if (precomputed.has(key) || known.has(key)) invalid("resolution-runtime");
      known.set(key, canonicalJson(chain));
    }
    const fresh: RuntimeResolution[] = [];
    let requests: ConversionStore | undefined;
    for (const row of rows) {
      const typed = contract(row);
      const chain = names(row.chain);
      if (!typed || !chain) continue;
      if (row.status === "no-converter") {
        (requests ??= new ConversionStore(context)).openOrMerge(typed, { foundAt: { epoch, step: row.step, port: row.port } });
        continue;
      }
      if (row.status !== "direct" && row.status !== "convert") continue;
      const key = canonicalJson(typed);
      const encoded = canonicalJson(chain);
      const frozen = precomputed.get(key);
      const cached = known.get(key);
      if (frozen !== undefined && frozen !== encoded) invalid("precomputed");
      if (cached !== undefined && cached !== encoded) invalid("resolution-runtime");
      if (frozen !== undefined || cached !== undefined) continue;
      fresh.push({ ...typed, chain, origin: "runtime" });
      known.set(key, encoded);
    }
    if (!fresh.length) return;
    const temporary = `${path}.${randomUuid()}.part`;
    mkdirSync(dirname(path), { recursive: true });
    try {
      writeFileSync(temporary, canonicalJson([...existing, ...fresh]), { flag: "wx" });
      renameSync(temporary, path);
    } finally { rmSync(temporary, { force: true }); }
  }).immediate();
}
