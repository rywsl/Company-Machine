import { Expr, parseExpression } from "./parser";
import { checkExpression, type CheckOptions, type FunctionSignature } from "./checker";
import { DYN } from "./types";

export class MrlEvalError extends Error {
  constructor(readonly code: string, message: string) { super(message); this.name = "MrlEvalError"; }
}

export type MrlValue = null | boolean | number | string | MrlValue[] | { [key: string]: MrlValue };
export type EvalEnvironment = Record<string, MrlValue>;

const isNumber = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const typeName = (v: unknown) => v === null ? "null" : Array.isArray(v) ? "list" : typeof v === "object" ? "map" : typeof v === "number" ? (Number.isInteger(v) ? "int" : "float") : typeof v;
const equal = (a: unknown, b: unknown): boolean => {
  if (isNumber(a) && isNumber(b)) return a === b;
  if (typeof a !== typeof b || Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => equal(x, b[i]));
  if (a && b && typeof a === "object" && typeof b === "object") {
    const ak = Object.keys(a as object).sort(), bk = Object.keys(b as object).sort();
    return equal(ak, bk) && ak.every((k) => equal((a as Record<string, MrlValue>)[k], (b as Record<string, MrlValue>)[k]));
  }
  return a === b;
};
const finite = (value: number, code = "MRL-DOMAIN") => { if (!Number.isFinite(value)) throw new MrlEvalError(code, "non-finite number"); return Object.is(value, -0) ? 0 : value; };

export interface EvaluateOptions { budget?: number; functions?: Record<string, (...args: MrlValue[]) => MrlValue>; }
export interface CompileExprOptions extends EvaluateOptions {
  environment?: CheckOptions["environment"];
  expect?: CheckOptions["expect"];
  checkFunctions?: Record<string, FunctionSignature>;
}

