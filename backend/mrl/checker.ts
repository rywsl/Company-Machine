import type { Expr } from "./parser";
import { parseExpression } from "./parser";
import { BOOL, DYN, FLOAT, INT, NULL, NUM, STRING, comparable, compatible, isNumeric, isPrimitive, list, lub, map, normalizeEnvironment, numericResult, typeName, type MrlType, type TypeEnvironment } from "./types";

export type DiagnosticSeverity = "error" | "warning";
export interface MrlDiagnostic { code: string; severity: DiagnosticSeverity; message: string; }

export class MrlStaticError extends Error {
  constructor(readonly diagnostics: MrlDiagnostic[]) {
    super(diagnostics.map((item) => `${item.code}: ${item.message}`).join("\n"));
    this.name = "MrlStaticError";
  }
}

export interface FunctionSignature {
  minArgs: number;
  maxArgs?: number;
  returns: MrlType;
  accepts?: (args: MrlType[]) => boolean;
  inferReturn?: (args: MrlType[]) => MrlType;
}

export interface CheckOptions {
  environment?: TypeEnvironment;
  functions?: Record<string, FunctionSignature>;
  expect?: MrlType | string;
  inferredTypes?: WeakMap<Expr, MrlType>;
}

export interface CheckResult {
  ast: Expr;
  type: MrlType;
  warnings: MrlDiagnostic[];
  paths: string[];
}

type TypePredicate = (type: MrlType) => boolean;
const anyType = () => true;
const stringType: TypePredicate = (type) => isPrimitive(type, "string");
const numericOrString: TypePredicate = (type) => isNumeric(type) || stringType(type);
const mapType: TypePredicate = (type) => type.kind === "map" || type.kind === "record";
const listElement = (type: MrlType) => type.kind === "list" ? type.element : DYN;
const listOf = (accepts: TypePredicate): TypePredicate => (type) => type.kind === "list" && (isPrimitive(type.element, "dyn") || accepts(type.element));
const listType = listOf(anyType);

function reducedType(items: MrlType[]): MrlType {
  return items.length ? items.reduce(lub) : DYN;
}

function mapElement(type: MrlType): MrlType {
  if (type.kind === "map") return type.element;
  if (type.kind === "record" && !type.open) return reducedType(Object.values(type.fields).map((field) => field.type));
  return DYN;
}

function fixed(returns: MrlType, types: TypePredicate[], inferReturn?: FunctionSignature["inferReturn"]): FunctionSignature {
  return {
    minArgs: types.length,
    maxArgs: types.length,
    returns,
    inferReturn,
    accepts: (args) => args.every((type, index) => isPrimitive(type, "dyn") || types[index](type)),
  };
}

const minMax: FunctionSignature = {
  minArgs: 1,
  returns: DYN,
  accepts: (args) => {
    if (args.length === 1) return isPrimitive(args[0], "dyn") || listOf(numericOrString)(args[0]);
    return args.every((type) => isPrimitive(type, "dyn") || numericOrString(type))
      && !(args.some(isNumeric) && args.some(stringType));
  },
  inferReturn: (args) => args.length === 1 ? listElement(args[0]) : reducedType(args),
};

const setSignature = fixed(list(DYN), [listType, listType], ([a, b]) => list(lub(listElement(a), listElement(b))));
const stringPredicate = fixed(BOOL, [stringType, stringType]);
const stringTransform = fixed(STRING, [stringType]);
const signatures: Record<string, FunctionSignature> = {
  size: fixed(INT, [(type) => stringType(type) || listType(type) || mapType(type)]),
  int: fixed(INT, [numericOrString]),
  float: fixed(FLOAT, [numericOrString]),
  string: fixed(STRING, [(type) => numericOrString(type) || isPrimitive(type, "bool")]),
  abs: fixed(NUM, [isNumeric], ([type]) => isNumeric(type) ? type : NUM),
  floor: fixed(INT, [isNumeric]),
  ceil: fixed(INT, [isNumeric]),
  round: fixed(INT, [isNumeric]),
  min: minMax,
  max: minMax,
  sum: fixed(NUM, [listOf(isNumeric)], ([type]) => isNumeric(listElement(type)) ? listElement(type) : NUM),
  startsWith: stringPredicate,
  endsWith: stringPredicate,
  contains: stringPredicate,
  charsIn: stringPredicate,
  glob: stringPredicate,
  lower: stringTransform,
  upper: stringTransform,
  trim: stringTransform,
  split: fixed(list(STRING), [stringType, stringType]),
  join: fixed(STRING, [listOf(stringType), stringType]),
  replace: fixed(STRING, [stringType, stringType, stringType]),
  keys: fixed(list(STRING), [mapType]),
  values: fixed(list(DYN), [mapType], ([type]) => list(mapElement(type))),
  get: fixed(DYN, [mapType, stringType, anyType], ([type, , fallback]) => lub(mapElement(type), fallback)),
  sort: fixed(list(DYN), [listOf(numericOrString)], ([type]) => list(listElement(type))),
  unique: fixed(list(DYN), [listType], ([type]) => list(listElement(type))),
  union: setSignature,
  intersect: setSignature,
  difference: setSignature,
  isSubset: fixed(BOOL, [listType, listType]),
  type: fixed(STRING, [anyType]),
};

