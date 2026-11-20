import { compareCanonical, equalValues, identityKey, Evaluator, evaluate, isMrlMap, standardFunctions, type EvaluateOptions, type MrlValue } from "./evaluator";
import { floatValue, isInt, isMrlNumber, numericDomainError } from "./numbers";
import { checkAst, MrlStaticError, type FunctionSignature, type MrlDiagnostic } from "./checker";
import { parseExpression, reserved, type Expr } from "./parser";
import { BOOL, INT, NUM, comparable, compatible, isNumeric, isPrimitive, list, parseType, type MrlType } from "./types";

export interface RelationColumn { name: string; type: string; }
export interface RelationDeclaration { name: string; columns: RelationColumn[]; input: boolean; open: boolean; }
export type RuleTerm = { kind: "variable"; name: string } | { kind: "anonymous" } | { kind: "expression"; ast: Expr };
export interface RuleAtom { kind: "atom"; relation: string; args: RuleTerm[]; negated: boolean; }
export interface RuleAssign { kind: "assign"; name: string; expression: Expr; }
export interface RuleGuard { kind: "guard"; expression: Expr; }
export type RuleBody = RuleAtom | RuleAssign | RuleGuard;
type AggregateName = "count" | "sum" | "min" | "max" | "set";
type RuleHead = RuleTerm | { kind: "aggregate"; name: AggregateName; argument?: Expr };
export interface RuleLocation { file: string; line: number; }
export interface RuleClause { relation: string; head: RuleHead[]; body: RuleBody[]; location: RuleLocation; }
export interface RuleModule { name: string; relations: RelationDeclaration[]; rules: RuleClause[]; file?: string; }
interface RuleOptions {
  functions?: EvaluateOptions["functions"];
  signatures?: Record<string, FunctionSignature>;
  inferredTypes?: EvaluateOptions["inferredTypes"];
}

export class MrlRuleError extends Error {
  constructor(readonly code: string, message: string, readonly location?: RuleLocation) {
    super(message);
    this.name = "MrlRuleError";
  }
}

function withRuleLocation(error: unknown, location: RuleLocation): never {
  if (error instanceof MrlRuleError && error.location) throw error;
  const code = error && typeof error === "object" && "code" in error && typeof error.code === "string" ? error.code : "MRL-SYNTAX";
  throw new MrlRuleError(code, error instanceof Error ? error.message : String(error), location);
}

interface Lexeme {
  text: string;
  kind: "identifier" | "string" | "number" | "symbol" | "eof";
  start: number;
  end: number;
}

const variableName = /^[A-Z_][A-Za-z0-9_]*$/;
const aggregateNames = new Set<string>(["count", "sum", "min", "max", "set"]);

function headTerm(term: RuleTerm): RuleHead {
  if (term.kind !== "expression") return term;
  const ast = term.ast;
  if (ast.kind !== "call" || ast.receiver || !aggregateNames.has(ast.name)) return term;
  if (ast.args.length !== (ast.name === "count" ? 0 : 1)) {
    throw new MrlRuleError("MRL-ARITY", "invalid number of arguments to aggregate " + ast.name);
  }
  return { kind: "aggregate", name: ast.name as AggregateName, argument: ast.args[0] };
}

