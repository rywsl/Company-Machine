import type { Bean } from "./warehouse";
import { FactoryError } from "./errors";
import type { Snapshot } from "./snapshot";

export function beanValue(bean: Bean): Record<string, unknown> {
  return { id: bean.id, type: bean.type, facts: structuredClone(bean.facts), meta: structuredClone(bean.meta), payload: bean.payload.map((file) => ({ path: file.path, digest: file.digest, size: file.size, media_type: file.mediaType })) };
}
export function inputsValue(inputs: Record<string, Bean | Bean[]>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(inputs).map(([name, value]) => [name, Array.isArray(value) ? value.map(beanValue) : beanValue(value)]));
}
function matches(declared: string, value: unknown): boolean {
  if (typeof value === "boolean") return declared === "bool";
  if (declared === "string") return typeof value === "string";
  if (declared === "int") return typeof value === "number" && Number.isSafeInteger(value);
  if (declared === "float") return typeof value === "number" && Number.isFinite(value) && !Number.isInteger(value);
  if (declared === "num") return typeof value === "number" && Number.isFinite(value);
  return false;
}
export function paramsValue(snapshot: Snapshot, stationKey: string, given: Record<string, unknown>): Record<string, unknown> {
  const key = snapshot.key("Station", stationKey);
  const declarations = snapshot.spec(key).params && typeof snapshot.spec(key).params === "object" ? snapshot.spec(key).params as Record<string, any> : {};
  const result: Record<string, unknown> = {};
  const problems: Array<Record<string, unknown>> = [];
  for (const [name, declaration] of Object.entries(declarations)) {
    const value = name in given ? given[name] : declaration && typeof declaration === "object" && "default" in declaration ? declaration.default : undefined;
    if (value === undefined && !(declaration && typeof declaration === "object" && "default" in declaration)) { problems.push({ name, reason: "missing" }); continue; }
    const expected = declaration && typeof declaration === "object" ? declaration.type : undefined;
    if (typeof expected !== "string" || !matches(expected, value)) { problems.push({ name, reason: "type", expected, value }); continue; }
    result[name] = value;
  }
  for (const name of Object.keys(given)) if (!(name in declarations)) problems.push({ name, reason: "unknown" });
  if (problems.length) throw new FactoryError("PARAM_INVALID", "工位参数不合法", { station: key, problems: problems.sort((a, b) => String(a.name).localeCompare(String(b.name))) });
  return result;
}
export function propertyEnv(bean: Bean): Record<string, unknown> { return { bean: beanValue(bean) }; }
export function acceptEnv(output: Bean | Bean[], inputs: Record<string, Bean | Bean[]>, params: Record<string, unknown>): Record<string, unknown> { return { out: Array.isArray(output) ? output.map(beanValue) : beanValue(output), inputs: inputsValue(inputs), params: { ...params } }; }
export function approvalEnv(phase: "propose" | "in_run", payload: Record<string, unknown>, inputs: Record<string, Bean | Bean[]>, params: Record<string, unknown>): Record<string, unknown> { return { [phase === "propose" ? "proposal" : "request"]: { ...payload }, inputs: inputsValue(inputs), params: { ...params } }; }
export function recipeEnv(inputs: Record<string, Bean | Bean[]>, params: Record<string, unknown>): Record<string, unknown> { return { inputs: inputsValue(inputs), params: { ...params } }; }
export function runtimeEnv(brief: { task: string; factory_contract: string }, limits: { max_turns: number; timeout_s: number; budget_usd: number }, operator: { model: string; runtime: string }): Record<string, unknown> { return { brief: { task: brief.task, factory_contract: brief.factory_contract }, limits: { ...limits }, operator: { ...operator } }; }
