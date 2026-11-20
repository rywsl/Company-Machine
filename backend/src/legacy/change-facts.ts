import { readFileSync } from "node:fs";
import { join } from "node:path";
import { specRoot } from "../../mrl/factory/paths";
import { registryKinds } from "../../mrl/factory/schema";
import { buildRuleProgram, type RuleProgram } from "../../mrl/rules";
import { FactoryError } from "./errors";
import { canonicalJson, parseDuration } from "./foundation";
import type { Snapshot } from "./snapshot";

type Fields = Record<string, unknown>;
type Facts = Record<string, Array<Array<string | number>>>;
type Change = { id: string; kind: string; name: string; key: string; op: string; oldKey?: string; fields?: string[]; roles?: string[]; weakened?: string[] };
type Resolution = { line: string; step: string; port: string; status?: unknown; chain?: unknown };

export function object(value: unknown): Fields {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Fields : {};
}

const list = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
const same = (a: unknown, b: unknown): boolean => canonicalJson(a ?? null) === canonicalJson(b ?? null);
const keys = (a: Fields, b: Fields) => [...new Set([...Object.keys(a), ...Object.keys(b)])].sort();
const omit = (fields: Fields, excluded: string[]) => Object.fromEntries(Object.entries(fields).filter(([key]) => !excluded.includes(key)));
const below = (a: unknown, b: unknown): boolean => {
  const available = new Set(list(b).map((item) => canonicalJson(item)));
  return list(a).every((item) => available.has(canonicalJson(item)));
};

function compareVersion(a: string, b: string): number {
  const first = a.split(".").map(Number);
  const second = b.split(".").map(Number);
  for (let i = 0; i < Math.min(first.length, second.length); i++) if (first[i] !== second[i]) return first[i] - second[i];
  return first.length - second.length;
}

function lifecycleChange(old: Fields, next: Fields): "deprecate" | "retire" | undefined {
  const oldMeta = object(old.metadata);
  const newMeta = object(next.metadata);
  const before = oldMeta.lifecycle ?? "active";
  const after = newMeta.lifecycle ?? "active";
  if (!same({ ...old, metadata: omit(oldMeta, ["lifecycle"]) }, { ...next, metadata: omit(newMeta, ["lifecycle"]) })) return undefined;
  if (before === "active" && after === "deprecated") return "deprecate";
  if (before !== "retired" && after === "retired") return "retire";
}

function narrowFields(field: string, key: string, old: Fields, next: Fields): string[] {
  const fields: string[] = [];
  if (!same(old[key], next[key])) fields.push(`${field}.${key}`);
  if (!same(omit(old, [key]), omit(next, [key]))) fields.push(field);
  return fields;
}

function durationSeconds(value: unknown): number | undefined {
  if (typeof value !== "string") return undefined;
  try { return parseDuration(value); } catch { return undefined; }
}

function agentFields(old: Fields, next: Fields): string[] {
  const scope = ["lines", "subscribe", "budget"];
  const changed = keys(old, next).filter((field) => !scope.includes(field) && !same(old[field], next[field]));
  const from = object(old.subscribe);
  const to = object(next.subscribe);
  const oldBudget = object(old.budget);
  const newBudget = object(next.budget);
  const oldIngress = object(from.ingress);
  const newIngress = object(to.ingress);
  const schedule = (value: unknown) => list(value).map((item) => durationSeconds(object(item).every)).sort((a, b) => Number(a) - Number(b));
  const oldSchedule = schedule(from.schedule);
  const newSchedule = schedule(to.schedule);
  const oldTimeout = durationSeconds(oldBudget.turn_timeout);
  const newTimeout = durationSeconds(newBudget.turn_timeout);
  const expands = !below(next.lines, old.lines) || !below(Object.keys(newIngress), Object.keys(oldIngress)) ||
    !below(to.beans, from.beans) || newSchedule.length > oldSchedule.length ||
    newSchedule.some((value, index) => value !== undefined && oldSchedule[index] !== undefined && value < oldSchedule[index]!) ||
    ["daily_usd", "turn_usd"].some((key) => Number(newBudget[key] ?? 0) > Number(oldBudget[key] ?? 0)) ||
    (newTimeout !== undefined && oldTimeout !== undefined && newTimeout > oldTimeout);
  if (expands) return [...changed, "scope.expanded"];
  const scopeChanges = scope.filter((field) => !same(old[field], next[field]));
  if (!scopeChanges.length) return changed;
  const changesUnknownScope = Object.keys(newIngress).some((key) => !same(newIngress[key], oldIngress[key])) ||
    oldSchedule.includes(undefined) || newSchedule.includes(undefined) ||
    (oldBudget.turn_timeout !== newBudget.turn_timeout && (oldTimeout === undefined || newTimeout === undefined)) ||
    !same(omit(oldBudget, ["daily_usd", "turn_usd", "turn_timeout"]), omit(newBudget, ["daily_usd", "turn_usd", "turn_timeout"]));
  return !changed.length && !changesUnknownScope ? ["scope.reduced"] : [...changed, ...scopeChanges];
}

