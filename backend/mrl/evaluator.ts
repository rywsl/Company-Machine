import { Expr, parseExpression } from "./parser";
import { checkExpression, type CheckOptions, type FunctionSignature } from "./checker";
import { DYN, type MrlType } from "./types";
import { IntegralFloat, MAX_INT64, MIN_INT64, floatValue, isInt, isMrlNumber, narrowInt, nativeNumber, type MrlInt, type MrlNumber } from "./numbers";

export class MrlEvalError extends Error {
  constructor(readonly code: string, message: string) { super(message); this.name = "MrlEvalError"; }
}

export type MrlValue = null | boolean | MrlNumber | string | MrlValue[] | { [key: string]: MrlValue };
export type EvalEnvironment = Record<string, MrlValue>;

const typeName = (v: unknown) => v === null ? "null" : isInt(v) ? "int" : isMrlNumber(v) ? "float" : Array.isArray(v) ? "list" : typeof v === "object" ? "map" : typeof v === "boolean" ? "bool" : typeof v;
export const equalValues = (a: unknown, b: unknown): boolean => {
  if (isMrlNumber(a) || isMrlNumber(b)) return isMrlNumber(a) && isMrlNumber(b) && compareNumbers(a, b) === 0;
  if (typeof a !== typeof b || Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => equalValues(x, b[i]));
  if (a && b && typeof a === "object" && typeof b === "object") {
    const ak = Object.keys(a as object).sort(), bk = Object.keys(b as object).sort();
    return equalValues(ak, bk) && ak.every((k) => equalValues((a as Record<string, MrlValue>)[k], (b as Record<string, MrlValue>)[k]));
  }
  return a === b;
};
function compareNumbers(a: MrlNumber, b: MrlNumber): number {
  const left = nativeNumber(a);
  const right = nativeNumber(b);
  return left < right ? -1 : left > right ? 1 : 0;
}

function finiteFloat(value: number): MrlNumber {
  if (!Number.isFinite(value)) throw new MrlEvalError("MRL-DOMAIN", "non-finite number");
  return floatValue(value);
}

export function isMrlMap(value: unknown): value is Record<string, MrlValue> {
  return value !== null && typeof value === "object" && !Array.isArray(value) && !(value instanceof IntegralFloat);
}

export interface EvaluateOptions {
  budget?: number;
  functions?: Record<string, (...args: MrlValue[]) => MrlValue>;
  inferredTypes?: WeakMap<Expr, MrlType>;
}
export interface CompileExprOptions extends EvaluateOptions {
  environment?: CheckOptions["environment"];
  expect?: CheckOptions["expect"];
  checkFunctions?: Record<string, FunctionSignature>;
}