function lex(source: string): Lexeme[] {
  const tokens: Lexeme[] = [];
  let index = 0;
  const push = (kind: Lexeme["kind"], start: number) => {
    tokens.push({ kind, text: source.slice(start, index), start, end: index });
  };
  while (index < source.length) {
    const character = source[index];
    if (/\s/.test(character)) { index += 1; continue; }
    if (source.startsWith("//", index)) { const end = source.indexOf("\n", index + 2); index = end < 0 ? source.length : end + 1; continue; }
    if (/[A-Za-z_]/.test(character)) { const start = index++; while (/[A-Za-z0-9_]/.test(source[index] ?? "")) index += 1; push("identifier", start); continue; }
    if (/[0-9]/.test(character) || (character === "-" && /[0-9]/.test(source[index + 1] ?? ""))) {
      const start = index;
      if (source[index] === "-") index += 1;
      while (/[0-9]/.test(source[index] ?? "")) index += 1;
      if (source[index] === "." && /[0-9]/.test(source[index + 1] ?? "")) { index += 1; while (/[0-9]/.test(source[index] ?? "")) index += 1; }
      if (source[index] === "e" || source[index] === "E") { index += 1; if (source[index] === "+" || source[index] === "-") index += 1; while (/[0-9]/.test(source[index] ?? "")) index += 1; }
      push("number", start); continue;
    }
    if (character === "\"" || character === "'") {
      const quote = character; const start = index++;
      while (index < source.length) { if (source[index] === "\\") index += 2; else if (source[index++] === quote) break; }
      if (source[index - 1] !== quote) throw new MrlRuleError("MRL-SYNTAX", "unterminated rule string");
      push("string", start); continue;
    }
    const pair = source.slice(index, index + 2);
    const start = index;
    index += [":-", "==", "!=", "<=", ">=", "&&", "||"].includes(pair) ? 2 : 1;
    push("symbol", start);
  }
  push("eof", index);
  return tokens;
}