const pathOf = (node: Expr): string | undefined => {
  const fields: string[] = [];
  let current = node;
  while (current.kind === "member") { fields.push(current.field); current = current.object; }
  return current.kind === "identifier" ? [current.name, ...fields.reverse()].join(".") : undefined;
};

const guardsTrue = (node: Expr): Set<string> => {
  if (node.kind === "call" && node.name === "has" && !node.receiver && node.args.length === 1 && node.args[0].kind === "member") {
    const path = pathOf(node.args[0]);
    return path ? new Set([path]) : new Set();
  }
  if (node.kind === "binary" && node.op === "&&") return new Set([...guardsTrue(node.left), ...guardsTrue(node.right)]);
  if (node.kind === "binary" && node.op === "||") {
    const left = guardsTrue(node.left); const right = guardsTrue(node.right);
    return new Set([...left].filter((item) => right.has(item)));
  }
  if (node.kind === "unary" && node.op === "!") return guardsFalse(node.operand);
  return new Set();
};

const guardsFalse = (node: Expr): Set<string> => {
  if (node.kind === "binary" && node.op === "||") return new Set([...guardsFalse(node.left), ...guardsFalse(node.right)]);
  if (node.kind === "binary" && node.op === "&&") {
    const left = guardsFalse(node.left); const right = guardsFalse(node.right);
    return new Set([...left].filter((item) => right.has(item)));
  }
  if (node.kind === "unary" && node.op === "!") return guardsTrue(node.operand);
  return new Set();
};

class Checker {
  private readonly diagnostics: MrlDiagnostic[] = [];
  private readonly paths = new Set<string>();
  private readonly environment: Record<string, MrlType>;
  private readonly functions: Record<string, FunctionSignature>;
  private readonly inferredTypes?: WeakMap<Expr, MrlType>;

  constructor(options: CheckOptions) {
    this.environment = normalizeEnvironment(options.environment);
    this.functions = { ...signatures, ...(options.functions ?? {}) };
    this.inferredTypes = options.inferredTypes;
  }

  run(ast: Expr, expected?: MrlType): CheckResult {
    const type = this.infer(ast, {}, new Set());
    if (expected && compatible(type, expected) === "no") this.error("MRL-TYPE", `expression has type ${typeName(type)}, expected ${typeName(expected)}`);
    const errors = this.diagnostics.filter((item) => item.severity === "error");
    if (errors.length) throw new MrlStaticError(this.diagnostics);
    return { ast, type, warnings: this.diagnostics.filter((item) => item.severity === "warning"), paths: [...this.paths].sort() };
  }

  private error(code: string, message: string) { this.diagnostics.push({ code, severity: "error", message }); }
  private warning(code: string, message: string) { this.diagnostics.push({ code, severity: "warning", message }); }
  private lookup(name: string, scope: Record<string, MrlType>): MrlType | undefined {
    if (Object.hasOwn(scope, name)) return scope[name];
    if (Object.hasOwn(this.environment, name)) return this.environment[name];
    return undefined;
  }

  private infer(node: Expr, scope: Record<string, MrlType>, guards: Set<string>): MrlType {
    const type = this.inferType(node, scope, guards);
    this.inferredTypes?.set(node, type);
    return type;
  }