export class Evaluator {
  private steps = 0;
  private readonly budget: number;
  private readonly functions?: EvaluateOptions["functions"];
  constructor(options: EvaluateOptions = {}) {
    this.budget = options.budget ?? 100_000;
    this.functions = options.functions;
  }
  evaluate(ast: Expr, environment: EvalEnvironment): MrlValue {
    this.steps++;
    if (this.steps > this.budget) throw new MrlEvalError("MRL-COST", `evaluation budget ${this.budget} exceeded`);
    switch (ast.kind) {
      case "literal": return ast.numericKind === "float" ? floatValue(ast.value as number) : ast.value as MrlValue;
      case "identifier": if (!Object.hasOwn(environment, ast.name)) throw new MrlEvalError("MRL-UNDEFINED", `undefined name ${ast.name}`); return environment[ast.name];
      case "list": return ast.items.map((item) => this.evaluate(item, environment));
      case "record": {
        const result = new Map<string, MrlValue>();
        for (const entry of ast.entries) {
          const key = this.evaluate(entry.key, environment);
          if (typeof key !== "string") throw new MrlEvalError("MRL-TYPE", "record key must be string");
          if (result.has(key)) throw new MrlEvalError("MRL-DUP-KEY", `duplicate key ${key}`);
          result.set(key, this.evaluate(entry.value, environment));
        }
        return Object.fromEntries(result);
      }
      case "member": { const object = this.evaluate(ast.object, environment); if (!isMrlMap(object)) throw new MrlEvalError("MRL-TYPE", "field selection requires record"); if (!Object.hasOwn(object, ast.field)) throw new MrlEvalError("MRL-NO-FIELD", `missing field ${ast.field}`); return object[ast.field]; }
      case "index": {
        const object = this.evaluate(ast.object, environment);
        const index = this.evaluate(ast.index, environment);
        if (Array.isArray(object)) {
          if (!isInt(index)) throw new MrlEvalError("MRL-TYPE", "list index requires int");
          if (index < 0 || index >= object.length) throw new MrlEvalError("MRL-INDEX", "list index out of range");
          return object[Number(index)];
        }
        if (isMrlMap(object) && typeof index === "string") {
          if (!Object.hasOwn(object, index)) throw new MrlEvalError("MRL-NO-FIELD", `missing key ${index}`);
          return object[index];
        }
        throw new MrlEvalError("MRL-TYPE", "invalid index operation");
      }
      case "unary": {
        const value = this.evaluate(ast.operand, environment);
        if (ast.op === "!") return !this.mustBool(value);
        const number = numericArgument("-", value);
        return isInt(number) ? int64Result(-number) : finiteFloat(-Number(nativeNumber(number)));
      }
      case "binary": return this.binary(ast.op, ast.left, ast.right, environment);
      case "conditional": { const condition = this.evaluate(ast.condition, environment); if (typeof condition !== "boolean") throw new MrlEvalError("MRL-TYPE", "condition requires bool"); return this.evaluate(condition ? ast.then : ast.otherwise, environment); }
      case "call": {
        if (ast.name === "has" && !ast.receiver && ast.args.length === 1 && ast.args[0].kind === "member") {
          const member = ast.args[0];
          const object = this.evaluate(member.object, environment);
          if (object === null) return false;
          if (!isMrlMap(object)) throw new MrlEvalError("MRL-TYPE", "has requires a record");
          return Object.hasOwn(object, member.field);
        }
        const hostFunction = this.functions && Object.hasOwn(this.functions, ast.name) ? this.functions[ast.name] : undefined;
        const fn = hostFunction ?? (Object.hasOwn(builtins, ast.name) ? builtins[ast.name] : undefined);
        if (!fn) throw new MrlEvalError("MRL-UNDEFINED", `undefined function ${ast.name}`);
        const args = [];
        if (ast.receiver) args.push(this.evaluate(ast.receiver, environment));
        for (const arg of ast.args) args.push(this.evaluate(arg, environment));
        return fn(...args);
      }
      case "macro": return this.macro(ast, environment);
    }
  }
  private binary(op: string, leftAst: Expr, rightAst: Expr, environment: EvalEnvironment): MrlValue {
    const left = this.evaluate(leftAst, environment);
    if (op === "&&") { if (typeof left !== "boolean") throw new MrlEvalError("MRL-TYPE", "&& requires bool"); return left && this.mustBool(this.evaluate(rightAst, environment)); }
    if (op === "||") { if (typeof left !== "boolean") throw new MrlEvalError("MRL-TYPE", "|| requires bool"); return left || this.mustBool(this.evaluate(rightAst, environment)); }
    const right = this.evaluate(rightAst, environment);
    if (op === "==") return equalValues(left, right); if (op === "!=") return !equalValues(left, right);
    if (op === "in") { if (Array.isArray(right)) return right.some((item) => equalValues(left, item)); if (isMrlMap(right)) { if (typeof left !== "string") throw new MrlEvalError("MRL-TYPE", "map key must be string"); return Object.hasOwn(right, left); } throw new MrlEvalError("MRL-TYPE", "in requires list or map"); }
    if (["<", "<=", ">", ">="].includes(op)) {
      const order = compareOrdered(left, right);
      return op === "<" ? order < 0 : op === "<=" ? order <= 0 : op === ">" ? order > 0 : order >= 0;
    }
    if (op === "+" && typeof left === "string" && typeof right === "string") return left + right;
    if (op === "+" && Array.isArray(left) && Array.isArray(right)) return [...left, ...right];
    return numericOperation(op, numericArgument(op, left), numericArgument(op, right));
  }
  private mustBool(value: MrlValue) { if (typeof value !== "boolean") throw new MrlEvalError("MRL-TYPE", "logical operand requires bool"); return value; }
  private macro(ast: Extract<Expr, { kind: "macro" }>, environment: EvalEnvironment): MrlValue {
    const target = this.evaluate(ast.target, environment);
    const items = Array.isArray(target) ? target : isMrlMap(target) ? orderedKeys(target) : null;
    if (!items) throw new MrlEvalError("MRL-TYPE", `${ast.name} requires list or map`);
    let matches = 0;
    const output: MrlValue[] = [];
    const scope = { ...environment, [ast.variable]: null as MrlValue };
    for (const item of items) {
      scope[ast.variable] = item;
      const included = ast.predicate ? this.mustBool(this.evaluate(ast.predicate, scope)) : true;
      if (ast.name === "all" && !included) return false;
      if (!included) continue;
      if (ast.name === "exists") return true;
      if (ast.name === "filter") output.push(item);
      if (ast.name === "map") output.push(this.evaluate(ast.body!, scope));
      matches += 1;
    }
    if (ast.name === "all") return true;
    if (ast.name === "exists") return false;
    if (ast.name === "exists_one") return matches === 1;
    return output;
  }
}