class RuleParser {
  private position = 0;
  private line = 1;
  private lastRuleOffset = 0;
  constructor(private readonly tokens: Lexeme[], private readonly source: string, private readonly file: string, private readonly functions = new Set<string>()) {}
  private get token() { return this.tokens[this.position]; }
  private take(text: string) { if (this.token.text !== text) throw new MrlRuleError("MRL-SYNTAX", `expected ${text}, got ${this.token.text || "eof"}`); this.position += 1; }
  private optional(text: string) { if (this.token.text !== text) return false; this.position += 1; return true; }
  private identifier() { if (this.token.kind !== "identifier") throw new MrlRuleError("MRL-SYNTAX", "expected identifier"); return this.tokens[this.position++].text; }
  private relationName(): string {
    const name = this.identifier();
    if (reserved.has(name) || ["in", "true", "false", "null"].includes(name)) throw new MrlRuleError("MRL-SYNTAX", "invalid relation name " + name);
    return name;
  }
  private endsStatement(token: Lexeme): boolean {
    return token.text === "." && (token.end === this.source.length || /\s/.test(this.source[token.end]) || this.source.startsWith("//", token.end));
  }
  private endStatement() {
    if (!this.endsStatement(this.token)) throw new MrlRuleError("MRL-SYNTAX", "statement must end with a dot followed by whitespace");
    this.position += 1;
  }
  private typeRef(): string {
    const name = this.identifier();
    if (["bool", "int", "float", "string", "any"].includes(name)) return name;
    if (name !== "list" && name !== "map") throw new MrlRuleError("MRL-DECL", "invalid column type " + name);
    this.take("<");
    const inner = this.typeRef(); this.take(">"); return `${name}<${inner}>`;
  }
  parse(): RuleModule {
    this.take("module");
    const moduleParts = [this.identifier()];
    while (this.token.text === "." && !this.endsStatement(this.token)) {
      this.take(".");
      moduleParts.push(this.identifier());
    }
    this.endStatement();
    const name = moduleParts.join(".");
    const relations: RelationDeclaration[] = []; const rules: RuleClause[] = [];
    while (this.token.kind !== "eof") {
      const annotations = new Set<string>();
      while (this.optional("@")) {
        const annotation = this.identifier();
        if (!["input", "open", "doc"].includes(annotation) || annotations.has(annotation)) {
          throw new MrlRuleError("MRL-DECL", "invalid relation annotation " + annotation);
        }
        annotations.add(annotation);
        if (annotation === "doc") {
          this.take("(");
          if (this.token.kind !== "string") throw new MrlRuleError("MRL-DECL", "@doc requires a string");
          parseExpression(this.token.text);
          this.position += 1;
          this.take(")");
        }
      }
      if (this.optional("rel")) {
        const relation = this.relationName();
        this.take("(");
        const columns: RelationColumn[] = [];
        const columnNames = new Set<string>();
        while (this.token.text !== ")") {
          const column = this.identifier();
          if (columnNames.has(column)) throw new MrlRuleError("MRL-DECL", "duplicate relation column " + column);
          columnNames.add(column);
          this.take(":");
          columns.push({ name: column, type: this.typeRef() });
          if (!this.optional(",")) break;
        }
        this.take(")");
        const input = annotations.has("input");
        const open = annotations.has("open");
        if (input && open) throw new MrlRuleError("MRL-DECL", "a relation cannot be both input and open");
        this.endStatement(); relations.push({ name: relation, columns, input, open }); continue;
      }
      if (annotations.size) throw new MrlRuleError("MRL-DECL", "relation annotation must precede rel declaration");
      rules.push(this.rule());
    }
    return { name, relations, rules, file: this.file };
  }
  private rule(): RuleClause {
    const start = this.token.start;
    for (let offset = this.lastRuleOffset; offset < start; offset += 1) {
      if (this.source[offset] === "\n") this.line += 1;
    }
    this.lastRuleOffset = start;
    const location = { file: this.file, line: this.line };
    const relation = this.relationName(); this.take("("); const head = this.terms(")").map(headTerm); this.take(")");
    const body: RuleBody[] = [];
    if (this.optional(":-")) {
      while (true) { body.push(this.bodyItem()); if (!this.optional(",")) break; }
    }
    this.endStatement(); return { relation, head, body, location };
  }
  private terms(end: string): RuleTerm[] {
    const terms: RuleTerm[] = [];
    if (this.token.text === end) return terms;
    while (true) {
      terms.push(this.term(end === ")" ? [",", ")"] : [",", "]"]));
      if (this.optional(",")) continue;
      break;
    }
    return terms;
  }
  private term(stop: string[]): RuleTerm {
    const next = this.tokens[this.position + 1];
    if (next && stop.includes(next.text)) {
      if (this.optional("_")) return { kind: "anonymous" };
      if (this.token.kind === "identifier" && variableName.test(this.token.text)) return { kind: "variable", name: this.identifier() };
    }
    const expression = this.captureExpression(stop);
    return { kind: "expression", ast: parseExpression(expression) };
  }
  private captureExpression(stop: string[]): string {
    const start = this.token.start;
    let end = start;
    let depth = 0;
    while (this.token.kind !== "eof") {
      if (depth === 0 && stop.includes(this.token.text) && (this.token.text !== "." || this.endsStatement(this.token))) break;
      if (["(", "[", "{"].includes(this.token.text)) depth += 1;
      if ([")", "]", "}"].includes(this.token.text)) depth -= 1;
      end = this.token.end;
      this.position += 1;
    }
    if (end === start) throw new MrlRuleError("MRL-SYNTAX", "expected rule term");
    return this.source.slice(start, end);
  }
  private startsAtom(position: number): boolean {
    const relation = this.tokens[position];
    if (relation?.kind !== "identifier" || this.tokens[position + 1]?.text !== "("
      || relation.text === "has" || Object.hasOwn(standardFunctions, relation.text) || this.functions.has(relation.text)) return false;
    let depth = 1;
    for (let index = position + 2; index < this.tokens.length; index += 1) {
      if (this.tokens[index].text === "(") depth += 1;
      if (this.tokens[index].text === ")") depth -= 1;
      if (depth !== 0) continue;
      const next = this.tokens[index + 1];
      return next?.text === "," || (next !== undefined && this.endsStatement(next));
    }
    return false;
  }
  private bodyItem(): RuleBody {
    const negated = this.token.text === "!";
    if (this.startsAtom(this.position + Number(negated))) {
      if (negated) this.take("!");
      const relation = this.relationName();
      this.take("(");
      const args = this.terms(")");
      this.take(")");
      return { kind: "atom", relation, args, negated };
    }
    if (this.token.kind === "identifier" && variableName.test(this.token.text) && this.tokens[this.position + 1]?.text === "=") {
      const name = this.identifier();
      this.take("=");
      return { kind: "assign", name, expression: parseExpression(this.captureExpression([",", "."])) };
    }
    const expression = this.captureExpression([",", "."]);
    return { kind: "guard", expression: parseExpression(expression) };
  }
}