  private inferType(node: Expr, scope: Record<string, MrlType>, guards: Set<string>): MrlType {
    switch (node.kind) {
      case "literal":
        if (node.value === null) return NULL;
        if (typeof node.value === "boolean") return BOOL;
        if (node.numericKind) return node.numericKind === "float" ? FLOAT : INT;
        return STRING;
      case "identifier": {
        const type = this.lookup(node.name, scope);
        if (!type) { this.error("MRL-UNDEFINED", `undefined name ${node.name}`); return DYN; }
        this.paths.add(node.name); return type;
      }
      case "list": {
        return list(reducedType(node.items.map((item) => this.infer(item, scope, guards))));
      }
      case "record": {
        const fields = new Map<string, { type: MrlType; required: boolean }>();
        const values: MrlType[] = [];
        for (const entry of node.entries) {
          const keyType = this.infer(entry.key, scope, guards);
          if (!isPrimitive(keyType, "string") && keyType !== DYN) this.error("MRL-TYPE", "record keys must be string");
          const type = this.infer(entry.value, scope, guards);
          values.push(type);
          if (entry.key.kind === "literal" && typeof entry.key.value === "string") {
            if (fields.has(entry.key.value)) this.error("MRL-DUP-KEY", `duplicate key ${entry.key.value}`);
            fields.set(entry.key.value, { type, required: true });
          }
        }
        if (fields.size === node.entries.length && fields.size) return { kind: "record", fields: Object.fromEntries(fields), open: false };
        return map(reducedType(values));
      }
      case "member": return this.select(node, scope, guards);
      case "index": return this.index(node, scope, guards);
      case "unary": {
        const type = this.infer(node.operand, scope, guards);
        if (node.op === "!") { if (type !== DYN && !isPrimitive(type, "bool")) this.error("MRL-TYPE", `! requires bool, got ${typeName(type)}`); return BOOL; }
        if (type !== DYN && !isNumeric(type)) this.error("MRL-TYPE", `- requires number, got ${typeName(type)}`);
        return type === DYN ? NUM : type;
      }
      case "binary": return this.binary(node, scope, guards);
      case "conditional": {
        const condition = this.infer(node.condition, scope, guards);
        if (condition !== DYN && !isPrimitive(condition, "bool")) this.error("MRL-TYPE", "conditional condition requires bool");
        return lub(this.infer(node.then, scope, new Set([...guards, ...guardsTrue(node.condition)])), this.infer(node.otherwise, scope, new Set([...guards, ...guardsFalse(node.condition)])));
      }
      case "call": return this.call(node, scope, guards);
      case "macro": return this.macro(node, scope, guards);
    }
  }

  private select(node: Extract<Expr, { kind: "member" }>, scope: Record<string, MrlType>, guards: Set<string>): MrlType {
    const object = this.infer(node.object, scope, guards);
    const path = pathOf(node); if (path) this.paths.add(path);
    if (object.kind === "record") {
      const field = Object.hasOwn(object.fields, node.field) ? object.fields[node.field] : undefined;
      if (!field) { if (object.open) return DYN; this.error("MRL-NO-FIELD", `record has no field ${node.field}`); return DYN; }
      if (!field.required && (!path || !guards.has(path))) this.warning("MRL-MAYBE-ABSENT", `field ${path ?? node.field} may be absent`);
      return field.type;
    }
    if (object.kind === "map" || object === DYN) return object.kind === "map" ? object.element : DYN;
    this.error("MRL-TYPE", `cannot select field ${node.field} from ${typeName(object)}`); return DYN;
  }

  private index(node: Extract<Expr, { kind: "index" }>, scope: Record<string, MrlType>, guards: Set<string>): MrlType {
    const object = this.infer(node.object, scope, guards); const index = this.infer(node.index, scope, guards);
    if (object.kind === "list") { if (index !== DYN && !isPrimitive(index, "int") && !isPrimitive(index, "num")) this.error("MRL-TYPE", "list index requires int"); return object.element; }
    if (object.kind === "map") { if (index !== DYN && !isPrimitive(index, "string")) this.error("MRL-TYPE", "map index requires string"); return object.element; }
    if (object.kind === "record") {
      if (node.index.kind === "literal" && typeof node.index.value === "string") {
        const field = Object.hasOwn(object.fields, node.index.value) ? object.fields[node.index.value] : undefined;
        if (!field) { if (object.open) return DYN; this.error("MRL-NO-FIELD", `record has no field ${node.index.value}`); return DYN; }
        return field.type;
      }
      if (index !== DYN && !isPrimitive(index, "string")) this.error("MRL-TYPE", "record index requires string");
      return DYN;
    }
    if (object !== DYN) this.error("MRL-TYPE", `cannot index ${typeName(object)}`);
    return DYN;
  }