export function formatFloat(value: number): string {
  const magnitude = Math.abs(value);
  if (magnitude !== 0 && (magnitude < 1e-4 || magnitude >= 1e16)) {
    const [mantissa, exponent] = value.toExponential().split("e");
    return mantissa + "e" + exponent[0] + exponent.slice(1).padStart(2, "0");
  }
  const text = String(value);
  return Number.isInteger(value) ? text + ".0" : text;
}

function compareStrings(left: string, right: string): number {
  if (left === right) return 0;
  let offset = 0;
  for (const character of left) {
    const code = character.codePointAt(0)!;
    const other = right.codePointAt(offset);
    if (other === undefined) return 1;
    if (code !== other) return code - other;
    offset += character.length;
  }
  return -1;
}

function orderedKeys(value: Record<string, MrlValue>): string[] {
  return Object.keys(value).sort(compareStrings);
}

function compareOrdered(left: MrlValue, right: MrlValue): number {
  if (isMrlNumber(left) && isMrlNumber(right)) return compareNumbers(left, right);
  if (typeof left === "string" && typeof right === "string") return compareStrings(left, right);
  throw new MrlEvalError("MRL-TYPE", "values are not orderable");
}

function mapArgument(name: string, value: MrlValue): Record<string, MrlValue> {
  if (isMrlMap(value)) return value;
  throw new MrlEvalError("MRL-TYPE", name + " requires a map");
}

function listArgument(name: string, value: MrlValue): MrlValue[] {
  if (Array.isArray(value)) return value;
  throw new MrlEvalError("MRL-TYPE", name + " requires a list");
}

function numericArgument(name: string, value: MrlValue): MrlNumber {
  if (isMrlNumber(value)) return value;
  throw new MrlEvalError("MRL-TYPE", name + " requires a number");
}

function int64Result(value: MrlInt): MrlInt {
  if (typeof value === "number" && Number.isSafeInteger(value)) return value === 0 ? 0 : value;
  if (!Number.isFinite(Number(value)) || value < MIN_INT64 || value > MAX_INT64) {
    throw new MrlEvalError("MRL-OVERFLOW", "int is outside int64");
  }
  return narrowInt(BigInt(value));
}

function numericOperation(op: string, left: MrlNumber, right: MrlNumber): MrlNumber {
  const a = nativeNumber(left);
  const b = nativeNumber(right);
  if ((op === "/" || op === "%") && (b === 0 || b === 0n)) throw new MrlEvalError("MRL-DIV-ZERO", "division by zero");
  if (op !== "/" && isInt(left) && isInt(right)) {
    if (typeof left === "number" && typeof right === "number" && op !== "%") {
      const result = op === "+" ? left + right : op === "-" ? left - right : left * right;
      if (Number.isSafeInteger(result)) return result === 0 ? 0 : result;
    }
    const x = BigInt(a);
    const y = BigInt(b);
    if (op === "%") {
      const remainder = x % y;
      return int64Result(remainder !== 0n && (remainder < 0n) !== (y < 0n) ? remainder + y : remainder);
    }
    return int64Result(op === "+" ? x + y : op === "-" ? x - y : x * y);
  }
  if (op === "%") throw new MrlEvalError("MRL-TYPE", "% requires ints");
  const x = Number(a);
  const y = Number(b);
  return finiteFloat(op === "+" ? x + y : op === "-" ? x - y : op === "*" ? x * y : x / y);
}