export function parseRuleModule(source: string, name = "", functions = new Set<string>()): RuleModule {
  try {
    return new RuleParser(lex(source), source, name, functions).parse();
  } catch (error) {
    return withRuleLocation(error, { file: name, line: 1 });
  }
}
export function parseRuleModules(sources: Array<{ name: string; source: string }>, functions = new Set<string>()): RuleModule[] {
  return sources.map(({ source, name }) => parseRuleModule(source, name, functions));
}

type Row = MrlValue[];
type Binding = Record<string, MrlValue>;

class RelationIndex {
  private readonly indexes = new WeakMap<Row[], Map<number, { size: number; buckets: Map<string, Row[]> }>>();

  candidates(rows: Row[], lookup: (MrlValue | undefined)[]): Row[] {
    if (rows.length < 8) return rows;
    let candidates = rows;
    for (const [column, value] of lookup.entries()) {
      if (value === undefined) continue;
      const matches = this.bucket(rows, column, identityKey(value));
      if (!matches?.length) return [];
      if (matches.length < candidates.length) candidates = matches;
    }
    return candidates;
  }

  private bucket(rows: Row[], column: number, value: string): Row[] | undefined {
    let columns = this.indexes.get(rows);
    if (!columns) this.indexes.set(rows, columns = new Map());
    let index = columns.get(column);
    if (!index) columns.set(column, index = { size: 0, buckets: new Map() });
    while (index.size < rows.length) {
      const row = rows[index.size++];
      const key = identityKey(row[column]);
      let matches = index.buckets.get(key);
      if (!matches) index.buckets.set(key, matches = []);
      matches.push(row);
    }
    return index.buckets.get(value);
  }
}

function evaluateBinding(expression: Expr, binding: Binding, options: RuleOptions): MrlValue {
  return new Evaluator(options).evaluate(expression, binding, false);
}

function termValue(term: RuleTerm, binding: Binding, options: RuleOptions): MrlValue | undefined {
  if (term.kind === "anonymous") return undefined;
  if (term.kind === "variable") return binding[term.name];
  return evaluateBinding(term.ast, binding, options);
}

function same(a: MrlValue | undefined, b: MrlValue): boolean { return a !== undefined && identityKey(a) === identityKey(b); }

function requireType(actual: MrlType, wanted: MrlType, subject: string): void {
  if (compatible(actual, wanted) === "no") throw new MrlRuleError("MRL-TYPE", "incompatible type in " + subject);
}

function ruleExpressionType(ast: Expr, bindings: Record<string, MrlType>, options: RuleOptions): MrlType {
  try {
    return checkAst(ast, { environment: bindings, functions: options.signatures, inferredTypes: options.inferredTypes }).type;
  } catch (error) {
    if (error instanceof MrlStaticError) {
      const issue = error.diagnostics.find((item) => item.severity === "error");
      const unknownVariable = issue?.code === "MRL-UNDEFINED" && issue.message.match(/^undefined name ([A-Z_][A-Za-z0-9_]*)$/);
      if (unknownVariable) throw new MrlRuleError("MRL-UNSAFE", "unbound variable " + unknownVariable[1]);
      if (issue) throw new MrlRuleError(issue.code, issue.message);
    }
    throw error;
  }
}

function checkTerm(term: RuleTerm, bindings: Record<string, MrlType>, options: RuleOptions): MrlType {
  if (term.kind === "expression") return ruleExpressionType(term.ast, bindings, options);
  if (term.kind === "variable" && Object.hasOwn(bindings, term.name)) return bindings[term.name];
  throw new MrlRuleError("MRL-UNSAFE", term.kind === "anonymous" ? "anonymous variable in rule head" : "unbound variable " + term.name);
}

function checkHead(term: RuleHead, bindings: Record<string, MrlType>, options: RuleOptions): MrlType {
  if (term.kind !== "aggregate") return checkTerm(term, bindings, options);
  if (term.name === "count") return INT;
  const type = ruleExpressionType(term.argument!, bindings, options);
  if (term.name === "set") return list(type);
  if (isNumeric(type)) return type;
  if (isPrimitive(type, "dyn")) return term.name === "sum" ? NUM : type;
  if (term.name !== "sum" && isPrimitive(type, "string")) return type;
  throw new MrlRuleError("MRL-TYPE", "invalid argument to aggregate " + term.name);
}

