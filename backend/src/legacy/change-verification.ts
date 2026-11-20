import { readFileSync } from "node:fs";
import { join } from "node:path";
import { diffChangeFacts, object, scoreChangeRisk } from "./change-facts";
import type { ChangeSet } from "./changesets";
import type { Context } from "./context";
import type { ContractRunner } from "./contract";
import { FactoryError } from "./errors";
import { canonicalJson } from "./foundation";
import type { Snapshot } from "./snapshot";

type Change = ReturnType<typeof diffChangeFacts>["changes"][number];
export type VerificationContracts = Pick<ContractRunner, "runDiff" | "runEquivalence" | "runGolden" | "runValidator">;
type Data = Record<string, unknown>;
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : [];

export interface VerificationOutcome {
  state: "verified" | "rejected";
  report: Data;
  tier?: string;
  reviews?: number;
  runtime?: string | null;
}

export function verificationReject(code: string, detail: Data): VerificationOutcome {
  return { state: "rejected", report: { stage: "verifying", ok: false, code, detail } };
}

function mismatch(document: Data, changes: Change[]): Data | undefined {
  const compile = (items: unknown[][]) => new Map(items.map((item) => [canonicalJson(item), item]));
  const declared = compile(list(object(document.spec).changes).map((value) => {
    const item = object(value);
    return [item.kind, item.name, item.op];
  }));
  const actual = compile(changes.map((item) => [item.kind, item.name, ["deprecate", "retire"].includes(item.op) ? item.op : "upsert"]));
  const difference = (first: typeof declared, second: typeof declared) => [...first.keys()].sort().filter((key) => !second.has(key)).map((key) => first.get(key)!);
  const declaredOnly = difference(declared, actual);
  const actualOnly = difference(actual, declared);
  return declaredOnly.length || actualOnly.length ? { declared_only: declaredOnly, actual_only: actualOnly } : undefined;
}

function compatibility(old: Data, next: Data): string | undefined {
  const same = (left: unknown, right: unknown) => canonicalJson(left ?? null) === canonicalJson(right ?? null);
  const paths = (spec: Data, required: boolean) => list(spec.payload).map(object).filter((item) => !required || item.required !== false).map((item) => String(item.path)).sort();
  if (!same(paths(old, true), paths(next, true))) return "required payload changed";
  if (paths(old, false).some((path) => !paths(next, false).includes(path))) return "payload path removed";
  const oldMeta = object(old.meta_schema);
  const newMeta = object(next.meta_schema);
  if (!same(list(oldMeta.required).map(String).sort(), list(newMeta.required).map(String).sort())) return "required metadata changed";
  for (const key of Object.keys(object(oldMeta.properties))) {
    if (!Object.hasOwn(object(newMeta.properties), key) || !same(object(object(oldMeta.properties)[key]).type, object(object(newMeta.properties)[key]).type)) return "metadata property changed";
  }
  for (const field of ["facts", "properties"]) {
    const oldFields = object(old[field]);
    const newFields = object(next[field]);
    if (Object.entries(oldFields).some(([key, value]) => !Object.hasOwn(newFields, key) || !same(value, newFields[key]))) return `${field} changed`;
  }
  return ["kind", "fields", "item", "target", "extends"].find((field) => !same(old[field], next[field]));
}

export function goldenKeys(snapshot: Snapshot, stations: readonly string[]): string[] {
  const names = new Set(stations.map((key) => key.replace(/^Station:/, "").split("@")[0]));
  return snapshot.keys("GoldenSet").filter((key) => names.has(String(snapshot.spec(key).station).replace(/^Station:/, "").split("@")[0]));
}

function nonterminalRuns(context: Context) {
  if (!context.db.query("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'").get()) return [];
  return context.db.query<{ id: string; epoch: string; plan: string }>("SELECT id, epoch, plan FROM runs WHERE state NOT IN ('SUCCEEDED','FAILED','CANCELLED') ORDER BY id").all();
}

