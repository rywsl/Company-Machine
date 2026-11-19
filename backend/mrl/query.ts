import { evaluate, EvalEnvironment, MrlValue } from "./evaluator";

/** Evaluate a predicate against each relation row. The row fields are bound as names. */
export function queryRelation<T extends Record<string, MrlValue>>(rows: readonly T[], predicate: string, environment: EvalEnvironment = {}): T[] {
  return rows.filter((row) => evaluate(predicate, { ...environment, ...row }) === true);
}

/** Small adapter for callers that model a relation as a map of named rows. */
export function queryRelations<T extends Record<string, MrlValue>>(relations: Record<string, readonly T[]>, relation: string, predicate = "true", environment: EvalEnvironment = {}): T[] {
  const rows = relations[relation];
  if (!rows) throw new Error(`unknown relation ${relation}`);
  return queryRelation(rows, predicate, environment);
}