function orderedList(name: string, value: MrlValue): MrlValue[] {
  const items = listArgument(name, value);
  if (items.every(isMrlNumber) || items.every((item) => typeof item === "string")) return items;
  throw new MrlEvalError("MRL-TYPE", name + " requires numbers or strings of a single kind");
}

function minMax(name: "min" | "max", values: MrlValue[]): MrlValue {
  const items = orderedList(name, values.length === 1 ? values[0] : values);
  if (!items.length) throw new MrlEvalError("MRL-DOMAIN", name + " requires at least one value");
  return items.reduce((best, item) => {
    const order = compareOrdered(item, best);
    return (name === "min" ? order < 0 : order > 0) ? item : best;
  });
}

const canonicalKinds = ["null", "bool", "int", "float", "string", "list", "map"];

export function compareCanonical(left: MrlValue, right: MrlValue): number {
  const a = typeName(left);
  const b = typeName(right);
  if (isMrlNumber(left) && isMrlNumber(right)) return compareNumbers(left, right) || canonicalKinds.indexOf(a) - canonicalKinds.indexOf(b);
  if (a !== b) return canonicalKinds.indexOf(a) - canonicalKinds.indexOf(b);
  if (typeof left === "boolean" && typeof right === "boolean") return Number(left) - Number(right);
  if (typeof left === "string" && typeof right === "string") return compareStrings(left, right);
  if (Array.isArray(left) && Array.isArray(right)) {
    for (let index = 0; index < Math.min(left.length, right.length); index += 1) {
      const comparison = compareCanonical(left[index], right[index]);
      if (comparison) return comparison;
    }
    return left.length - right.length;
  }
  if (isMrlMap(left) && isMrlMap(right)) {
    const leftKeys = orderedKeys(left);
    const rightKeys = orderedKeys(right);
    for (let index = 0; index < Math.min(leftKeys.length, rightKeys.length); index += 1) {
      const leftKey = leftKeys[index];
      const rightKey = rightKeys[index];
      const comparison = compareStrings(leftKey, rightKey) || compareCanonical(left[leftKey], right[rightKey]);
      if (comparison) return comparison;
    }
    return leftKeys.length - rightKeys.length;
  }
  return 0;
}

export function identityKey(value: MrlValue, byIdentity = true): string {
  if (isMrlNumber(value)) {
    const number = nativeNumber(value);
    if (isInt(value)) return "i" + number.toString();
    if (!byIdentity && typeof number === "number" && Number.isInteger(number)) return "i" + BigInt(number).toString();
    return "f" + number.toString();
  }
  if (Array.isArray(value)) return "[" + value.map((item) => identityKey(item, byIdentity)).join(",") + "]";
  if (isMrlMap(value)) return "{" + orderedKeys(value).map((key) => JSON.stringify(key) + ":" + identityKey(value[key], byIdentity)).join(",") + "}";
  return JSON.stringify(value);
}

function equalityKey(value: MrlValue): string {
  return identityKey(value, false);
}

export function uniqueValues(items: MrlValue[]): MrlValue[] {
  const seen = new Map<string, MrlValue>();
  for (const item of items) {
    const key = equalityKey(item);
    if (!seen.has(key)) seen.set(key, item);
  }
  return [...seen.values()];
}

function setOperation(name: "union" | "intersect" | "difference" | "isSubset", left: MrlValue, right: MrlValue): MrlValue {
  const items = listArgument(name, left);
  const other = listArgument(name, right);
  if (name === "union") return uniqueValues([...items, ...other]).sort(compareCanonical);
  const keys = new Set(other.map(equalityKey));
  if (name === "isSubset") return items.every((item) => keys.has(equalityKey(item)));
  const matching = items.filter((item) => keys.has(equalityKey(item)) === (name === "intersect"));
  return uniqueValues(matching).sort(compareCanonical);
}