function normalizeValue(value: MrlValue, type: MrlType): MrlValue | undefined {
  if (type.kind === "list" || (type.kind === "primitive" && type.name === "dyn" && Array.isArray(value))) {
    return Array.isArray(value) ? normalizeRow(value, () => type.kind === "list" ? type.element : type) : undefined;
  }
  if (type.kind === "map" || (type.kind === "primitive" && type.name === "dyn" && isMrlMap(value))) {
    if (!isMrlMap(value)) return undefined;
    const result: [string, MrlValue][] = [];
    for (const [key, item] of Object.entries(value)) {
      const normalized = normalizeValue(item, type.kind === "map" ? type.element : type);
      if (normalized === undefined) return undefined;
      result.push([key, normalized]);
    }
    return Object.fromEntries(result);
  }
  if (type.kind !== "primitive") return undefined;
  if (numericDomainError(value)) return undefined;
  switch (type.name) {
    case "bool": return typeof value === "boolean" ? value : undefined;
    case "string": return typeof value === "string" ? value : undefined;
    case "float": return isMrlNumber(value) ? isInt(value) ? floatValue(Number(value)) : value : undefined;
    case "int": return isInt(value) ? value : undefined;
    case "dyn": return value === null || typeof value === "boolean" || typeof value === "string" || isMrlNumber(value) ? value : undefined;
    default: return undefined;
  }
}

function normalizeRow(row: Row, column: (index: number) => MrlType): Row | undefined {
  const result: Row = [];
  for (const [index, value] of row.entries()) {
    const normalized = normalizeValue(value, column(index));
    if (normalized === undefined) return undefined;
    result.push(normalized);
  }
  return result;
}

function strataFor(relations: Iterable<string>, rules: RuleClause[]): RuleClause[][] {
  const levels = new Map(Array.from(relations, (name) => [name, 0]));
  const edges = rules.flatMap((rule) => {
    const aggregate = rule.head.some((term) => term.kind === "aggregate");
    return rule.body.flatMap((item) => item.kind === "atom"
      ? [{ target: rule.relation, source: item.relation, strict: Number(item.negated || aggregate) }] : []);
  });
  for (let round = 0; round < levels.size; round += 1) {
    let changed = false;
    for (const { source, target, strict } of edges) {
      const minimum = levels.get(source)! + strict;
      if (levels.get(target)! >= minimum) continue;
      levels.set(target, minimum);
      changed = true;
    }
    if (!changed) break;
    if (round === levels.size - 1) throw new MrlRuleError("MRL-STRATIFY", "rule dependency cycle uses negation or aggregation");
  }
  const positive = new Map<string, string[]>();
  for (const { source, target, strict } of edges) {
    if (strict || levels.get(source) !== levels.get(target)) continue;
    const targets = positive.get(source);
    if (targets) targets.push(target);
    else positive.set(source, [target]);
  }
  const reachable = new Map<string, Set<string>>();
  const dependsOn = (source: string, target: string): boolean => {
    let visited = reachable.get(source);
    if (!visited) {
      visited = new Set([source]);
      const pending = [source];
      for (let index = 0; index < pending.length; index += 1) {
        for (const next of positive.get(pending[index]) ?? []) {
          if (visited.has(next)) continue;
          visited.add(next);
          pending.push(next);
        }
      }
      reachable.set(source, visited);
    }
    return visited.has(target);
  };
  for (const rule of rules) {
    const constructsValue = rule.head.some((term) => term.kind === "expression" && term.ast.kind !== "literal")
      || rule.body.some((item) => item.kind === "assign" && item.expression.kind !== "identifier" && item.expression.kind !== "literal");
    if (constructsValue && rule.body.some((item) => item.kind === "atom" && !item.negated && dependsOn(rule.relation, item.relation))) {
      throw new MrlRuleError("MRL-INVENTION", "recursive rules can only place variables or literals in heads and assignments", rule.location);
    }
  }
  const strata: RuleClause[][] = [];
  for (const rule of rules) (strata[levels.get(rule.relation)!] ??= []).push(rule);
  return strata.filter((stratum) => stratum.length);
}