export class Evaluator {
  private steps = 0;
  private readonly budget: number;
  private readonly functions: Record<string, (...args: MrlValue[]) => MrlValue>;
  constructor(options: EvaluateOptions = {}) { this.budget = options.budget ?? 100_000; this.functions = { ...builtins, ...(options.functions ?? {}) }; }
  evaluate(ast: Expr, environment: EvalEnvironment): MrlValue {
    this.steps++;
    if (this.steps > this.budget) throw new MrlEvalError("MRL-COST", `evaluation budget ${this.budget} exceeded`);
    switch (ast.kind) {
      case "literal": return ast.value as MrlValue;
      case "identifier": if (!(ast.name in environment)) throw new MrlEvalError("MRL-UNDEFINED", `undefined name ${ast.name}`); return environment[ast.name];
      case "list": return ast.items.map((item) => this.evaluate(item, environment));
      case "record": {
        const result: Record<string, MrlValue> = {};
        for (const entry of ast.entries) { const key = this.evaluate(entry.key, environment); if (typeof key !== "string") throw new MrlEvalError("MRL-TYPE", "record key must be string"); if (key in result) throw new MrlEvalError("MRL-DUP-KEY", `duplicate key ${key}`); result[key] = this.evaluate(entry.value, environment); }
        return result;
      }
      case "member": { const object = this.evaluate(ast.object, environment); if (!object || typeof object !== "object" || Array.isArray(object)) throw new MrlEvalError("MRL-TYPE", "field selection requires record"); if (!(ast.field in object)) throw new MrlEvalError("MRL-NO-FIELD", `missing field ${ast.field}`); return (object as Record<string, MrlValue>)[ast.field]; }
      case "index": {
        const object = this.evaluate(ast.object, environment);
        const index = this.evaluate(ast.index, environment);
        if (Array.isArray(object)) {
          if (typeof index !== "number" || !Number.isInteger(index) || index < 0 || index >= object.length) throw new MrlEvalError("MRL-INDEX", "list index out of range");
          return object[index];
        }
        if (object && typeof object === "object" && !Array.isArray(object) && typeof index === "string") {
          if (!(index in object)) throw new MrlEvalError("MRL-NO-FIELD", `missing key ${index}`);
          return (object as Record<string, MrlValue>)[index];
        }
        throw new MrlEvalError("MRL-TYPE", "invalid index operation");
      }
      case "unary": { const value = this.evaluate(ast.operand, environment); if (ast.op === "!") { if (typeof value !== "boolean") throw new MrlEvalError("MRL-TYPE", "! requires bool"); return !value; } if (!isNumber(value)) throw new MrlEvalError("MRL-TYPE", "- requires number"); return finite(-value, "MRL-OVERFLOW"); }
      case "binary": return this.binary(ast.op, ast.left, ast.right, environment);
      case "conditional": { const condition = this.evaluate(ast.condition, environment); if (typeof condition !== "boolean") throw new MrlEvalError("MRL-TYPE", "condition requires bool"); return this.evaluate(condition ? ast.then : ast.otherwise, environment); }
      case "call": {
        if (ast.name === "has" && !ast.receiver && ast.args.length === 1 && ast.args[0].kind === "member") {
          const member = ast.args[0];
          const object = this.evaluate(member.object, environment);
          if (object === null) return false;
          if (!object || typeof object !== "object" || Array.isArray(object)) throw new MrlEvalError("MRL-TYPE", "has requires a record");
          return member.field in object;
        }
        const fn = this.functions[ast.name]; if (!fn) throw new MrlEvalError("MRL-UNDEFINED", `undefined function ${ast.name}`);
        const args = ast.args.map((arg) => this.evaluate(arg, environment)); if (ast.receiver) args.unshift(this.evaluate(ast.receiver, environment)); return fn(...args);
      }
      case "macro": return this.macro(ast, environment);
    }
  }
  private binary(op: string, leftAst: Expr, rightAst: Expr, environment: EvalEnvironment): MrlValue {
    const left = this.evaluate(leftAst, environment);
    if (op === "&&") { if (typeof left !== "boolean") throw new MrlEvalError("MRL-TYPE", "&& requires bool"); return left && this.mustBool(this.evaluate(rightAst, environment)); }
    if (op === "||") { if (typeof left !== "boolean") throw new MrlEvalError("MRL-TYPE", "|| requires bool"); return left || this.mustBool(this.evaluate(rightAst, environment)); }
    const right = this.evaluate(rightAst, environment);
    if (op === "==") return equal(left, right); if (op === "!=") return !equal(left, right);
    if (op === "in") { if (Array.isArray(right)) return right.some((item) => equal(left, item)); if (right && typeof right === "object" && !Array.isArray(right)) { if (typeof left !== "string") throw new MrlEvalError("MRL-TYPE", "map key must be string"); return left in right; } throw new MrlEvalError("MRL-TYPE", "in requires list or map"); }
    if (["<", "<=", ">", ">="].includes(op)) { if (!((isNumber(left) && isNumber(right)) || (typeof left === "string" && typeof right === "string"))) throw new MrlEvalError("MRL-TYPE", "values are not orderable"); return op === "<" ? left < right : op === "<=" ? left <= right : op === ">" ? left > right : left >= right; }
    if (op === "+" && typeof left === "string" && typeof right === "string") return left + right;
    if (op === "+" && Array.isArray(left) && Array.isArray(right)) return [...left, ...right];
    if (!isNumber(left) || !isNumber(right)) throw new MrlEvalError("MRL-TYPE", `${op} requires numbers`);
    if (op === "%") { if (!Number.isInteger(left) || !Number.isInteger(right)) throw new MrlEvalError("MRL-TYPE", "% requires ints"); if (right === 0) throw new MrlEvalError("MRL-DIV-ZERO", "modulo by zero"); return left % right; }
    if (op === "/" && right === 0) throw new MrlEvalError("MRL-DIV-ZERO", "division by zero");
    return finite(op === "+" ? left + right : op === "-" ? left - right : op === "*" ? left * right : left / right, Number.isInteger(left) && Number.isInteger(right) && op !== "/" ? "MRL-OVERFLOW" : "MRL-DOMAIN");
  }
  private mustBool(value: MrlValue) { if (typeof value !== "boolean") throw new MrlEvalError("MRL-TYPE", "logical operand requires bool"); return value; }
  private macro(ast: Extract<Expr, { kind: "macro" }>, environment: EvalEnvironment): MrlValue {
    const target = this.evaluate(ast.target, environment);
    const items = Array.isArray(target) ? target : target && typeof target === "object" ? Object.keys(target).sort() : null;
    if (!items) throw new MrlEvalError("MRL-TYPE", `${ast.name} requires list or map`);
    const values = items.map((item) => { const inner = { ...environment, [ast.variable]: item }; return ast.predicate ? this.mustBool(this.evaluate(ast.predicate, inner)) : true; });
    if (ast.name === "all") return values.every(Boolean); if (ast.name === "exists") return values.some(Boolean); if (ast.name === "exists_one") return values.filter(Boolean).length === 1;
    if (ast.name === "filter") return items.filter((_, index) => values[index]);
    return items.flatMap((item, index) => values[index] ? [this.evaluate(ast.body!, { ...environment, [ast.variable]: item })] : []);
  }
}