  private binary(node: Extract<Expr, { kind: "binary" }>, scope: Record<string, MrlType>, guards: Set<string>): MrlType {
    const left = this.infer(node.left, scope, guards);
    if (node.op === "&&" || node.op === "||") {
      const right = this.infer(node.right, scope, new Set([...guards, ...(node.op === "&&" ? guardsTrue(node.left) : guardsFalse(node.left))]));
      if (left !== DYN && !isPrimitive(left, "bool")) this.error("MRL-TYPE", `${node.op} requires bool operands`);
      if (right !== DYN && !isPrimitive(right, "bool")) this.error("MRL-TYPE", `${node.op} requires bool operands`);
      return BOOL;
    }
    const right = this.infer(node.right, scope, guards);
    if (node.op === "==" || node.op === "!=") { if (!comparable(left, right)) this.error("MRL-TYPE", `values ${typeName(left)} and ${typeName(right)} are not comparable`); return BOOL; }
    if (["<", "<=", ">", ">="].includes(node.op)) { if (!((isNumeric(left) && isNumeric(right)) || (isPrimitive(left, "string") && isPrimitive(right, "string")) || left === DYN || right === DYN)) this.error("MRL-TYPE", `values ${typeName(left)} and ${typeName(right)} are not orderable`); return BOOL; }
    if (node.op === "in") {
      if (right.kind === "list") { if (!comparable(left, right.element)) this.error("MRL-TYPE", "in operands are not comparable"); }
      else if (right.kind === "map" || right.kind === "record") { if (left !== DYN && !isPrimitive(left, "string")) this.error("MRL-TYPE", "map membership requires string key"); }
      else if (right !== DYN) this.error("MRL-TYPE", "in requires list or map");
      return BOOL;
    }
    if (node.op === "+" && isPrimitive(left, "string") && isPrimitive(right, "string")) return STRING;
    if (node.op === "+" && (left === DYN || right === DYN)) {
      if (stringType(left) || stringType(right)) return STRING;
      if (left.kind === "list" || right.kind === "list") return list(DYN);
    }
    if (node.op === "+" && left.kind === "list" && right.kind === "list") return list(lub(left.element, right.element));
    if (!["+", "-", "*", "/", "%"].includes(node.op)) { this.error("MRL-SYNTAX", `unknown operator ${node.op}`); return DYN; }
    if (left !== DYN && !isNumeric(left) || right !== DYN && !isNumeric(right)) { this.error("MRL-TYPE", `${node.op} requires numeric operands`); return DYN; }
    if (node.op === "/") return FLOAT;
    if (node.op === "%") { if (left !== DYN && !isPrimitive(left, "int") || right !== DYN && !isPrimitive(right, "int")) this.error("MRL-TYPE", "% requires int operands"); return INT; }
    return numericResult(left === DYN ? NUM : left, right === DYN ? NUM : right);
  }

  private call(node: Extract<Expr, { kind: "call" }>, scope: Record<string, MrlType>, guards: Set<string>): MrlType {
    if (node.name === "has" && !node.receiver) {
      if (node.args.length !== 1 || node.args[0].kind !== "member") this.error("MRL-MACRO", "has requires one field selection");
      else {
        const member = node.args[0];
        const object = this.infer(member.object, scope, guards);
        if (object.kind === "record" && !object.open && !Object.hasOwn(object.fields, member.field)) this.error("MRL-NO-FIELD", "record has no field " + member.field);
        else if (object !== DYN && !mapType(object)) this.error("MRL-TYPE", "has requires a record");
      }
      return BOOL;
    }
    const args = node.args.map((arg) => this.infer(arg, scope, guards));
    if (node.receiver) args.unshift(this.infer(node.receiver, scope, guards));
    const signature = Object.hasOwn(this.functions, node.name) ? this.functions[node.name] : undefined;
    if (!signature) { this.error("MRL-UNDEFINED", `undefined function ${node.name}`); return DYN; }
    if (args.length < signature.minArgs || (signature.maxArgs !== undefined && args.length > signature.maxArgs)) { this.error("MRL-ARITY", `${node.name} received ${args.length} arguments`); return DYN; }
    if (signature.accepts && !signature.accepts(args)) this.error("MRL-TYPE", `arguments to ${node.name} have incompatible types`);
    return signature.inferReturn?.(args) ?? signature.returns;
  }

  private macro(node: Extract<Expr, { kind: "macro" }>, scope: Record<string, MrlType>, guards: Set<string>): MrlType {
    const target = this.infer(node.target, scope, guards);
    const variable = target.kind === "list" ? target.element : target.kind === "map" || target.kind === "record" ? STRING : target === DYN ? DYN : undefined;
    if (!variable) { this.error("MRL-TYPE", `${node.name} requires list or map`); return DYN; }
    if (this.lookup(node.variable, scope)) this.error("MRL-SHADOW", `macro variable ${node.variable} shadows an existing name`);
    const inner = { ...scope, [node.variable]: variable };
    if (node.predicate) { const predicate = this.infer(node.predicate, inner, guards); if (predicate !== DYN && !isPrimitive(predicate, "bool")) this.error("MRL-TYPE", `${node.name} predicate requires bool`); }
    if (node.name === "all" || node.name === "exists" || node.name === "exists_one") return BOOL;
    if (node.name === "filter") return list(variable);
    return list(node.body ? this.infer(node.body, inner, guards) : DYN);
  }
}

export function checkAst(ast: Expr, options: CheckOptions = {}): CheckResult {
  const expected = typeof options.expect === "string" ? normalizeEnvironment({ value: options.expect }).value : options.expect;
  return new Checker(options).run(ast, expected);
}

export function checkExpression(source: string, options: CheckOptions = {}): CheckResult {
  return checkAst(parseExpression(source), options);
}

export function standardSignatures(): Record<string, FunctionSignature> { return { ...signatures }; }