function aggregateRows(head: RuleHead[], bindings: Iterable<Binding>, options: RuleOptions): Row[] {
  const seen = new Set<string>();
  const groups = new Map<string, { values: Row; arguments: Binding[] }>();
  for (const binding of bindings) {
    const identity = identityKey(binding);
    if (seen.has(identity)) continue;
    seen.add(identity);
    const values = head.map((term) => term.kind === "aggregate" ? null : termValue(term, binding, options)!);
    const key = identityKey(values);
    let group = groups.get(key);
    if (!group) {
      group = { values, arguments: [] };
      groups.set(key, group);
    }
    group.arguments.push(binding);
  }
  if (!groups.size && head.every((term) => term.kind === "aggregate" && term.name !== "min" && term.name !== "max")) {
    groups.set("", { values: [], arguments: [] });
  }
  return [...groups.values()].map((group) => head.map((term, index) => {
    if (term.kind !== "aggregate") return group.values[index];
    if (term.name === "count") return group.arguments.length;
    const values = group.arguments.map((binding) => evaluateBinding(term.argument!, binding, options)).sort(compareCanonical);
    if (term.name === "set") return standardFunctions.unique(values);
    return standardFunctions[term.name](values);
  }));
}

export class RuleProgram {
  readonly relations = new Map<string, RelationDeclaration>();
  readonly rules: RuleClause[];
  readonly warnings: MrlDiagnostic[];
  private readonly columnTypes = new Map<string, MrlType[]>();
  private readonly strata: RuleClause[][];
  private readonly options: RuleOptions;
  constructor(readonly modules: RuleModule[], options: RuleOptions = {}) {
    this.options = { ...options, inferredTypes: new WeakMap() };
    const owners = new Map<string, string>();
    const moduleNames = new Set<string>();
    for (const module of modules) {
      if (moduleNames.has(module.name)) throw new MrlRuleError("MRL-DUP", "duplicate module " + module.name, { file: module.file ?? "", line: 1 });
      moduleNames.add(module.name);
      for (const relation of module.relations) {
        if (this.relations.has(relation.name) || Object.hasOwn(standardFunctions, relation.name)
          || Object.hasOwn(options.signatures ?? {}, relation.name) || relation.name === "has") {
          throw new MrlRuleError("MRL-DUP", "duplicate relation or function " + relation.name, { file: module.file ?? "", line: 1 });
        }
        this.relations.set(relation.name, relation);
        this.columnTypes.set(relation.name, relation.columns.map((column) => parseType(column.type)));
        owners.set(relation.name, module.name);
      }
    }
    this.rules = modules.flatMap((module) => module.rules);
    for (const module of modules) {
      for (const rule of module.rules) {
        try {
          const head = this.signature(rule.relation, rule.head.length);
          if (head.input || (!head.open && owners.get(head.name) !== module.name)) {
            throw new MrlRuleError("MRL-OWNERSHIP", "cannot write relation " + head.name + " in " + module.name);
          }
          this.checkClause(rule);
        } catch (error) {
          withRuleLocation(error, rule.location);
        }
      }
    }
    this.strata = strataFor(this.relations.keys(), this.rules);
    const definedRelations = new Set(this.rules.map((rule) => rule.relation));
    this.warnings = [...this.relations.values()]
      .filter(({ input, open, name }) => !input && !open && !definedRelations.has(name))
      .map(({ name }) => ({ code: "MRL-UNUSED", severity: "warning", message: `relation ${name} has no facts or rules` }));
  }
  private signature(name: string, arity: number): RelationDeclaration {
    const declaration = this.relations.get(name);
    if (!declaration) throw new MrlRuleError("MRL-UNDEFINED", "undefined relation " + name);
    if (arity !== declaration.columns.length) throw new MrlRuleError("MRL-ARITY", "relation " + name + " arity mismatch");
    return declaration;
  }
  private checkClause(rule: RuleClause): void {
    const bindings: Record<string, MrlType> = Object.create(null);
    for (const item of rule.body) {
      if (item.kind === "guard") {
        requireType(ruleExpressionType(item.expression, bindings, this.options), BOOL, "rule guard");
        continue;
      }
      if (item.kind === "assign") {
        if (item.name === "_") throw new MrlRuleError("MRL-UNSAFE", "cannot assign to anonymous variable");
        const type = ruleExpressionType(item.expression, bindings, this.options);
        const before = bindings[item.name];
        if (before && !comparable(before, type)) throw new MrlRuleError("MRL-TYPE", "incomparable variable assignment");
        if (!before) bindings[item.name] = type;
        continue;
      }
      const source = this.signature(item.relation, item.args.length);
      if (source.open) throw new MrlRuleError("MRL-OWNERSHIP", "cannot read open relation " + item.relation);
      const columns = this.columnTypes.get(item.relation)!;
      const next: Record<string, MrlType> = Object.create(null);
      for (const [index, term] of item.args.entries()) {
        if (term.kind === "anonymous") continue;
        const type = columns[index];
        if (term.kind === "variable" && !item.negated && !bindings[term.name]) {
          if (next[term.name]) requireType(next[term.name], type, item.relation);
          else next[term.name] = type;
          continue;
        }
        requireType(checkTerm(term, bindings, this.options), type, item.relation);
      }
      Object.assign(bindings, next);
    }
    const headTypes = this.columnTypes.get(rule.relation)!;
    for (const [index, term] of rule.head.entries()) requireType(checkHead(term, bindings, this.options), headTypes[index], rule.relation);
  }
  evaluate(facts: Record<string, Row[]> = {}, budget = 100_000, onDiagnosticRule?: (location: RuleLocation) => void): RuleModel {
    const rows = new Map<string, Row[]>();
    const keys = new Map<string, Set<string>>();
    for (const name of this.relations.keys()) rows.set(name, []);
    for (const [name, input] of Object.entries(facts)) {
      const columns = this.columnTypes.get(name);
      if (!this.relations.get(name)?.input || !columns || !Array.isArray(input)) {
        throw new MrlRuleError("MRL-FACT", "invalid input relation " + name);
      }
      const seen = new Set<string>();
      const distinct: Row[] = [];
      for (const row of input) {
        const tuple = Array.isArray(row) && row.length === columns.length ? normalizeRow(row, (index) => columns[index]) : undefined;
        if (!tuple) {
          throw new MrlRuleError("MRL-FACT", "invalid input tuple for " + name);
        }
        const key = identityKey(tuple);
        if (seen.has(key)) continue;
        seen.add(key);
        distinct.push(tuple);
      }
      rows.set(name, distinct);
      keys.set(name, seen);
    }
    let steps = 0;
    const indexes = new RelationIndex();
    for (const stratum of this.strata) {
      let changed = true;
      while (changed) {
        changed = false;
        for (const rule of stratum) {
          const existing = rows.get(rule.relation)!;
          let seen = keys.get(rule.relation);
          if (!seen) {
            seen = new Set();
            keys.set(rule.relation, seen);
          }
          const columns = this.columnTypes.get(rule.relation)!;
          const aggregate = rule.head.some((term) => term.kind === "aggregate");
          const bindings = this.bindBody(rule.body, rows, indexes, {}, aggregate);
          const counted = (function* () {
            for (const binding of bindings) {
              steps += 1;
              if (steps > budget) throw new MrlRuleError("MRL-COST", "rule budget " + budget + " exceeded");
              yield binding;
            }
          })();
          const options = this.options;
          const tuples = aggregate ? aggregateRows(rule.head, counted, options) : (function* () {
            for (const binding of counted) yield rule.head.map((term) => termValue(term as RuleTerm, binding, options)!);
          })();
          try {
            for (const raw of tuples) {
              const tuple = normalizeRow(raw, (index) => columns[index]);
              if (!tuple) {
                throw new MrlRuleError("MRL-TYPE", "derived value does not fit relation " + rule.relation);
              }
              if (rule.relation === "diag") onDiagnosticRule?.(rule.location);
              const key = identityKey(tuple);
              if (seen.has(key)) continue;
              seen.add(key);
              existing.push(tuple);
              changed = true;
            }
          } catch (error) {
            withRuleLocation(error, rule.location);
          }
        }
      }
    }
    return new RuleModel(this, rows);
  }
  private *bindBody(body: RuleBody[], rows: Map<string, Row[]>, indexes: RelationIndex, binding: Binding, aggregate: boolean, position = 0): Generator<Binding> {
    const item = body[position];
    if (!item) { yield binding; return; }
    if (item.kind === "guard") {
      const result = evaluateBinding(item.expression, binding, this.options);
      if (typeof result !== "boolean") throw new MrlRuleError("MRL-TYPE", "rule guard must be bool");
      if (result) yield* this.bindBody(body, rows, indexes, binding, aggregate, position + 1);
      return;
    }
    if (item.kind === "assign") {
      const value = evaluateBinding(item.expression, binding, this.options);
      if (Object.hasOwn(binding, item.name)) {
        if (equalValues(binding[item.name], value)) yield* this.bindBody(body, rows, indexes, binding, aggregate, position + 1);
      } else yield* this.bindBody(body, rows, indexes, { ...binding, [item.name]: value }, aggregate, position + 1);
      return;
    }
    const relationRows = rows.get(item.relation); if (!relationRows) throw new MrlRuleError("MRL-UNDEFINED", `undefined relation ${item.relation}`);
    const columns = this.columnTypes.get(item.relation)!;
    const lookup: (MrlValue | undefined)[] = [];
    for (const [index, term] of item.args.entries()) {
      const value = termValue(term, binding, this.options);
      const normalized = value === undefined ? undefined : normalizeValue(value, columns[index]);
      if (value !== undefined && normalized === undefined) {
        if (item.negated) yield* this.bindBody(body, rows, indexes, binding, aggregate, position + 1);
        return;
      }
      lookup.push(normalized);
    }
    const candidates = indexes.candidates(relationRows, lookup);
    if (item.negated) {
      const exists = candidates.some((row) => lookup.every((value, index) => value === undefined || same(value, row[index])));
      if (!exists) yield* this.bindBody(body, rows, indexes, binding, aggregate, position + 1);
      return;
    }
    for (const row of candidates) {
      if (row.length !== item.args.length) throw new MrlRuleError("MRL-ARITY", `relation ${item.relation} arity mismatch`);
      const next = { ...binding }; let valid = true;
      for (let index = 0; index < item.args.length; index += 1) {
        const term = item.args[index]; const value = row[index];
        if (lookup[index] !== undefined) {
          if (!same(lookup[index], value)) { valid = false; break; }
        } else if (term.kind === "variable") {
          if (Object.hasOwn(next, term.name) && !same(next[term.name], value)) { valid = false; break; }
          else Object.defineProperty(next, term.name, { value, enumerable: true, configurable: true, writable: true });
        } else if (term.kind === "anonymous" && aggregate) next["#anonymous:" + position + ":" + index] = value;
      }
      if (valid) yield* this.bindBody(body, rows, indexes, next, aggregate, position + 1);
    }
  }
}

export class RuleModel {
  constructor(private readonly program: RuleProgram, private readonly rows: Map<string, Row[]>) {}
  rowsOf(name: string): Row[] { if (!this.program.relations.has(name)) throw new MrlRuleError("MRL-UNDEFINED", `undefined relation ${name}`); return (this.rows.get(name) ?? []).map((row) => [...row]).sort(compareCanonical); }
  contains(name: string, row: Row): boolean { return this.rowsOf(name).some((candidate) => same(candidate, row)); }
  query(name: string, predicate = "true"): Row[] { return this.rowsOf(name).filter((row) => evaluate(predicate, Object.fromEntries(row.map((value, index) => [`_${index}`, value])) ) === true); }
}

export function buildRuleProgram(sources: Array<{ name: string; source: string }>, options: RuleOptions = {}): RuleProgram {
  return new RuleProgram(parseRuleModules(sources, new Set(Object.keys(options.signatures ?? {}))), options);
}