function changedFields(kind: string, old: Fields, next: Fields): string[] {
  const before = object(old.spec);
  const after = object(next.spec);
  if (kind === "Agent") return agentFields(before, after);
  const fields: string[] = [];
  if (kind === "GoldenSet" || kind === "Baseline") {
    if (!same(before, after)) fields.push("spec");
  } else for (const field of keys(before, after)) {
    if (same(before[field], after[field])) continue;
    if (kind === "Machine" && field === "runtime") fields.push(...narrowFields(field, "image", object(before[field]), object(after[field])));
    else if (kind === "Station" && field === "operator") fields.push(...narrowFields(field, "model", object(before[field]), object(after[field])));
    else if (kind === "Machine" && field === "recipes") {
      const stripTemplates = (value: unknown) => Object.fromEntries(Object.entries(object(value)).map(([name, recipe]) => [name, omit(object(recipe), ["task_template"])]));
      fields.push(same(stripTemplates(before[field]), stripTemplates(after[field])) ? "recipes.task_template" : "recipes");
    } else fields.push(field);
  }
  const identity = ["name", "version", "lifecycle"];
  if (!same(omit(object(old.metadata), identity), omit(object(next.metadata), identity))) fields.push("metadata");
  return fields;
}

function weakenedFields(old: Fields, next: Fields): string[] {
  const before = object(old.spec);
  const after = object(next.spec);
  const found = new Set<string>();
  const validators = new Map(list(after.validators).map((entry) => [object(entry).name, object(entry)]));
  for (const entry of list(before.validators)) {
    const validator = object(entry);
    const replacement = validators.get(validator.name);
    if (!replacement) found.add("validator-removed");
    if (!below(object(validator.fixtures).reject, object(replacement?.fixtures).reject)) found.add("reject-fixture-removed");
  }
  for (const [name, value] of Object.entries(object(before.outputs))) {
    const replacement = object(object(after.outputs)[name]);
    if (!below(object(value).guarantees, replacement.guarantees)) found.add("guarantee-removed");
    if (!below(object(value).accept, replacement.accept)) found.add("accept-removed");
  }
  for (const phase of ["in_run", "propose"]) {
    const previous = object(before.approval)[phase];
    const changed = object(after.approval)[phase];
    if ((previous !== "auto" && changed === "auto") || !same(object(previous).auto_if, object(changed).auto_if)) found.add("approval-auto");
  }
  return [...found].sort();
}

function split(key: string): { kind: string; name: string; version: string; family: string } {
  const colon = key.indexOf(":"), at = key.lastIndexOf("@");
  return { kind: key.slice(0, colon), name: key.slice(colon + 1, at), version: key.slice(at + 1), family: key.slice(0, at) };
}