const builtins: Record<string, (...args: MrlValue[]) => MrlValue> = {
  size: (value) => { if (typeof value === "string") return Array.from(value).length; if (Array.isArray(value)) return value.length; if (value && typeof value === "object") return Object.keys(value).length; throw new MrlEvalError("MRL-TYPE", "size requires string, list or map"); },
  int: (value) => { if (typeof value === "number") return Math.trunc(value); if (typeof value === "string" && /^[+-]?[0-9]+$/.test(value)) { const n = Number(value); if (Number.isSafeInteger(n)) return n; } throw new MrlEvalError("MRL-CONVERT", "cannot convert to int"); },
  float: (value) => { if (typeof value === "number") return value; if (typeof value === "string" && /^[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/.test(value)) return finite(Number(value)); throw new MrlEvalError("MRL-CONVERT", "cannot convert to float"); },
  string: (value) => { if (typeof value === "string") return value; if (typeof value === "boolean") return value ? "true" : "false"; if (typeof value === "number") return String(value); throw new MrlEvalError("MRL-TYPE", "cannot convert to string"); },
  abs: (value) => { if (!isNumber(value)) throw new MrlEvalError("MRL-TYPE", "abs requires number"); return finite(Math.abs(value), "MRL-OVERFLOW"); },
  min: (...values) => { const list = values.length === 1 && Array.isArray(values[0]) ? values[0] : values; if (!list.length || list.some((item) => !isNumber(item))) throw new MrlEvalError("MRL-TYPE", "min requires numeric values"); return (list as number[]).reduce((a, b) => a < b ? a : b); },
  max: (...values) => { const list = values.length === 1 && Array.isArray(values[0]) ? values[0] : values; if (!list.length || list.some((item) => !isNumber(item))) throw new MrlEvalError("MRL-TYPE", "max requires numeric values"); return (list as number[]).reduce((a, b) => a > b ? a : b); },
  sum: (value) => { if (!Array.isArray(value) || value.some((item) => !isNumber(item))) throw new MrlEvalError("MRL-TYPE", "sum requires numeric list"); return finite((value as number[]).reduce((a, b) => a + b, 0), "MRL-OVERFLOW"); },
  startsWith: (a, b) => { if (typeof a !== "string" || typeof b !== "string") throw new MrlEvalError("MRL-TYPE", "startsWith requires strings"); return a.startsWith(b); },
  endsWith: (a, b) => { if (typeof a !== "string" || typeof b !== "string") throw new MrlEvalError("MRL-TYPE", "endsWith requires strings"); return a.endsWith(b); },
  contains: (a, b) => { if (typeof a !== "string" || typeof b !== "string") throw new MrlEvalError("MRL-TYPE", "contains requires strings"); return a.includes(b); },
  lower: (value) => typeof value === "string" ? value.replace(/[A-Z]/g, (x) => x.toLowerCase()) : (() => { throw new MrlEvalError("MRL-TYPE", "lower requires string"); })(),
  upper: (value) => typeof value === "string" ? value.replace(/[a-z]/g, (x) => x.toUpperCase()) : (() => { throw new MrlEvalError("MRL-TYPE", "upper requires string"); })(),
  trim: (value) => typeof value === "string" ? value.trim() : (() => { throw new MrlEvalError("MRL-TYPE", "trim requires string"); })(),
  split: (value, separator) => { if (typeof value !== "string" || typeof separator !== "string") throw new MrlEvalError("MRL-TYPE", "split requires strings"); if (!separator) throw new MrlEvalError("MRL-DOMAIN", "empty separator"); return value.split(separator); },
  join: (value, separator) => { if (!Array.isArray(value) || typeof separator !== "string" || value.some((x) => typeof x !== "string")) throw new MrlEvalError("MRL-TYPE", "join requires string list"); return value.join(separator); },
  replace: (value, from, to) => { if (typeof value !== "string" || typeof from !== "string" || typeof to !== "string") throw new MrlEvalError("MRL-TYPE", "replace requires strings"); if (!from) throw new MrlEvalError("MRL-DOMAIN", "empty replacement"); return value.split(from).join(to); },
  type: (value) => typeName(value),
};

export function evaluate(source: string | Expr, environment: EvalEnvironment = {}, options: EvaluateOptions = {}): MrlValue { return new Evaluator(options).evaluate(typeof source === "string" ? parseExpression(source) : source, environment); }
export function compileExpr(source: string, options: CompileExprOptions = {}) {
  const checkFunctions = options.checkFunctions ?? (options.functions
    ? Object.fromEntries(Object.entries(options.functions).map(([name, fn]) => [name, { minArgs: fn.length, maxArgs: fn.length, returns: DYN }]))
    : undefined);
  const checked = checkExpression(source, { environment: options.environment, expect: options.expect, functions: checkFunctions });
  return {
    ast: checked.ast,
    type: checked.type,
    warnings: checked.warnings,
    paths: checked.paths,
    evaluate: (environment: EvalEnvironment = {}) => new Evaluator(options).evaluate(checked.ast, environment),
  };
}
export const standardFunctions = builtins;