function glob(value: MrlValue, pattern: MrlValue): boolean {
  if (typeof value !== "string" || typeof pattern !== "string") throw new MrlEvalError("MRL-TYPE", "glob requires strings");
  const characters = [...pattern];
  const tokens: Array<{ kind: "literal" | "star" | "deepStar" | "one"; value?: string }> = [];
  for (let index = 0; index < characters.length; index += 1) {
    const character = characters[index];
    if (character === "\\" && index + 1 < characters.length) {
      tokens.push({ kind: "literal", value: characters[++index] });
    } else if (character === "*" && characters[index + 1] === "*") {
      tokens.push({ kind: "deepStar" });
      index += 1;
    } else if (character === "*") {
      tokens.push({ kind: "star" });
    } else if (character === "?") {
      tokens.push({ kind: "one" });
    } else {
      tokens.push({ kind: "literal", value: character });
    }
  }
  const closure = (states: Uint8Array) => {
    for (const [index, token] of tokens.entries()) {
      if (states[index] && (token.kind === "star" || token.kind === "deepStar")) states[index + 1] = 1;
    }
  };
  let states = new Uint8Array(tokens.length + 1);
  let next = new Uint8Array(states.length);
  states[0] = 1;
  closure(states);
  for (const character of value) {
    next.fill(0);
    for (const [index, token] of tokens.entries()) {
      if (!states[index]) continue;
      if (token.kind === "deepStar" || (token.kind === "star" && character !== "/")) next[index] = 1;
      if ((token.kind === "literal" && token.value === character) || (token.kind === "one" && character !== "/")) next[index + 1] = 1;
    }
    if (!next.includes(1)) return false;
    closure(next);
    [states, next] = [next, states];
  }
  return states[tokens.length] === 1;
}