export function diffChangeFacts(base: Snapshot, candidate: Snapshot, baseResolutions: readonly Resolution[] = base.result.resolutions) {
  const governed = new Set<string>(registryKinds);
  const before = new Set(base.keys().filter((key) => governed.has(split(key).kind)));
  const after = new Set(candidate.keys().filter((key) => governed.has(split(key).kind)));
  const latest = new Map<string, string>();
  for (const key of before) {
    const { family, version } = split(key);
    const existing = latest.get(family);
    if (!existing || compareVersion(version, split(existing).version) > 0) latest.set(family, key);
  }
  const changes: Change[] = [];
  const immutable: { key: string; reason: "modified" | "deleted" }[] = [];
  for (const key of [...before].sort()) if (!after.has(key)) immutable.push({ key, reason: "deleted" });
  const order = (key: string) => { const { kind, name } = split(key); return `${kind}\0${name}\0${key}`; };
  for (const key of [...after].sort((a, b) => order(a) < order(b) ? -1 : 1)) {
    const { kind, name, version, family } = split(key);
    const next = object(candidate.doc(key));
    const item = { id: `c${changes.length}`, kind, name, key };
    if (before.has(key)) {
      const old = object(base.doc(key));
      if (same(old, next)) continue;
      const op = lifecycleChange(old, next);
      if (!op) { immutable.push({ key, reason: "modified" }); continue; }
      changes.push({ ...item, op });
      continue;
    }
    const oldKey = latest.get(family);
    const old = oldKey ? object(base.doc(oldKey)) : {};
    const op = !oldKey ? "add" : kind !== "Type" ? "modify"
      : split(oldKey).version.split(".")[0] === version.split(".")[0] ? "minor" : "major";
    const change: Change = { ...item, op };
    if (oldKey) {
      change.oldKey = oldKey;
      if (op === "modify") change.fields = changedFields(kind, old, next);
      change.weakened = weakenedFields(old, next);
    }
    if (kind === "Station") change.roles = [...new Set([object(old.spec).role, object(next.spec).role].filter((role): role is string => typeof role === "string"))];
    changes.push(change);
  }
  const facts: Facts = { change: changes.map(({ id, kind, op }) => [id, kind, op]), spec_count: [[changes.length]] };
  for (const [relation, field] of [["field_changed", "fields"], ["station_role", "roles"], ["weakened", "weakened"]] as const) {
    const rows = changes.flatMap((change) => (change[field] ?? []).map((value) => [change.id, value]));
    if (rows.length) facts[relation] = rows;
  }
  const edge = ({ line, step, port }: Resolution) => [line, step, port];
  const resolutions = new Map<string, Resolution>(candidate.result.resolutions.map((row) => [canonicalJson(edge(row)), row]));
  const moved = baseResolutions.filter((row) => {
    const next = resolutions.get(canonicalJson(edge(row)));
    return !next || !same(row.status, next.status) || !same(row.chain, next.chain);
  }).map(edge).sort();
  if (moved.length) facts.resolution_changed = moved;
  return { facts, changes, immutable };
}

let riskRules: { source: string; program: RuleProgram } | undefined;
function riskProgram(): RuleProgram {
  const name = join(specRoot(), "mrl", "governance", "risk.mrl");
  const source: string = readFileSync(name, "utf8");
  if (riskRules?.source !== source) riskRules = { source, program: buildRuleProgram([{ name, source }]) };
  return riskRules.program;
}

export function governanceLimits(): Record<string, number> {
  return Object.fromEntries(riskProgram().evaluate().rowsOf("limit").map(([name, value]) => [String(name), Number(value)]));
}

export function scoreChangeRisk(facts: Facts) {
  const model = riskProgram().evaluate(facts);
  const unscored = model.rowsOf("unscored");
  const tiers = model.rowsOf("risk").map(([value]) => Number(value));
  const tier = Math.max(...tiers);
  const reviews = model.rowsOf("reviews").find(([value]) => Number(value) === tier)?.[1];
  const multiplier = model.rowsOf("stage_multiplier").find(([value]) => Number(value) === tier)?.[1];
  const stages = model.rowsOf("needs_stage").filter(([value]) => Number(value) === tier).map(([, stage]) => String(stage)).sort();
  if (unscored.length || !tiers.length || reviews === undefined || multiplier === undefined || (tier > 0 && !stages.length)) {
    throw new FactoryError("RISK_UNSCORED", "风险规则给不出完整等级", { changes: unscored });
  }
  return { tier: `T${tier}`, reviews_required: Number(reviews), multiplier: Number(multiplier), stages };
}