async function checkContracts(snapshot: Snapshot, changes: Change[], contracts: () => VerificationContracts | Promise<VerificationContracts>, timeoutS: number) {
  const changedStations = changes.filter((item) => item.kind === "Station").map(({ key }) => key);
  const timeout = { timeoutS };
  try {
    const runner = await contracts();
    const results: Array<{ id: string; verdict: string }> = [];
    for (const item of changes) {
      const spec = object(snapshot.spec(item.key));
      if (item.kind === "Station" && item.op === "modify") results.push(await runner.runDiff(item.key, timeout));
      if (item.kind === "Station" && ["add", "modify"].includes(item.op) && spec.role === "converter") {
        results.push(await runner.runEquivalence(item.key, timeout));
      }
      if (item.kind === "Type" && ["add", "minor", "major"].includes(item.op)) {
        for (const validator of list(spec.validators).map(object)) results.push(await runner.runValidator(item.key.slice(5), String(validator.name), timeout));
      }
    }
    for (const key of goldenKeys(snapshot, changedStations)) results.push(await runner.runGolden(key, undefined, timeout));
    const failed = results.filter((result) => result.verdict !== "pass").map((result) => result.id);
    return failed.length ? verificationReject("V4_FAILED", { results: failed }) : undefined;
  } catch (error) {
    if (error instanceof FactoryError) return verificationReject("V4_ERROR", { cause: error.code });
    throw error;
  }
}

export async function verifyChangeSet(context: Context, cs: ChangeSet, base: Snapshot, candidate: Snapshot, contracts: () => VerificationContracts | Promise<VerificationContracts>, timeoutS: number): Promise<VerificationOutcome> {
  const runtime = cs.proposer.startsWith("human:") ? null : candidate.key("OperatorRuntime", cs.proposer);
  if (runtime && !candidate.keys("OperatorRuntime").includes(runtime)) return verificationReject("PROPOSER_UNKNOWN", { by: cs.proposer });
  const frozen = JSON.parse(readFileSync(join(context.root, "epochs", cs.base_epoch, "resolutions.json"), "utf8"));
  const { changes, immutable, facts } = diffChangeFacts(base, candidate, frozen);
  if (immutable.length) return verificationReject("V3_IMMUTABLE", immutable[0]);
  const extra = mismatch(cs.document, changes);
  if (extra) return verificationReject("CHANGESET_MISMATCH", extra);
  for (const change of changes) if (change.op === "minor" && change.oldKey) {
    const reason = compatibility(base.spec(change.oldKey), candidate.spec(change.key));
    if (reason) return verificationReject("V3_INCOMPATIBLE", { type: change.key, reason });
  }
  const activeRuns = nonterminalRuns(context);
  for (const change of changes) if (change.op === "retire") {
    const runs = activeRuns.filter((run) => run.plan.includes(JSON.stringify(change.key))).map((run) => run.id);
    if (runs.length) return verificationReject("V3_RETIRE_REFERENCED", { key: change.key, runs });
  }
  let risk: ReturnType<typeof scoreChangeRisk>;
  try { risk = scoreChangeRisk(facts); }
  catch (error) {
    if (error instanceof FactoryError && error.code === "RISK_UNSCORED") return verificationReject(error.code, error.detail);
    throw error;
  }
  const failure = await checkContracts(candidate, changes, contracts, timeoutS);
  if (failure) return failure;
  const stations = changes.filter((change) => change.kind === "Station").map((change) => change.key).sort();
  const lines = [...new Set([...changes.filter((change) => change.kind === "Line").map((change) => change.key),
    ...(facts.resolution_changed ?? []).map(([line]) => String(line)),
    ...candidate.keys("Line").filter((key) => Object.values(object(candidate.spec(key).steps)).some((step) =>
      stations.includes(candidate.key("Station", String(object(step).station)))))])].sort();
  const runs = activeRuns.filter((run) => run.epoch === cs.base_epoch).map((run) => run.id);
  const sinks = stations.filter((station) => candidate.spec(station).role === "sink");
  return { state: "verified", tier: risk.tier, reviews: risk.reviews_required, runtime, report: {
    stage: "verifying", ok: true, code: null, detail: {}, impact: { lines, runs, stations, sinks }, stages: risk.stages, multiplier: risk.multiplier,
  } };
}