const builtins: Record<string, (...args: MrlValue[]) => MrlValue> = {
  size: (value) => { if (typeof value === "string") return Array.from(value).length; if (Array.isArray(value)) return value.length; if (isMrlMap(value)) return Object.keys(value).length; throw new MrlEvalError("MRL-TYPE", "size requires string, list or map"); },
  int: (value) => {
    if (isInt(value)) return int64Result(value);
    if (isMrlNumber(value)) return int64Result(Math.trunc(Number(nativeNumber(value))));
    if (typeof value !== "string") throw new MrlEvalError("MRL-TYPE", "int requires a number or string");
    if (!/^[+-]?[0-9]+$/.test(value)) throw new MrlEvalError("MRL-CONVERT", "cannot convert to int");
    return int64Result(BigInt(value));
  },
  float: (value) => { if (isMrlNumber(value)) return finiteFloat(Number(nativeNumber(value))); if (typeof value === "string" && /^[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/.test(value)) return finiteFloat(Number(value)); throw new MrlEvalError("MRL-CONVERT", "cannot convert to float"); },
  string: (value) => { if (typeof value === "string" || typeof value === "boolean" || isInt(value)) return String(value); if (isMrlNumber(value)) return formatFloat(Number(nativeNumber(value))); throw new MrlEvalError("MRL-TYPE", "cannot convert to string"); },
  abs: (value) => {
    const number = numericArgument("abs", value);
    if (isInt(number)) return int64Result(number < 0 ? -number : number);
    return finiteFloat(Math.abs(Number(nativeNumber(number))));
  },
  floor: (value) => isInt(value) ? int64Result(value) : int64Result(Math.floor(Number(nativeNumber(numericArgument("floor", value))))),
  ceil: (value) => isInt(value) ? int64Result(value) : int64Result(Math.ceil(Number(nativeNumber(numericArgument("ceil", value))))),
  round: (value) => {
    if (isInt(value)) return int64Result(value);
    const number = Number(nativeNumber(numericArgument("round", value)));
    const magnitude = Math.abs(number);
    const whole = Math.floor(magnitude);
    return int64Result(Math.sign(number) * (magnitude - whole >= 0.5 ? whole + 1 : whole));
  },
  min: (...values) => minMax("min", values),
  max: (...values) => minMax("max", values),
  sum: (value) => {
    const items = listArgument("sum", value);
    return items.reduce<MrlNumber>((total, item) => numericOperation("+", total, numericArgument("sum", item)), 0);
  },
  startsWith: (a, b) => { if (typeof a !== "string" || typeof b !== "string") throw new MrlEvalError("MRL-TYPE", "startsWith requires strings"); return a.startsWith(b); },
  endsWith: (a, b) => { if (typeof a !== "string" || typeof b !== "string") throw new MrlEvalError("MRL-TYPE", "endsWith requires strings"); return a.endsWith(b); },
  contains: (a, b) => { if (typeof a !== "string" || typeof b !== "string") throw new MrlEvalError("MRL-TYPE", "contains requires strings"); return a.includes(b); },
  lower: (value) => typeof value === "string" ? value.replace(/[A-Z]/g, (x) => x.toLowerCase()) : (() => { throw new MrlEvalError("MRL-TYPE", "lower requires string"); })(),
  upper: (value) => typeof value === "string" ? value.replace(/[a-z]/g, (x) => x.toUpperCase()) : (() => { throw new MrlEvalError("MRL-TYPE", "upper requires string"); })(),
  trim: (value) => typeof value === "string" ? value.replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, "") : (() => { throw new MrlEvalError("MRL-TYPE", "trim requires string"); })(),
  split: (value, separator) => { if (typeof value !== "string" || typeof separator !== "string") throw new MrlEvalError("MRL-TYPE", "split requires strings"); if (!separator) throw new MrlEvalError("MRL-DOMAIN", "empty separator"); return value.split(separator); },
  join: (value, separator) => { if (!Array.isArray(value) || typeof separator !== "string" || value.some((x) => typeof x !== "string")) throw new MrlEvalError("MRL-TYPE", "join requires string list"); return value.join(separator); },
  replace: (value, from, to) => { if (typeof value !== "string" || typeof from !== "string" || typeof to !== "string") throw new MrlEvalError("MRL-TYPE", "replace requires strings"); if (!from) throw new MrlEvalError("MRL-DOMAIN", "empty replacement"); return value.split(from).join(to); },
  glob,
  charsIn: (value, allowed) => {
    if (typeof value !== "string" || typeof allowed !== "string") throw new MrlEvalError("MRL-TYPE", "charsIn requires strings");
    const pool = new Set(allowed);
    for (const character of value) if (!pool.has(character)) return false;
    return true;
  },
  keys: (value) => orderedKeys(mapArgument("keys", value)),
  values: (value) => {
    const map = mapArgument("values", value);
    return orderedKeys(map).map((key) => map[key]);
  },
  get: (value, key, fallback) => {
    const map = mapArgument("get", value);
    if (typeof key !== "string") throw new MrlEvalError("MRL-TYPE", "get requires a string key");
    return Object.hasOwn(map, key) ? map[key] : fallback;
  },
  sort: (value) => [...orderedList("sort", value)].sort(compareOrdered),
  unique: (value) => uniqueValues(listArgument("unique", value)),
  union: (left, right) => setOperation("union", left, right),
  intersect: (left, right) => setOperation("intersect", left, right),
  difference: (left, right) => setOperation("difference", left, right),
  isSubset: (left, right) => setOperation("isSubset", left, right),
  type: (value) => typeName(value),
};

export function evaluate(source: string | Expr, environment: EvalEnvironment = {}, options: EvaluateOptions = {}): MrlValue { return new Evaluator(options).evaluate(typeof source === "string" ? parseExpression(source) : source, environment); }
export function compileExpr(source: string, options: CompileExprOptions = {}) {
  const checkFunctions = options.checkFunctions ?? (options.functions
    ? Object.fromEntries(Object.entries(options.functions).map(([name, fn]) => [name, { minArgs: fn.length, maxArgs: fn.length, returns: DYN }]))
    : undefined);
  const inferredTypes = options.inferredTypes ?? new WeakMap<Expr, MrlType>();
  const checked = checkExpression(source, { environment: options.environment, expect: options.expect, functions: checkFunctions, inferredTypes });
  return {
    ast: checked.ast,
    type: checked.type,
    warnings: checked.warnings,
    paths: checked.paths,
    evaluate: (environment: EvalEnvironment = {}) => new Evaluator({ ...options, inferredTypes }).evaluate(checked.ast, environment),
  };
}
export const standardFunctions = builtins;
