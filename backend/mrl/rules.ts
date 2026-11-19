import { evaluate, type MrlValue } from "./evaluator";
import { parseExpression, type Expr } from "./parser";

export interface RelationColumn { name: string; type: string; }
export interface RelationDeclaration { name: string; columns: RelationColumn[]; input: boolean; open: boolean; }
export type RuleTerm = { kind: "variable"; name: string } | { kind: "anonymous" } | { kind: "expression"; ast: Expr };
export interface RuleAtom { kind: "atom"; relation: string; args: RuleTerm[]; negated: boolean; }
export interface RuleAssign { kind: "assign"; name: string; expression: Expr; }
export interface RuleGuard { kind: "guard"; expression: Expr; }
export type RuleBody = RuleAtom | RuleAssign | RuleGuard;
export interface RuleClause { relation: string; head: RuleTerm[]; body: RuleBody[]; }
export interface RuleModule { name: string; relations: RelationDeclaration[]; rules: RuleClause[]; }

export class MrlRuleError extends Error {
  constructor(readonly code: string, message: string) { super(message); this.name = "MrlRuleError"; }
}

interface Lexeme { text: string; kind: "identifier" | "string" | "number" | "symbol" | "eof"; }

function lex(source: string): Lexeme[] {
  const tokens: Lexeme[] = [];
  let index = 0;
  while (index < source.length) {
    const character = source[index];
    if (/\s/.test(character)) { index += 1; continue; }
    if (source.startsWith("//", index)) { const end = source.indexOf("\n", index + 2); index = end < 0 ? source.length : end + 1; continue; }
    if (/[A-Za-z_]/.test(character)) { const start = index++; while (/[A-Za-z0-9_]/.test(source[index] ?? "")) index += 1; tokens.push({ text: source.slice(start, index), kind: "identifier" }); continue; }
    if (/[0-9]/.test(character) || (character === "-" && /[0-9]/.test(source[index + 1] ?? ""))) {
      const start = index;
      if (source[index] === "-") index += 1;
      while (/[0-9]/.test(source[index] ?? "")) index += 1;
      if (source[index] === "." && /[0-9]/.test(source[index + 1] ?? "")) { index += 1; while (/[0-9]/.test(source[index] ?? "")) index += 1; }
      if (source[index] === "e" || source[index] === "E") { index += 1; if (source[index] === "+" || source[index] === "-") index += 1; while (/[0-9]/.test(source[index] ?? "")) index += 1; }
      tokens.push({ text: source.slice(start, index), kind: "number" }); continue;
    }
    if (character === "\"" || character === "'") {
      const quote = character; const start = index++;
      while (index < source.length) { if (source[index] === "\\") index += 2; else if (source[index++] === quote) break; }
      if (source[index - 1] !== quote) throw new MrlRuleError("MRL-SYNTAX", "unterminated rule string");
      tokens.push({ text: source.slice(start, index), kind: "string" }); continue;
    }
    const pair = source.slice(index, index + 2);
    if ([":-", "==", "!=", "<=", ">=", "&&", "||"].includes(pair)) { tokens.push({ text: pair, kind: "symbol" }); index += 2; continue; }
    tokens.push({ text: character, kind: "symbol" }); index += 1;
  }
  tokens.push({ text: "", kind: "eof" });
  return tokens;
}

class RuleParser {
  private position = 0;
  constructor(private readonly tokens: Lexeme[], private readonly source: string) {}
  private get token() { return this.tokens[this.position]; }
  private take(text: string) { if (this.token.text !== text) throw new MrlRuleError("MRL-SYNTAX", `expected ${text}, got ${this.token.text || "eof"}`); this.position += 1; }
  private optional(text: string) { if (this.token.text !== text) return false; this.position += 1; return true; }
  private identifier() { if (this.token.kind !== "identifier") throw new MrlRuleError("MRL-SYNTAX", "expected identifier"); return this.tokens[this.position++].text; }
  private typeRef(): string {
    const name = this.identifier();
    if (!this.optional("<")) return name;
    const inner = this.typeRef(); this.take(">"); return `${name}<${inner}>`;
  }
  parse(): RuleModule {
    this.take("module"); const name = this.identifier(); this.take(".");
    const relations: RelationDeclaration[] = []; const rules: RuleClause[] = [];
    while (this.token.kind !== "eof") {
      let input = false; let open = false;
      while (this.optional("@")) { const annotation = this.identifier(); if (annotation === "input") input = true; else if (annotation === "open") open = true; else throw new MrlRuleError("MRL-DECL", `unknown relation annotation ${annotation}`); }
      if (this.optional("rel")) {
        const relation = this.identifier(); this.take("("); const columns: RelationColumn[] = [];
        while (this.token.text !== ")") {
          const column = this.identifier(); this.take(":"); columns.push({ name: column, type: this.typeRef() });
          if (!this.optional(",")) break;
        }
        this.take(")");
        this.take("."); relations.push({ name: relation, columns, input, open }); continue;
      }
      if (input || open) throw new MrlRuleError("MRL-SYNTAX", "relation annotation must precede rel declaration");
      rules.push(this.rule());
    }
    return { name, relations, rules };
  }
  private rule(): RuleClause {
    const relation = this.identifier(); this.take("("); const head = this.terms(")"); this.take(")");
    const body: RuleBody[] = [];
    if (this.optional(":-")) {
      while (true) { body.push(this.bodyItem()); if (!this.optional(",")) break; }
    }
    this.take("."); return { relation, head, body };
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
    if (this.optional("_")) return { kind: "anonymous" };
    if (this.token.kind === "identifier" && /^[A-Z_]/.test(this.token.text) && this.tokens[this.position + 1]?.text !== "(") return { kind: "variable", name: this.identifier() };
    const expression = this.captureExpression(stop);
    return { kind: "expression", ast: parseExpression(expression) };
  }
  private captureExpression(stop: string[]): string {
    const parts: string[] = []; let depth = 0;
    while (this.token.kind !== "eof") {
      if (depth === 0 && stop.includes(this.token.text)) break;
      if (["(", "[", "{"].includes(this.token.text)) depth += 1;
      if ([")", "]", "}"].includes(this.token.text)) depth -= 1;
      parts.push(this.token.text); this.position += 1;
    }
    if (!parts.length) throw new MrlRuleError("MRL-SYNTAX", "expected rule term");
    return parts.join(" ");
  }
  private bodyItem(): RuleBody {
    if (this.optional("!")) { const relation = this.identifier(); this.take("("); const args = this.terms(")"); this.take(")"); return { kind: "atom", relation, args, negated: true }; }
    if (this.token.kind === "identifier" && this.tokens[this.position + 1]?.text === "(") {
      const relation = this.identifier(); this.take("("); const args = this.terms(")"); this.take(")"); return { kind: "atom", relation, args, negated: false };
    }
    const expression = this.captureExpression([",", "."]);
    const equals = expression.indexOf("=");
    if (equals > 0 && !/[!<>]=/.test(expression.slice(Math.max(0, equals - 1), equals + 1))) {
      const name = expression.slice(0, equals).trim();
      if (/^[A-Z_][A-Za-z0-9_]*$/.test(name)) return { kind: "assign", name, expression: parseExpression(expression.slice(equals + 1).trim()) };
    }
    return { kind: "guard", expression: parseExpression(expression) };
  }
}

export function parseRuleModule(source: string): RuleModule { return new RuleParser(lex(source), source).parse(); }
export function parseRuleModules(sources: Array<{ name: string; source: string }>): RuleModule[] { return sources.map(({ source }) => parseRuleModule(source)); }

type Row = MrlValue[];
type Binding = Record<string, MrlValue>;

function termValue(term: RuleTerm, binding: Binding): MrlValue | undefined {
  if (term.kind === "anonymous") return undefined;
  if (term.kind === "variable") return binding[term.name];
  return evaluate(term.ast, binding);
}

function same(a: MrlValue | undefined, b: MrlValue): boolean { return JSON.stringify(a) === JSON.stringify(b); }

export class RuleProgram {
  readonly relations = new Map<string, RelationDeclaration>();
  readonly rules: RuleClause[];
  constructor(readonly modules: RuleModule[]) {
    for (const module of modules) for (const relation of module.relations) {
      if (this.relations.has(relation.name)) throw new MrlRuleError("MRL-DUP", `duplicate relation ${relation.name}`);
      this.relations.set(relation.name, relation);
    }
    this.rules = modules.flatMap((module) => module.rules);
    for (const rule of this.rules) {
      if (!this.relations.has(rule.relation)) throw new MrlRuleError("MRL-UNDEFINED", `undefined relation ${rule.relation}`);
      for (const body of rule.body) if (body.kind === "atom" && !this.relations.has(body.relation)) throw new MrlRuleError("MRL-UNDEFINED", `undefined relation ${body.relation}`);
    }
  }
  evaluate(facts: Record<string, Row[]> = {}, budget = 100_000): RuleModel {
    const rows = new Map<string, Row[]>();
    for (const [name, relation] of this.relations) rows.set(name, relation.input ? (facts[name] ?? []).map((row) => [...row]) : []);
    let steps = 0; let changed = true;
    while (changed) {
      changed = false;
      for (const rule of this.rules) {
        const existing = rows.get(rule.relation)!;
        for (const binding of this.bindBody(rule.body, rows)) {
          steps += 1; if (steps > budget) throw new MrlRuleError("MRL-COST", `rule budget ${budget} exceeded`);
          const tuple = rule.head.map((term) => termValue(term, binding) ?? null);
          if (!existing.some((row) => JSON.stringify(row) === JSON.stringify(tuple))) { existing.push(tuple); changed = true; }
        }
      }
    }
    return new RuleModel(this, rows);
  }
  private *bindBody(body: RuleBody[], rows: Map<string, Row[]>, binding: Binding = {}): Generator<Binding> {
    const [item, ...rest] = body;
    if (!item) { yield binding; return; }
    if (item.kind === "guard") { if (evaluate(item.expression, binding) === true) yield* this.bindBody(rest, rows, binding); return; }
    if (item.kind === "assign") { const value = evaluate(item.expression, binding); if (item.name === "_") return; if (binding[item.name] !== undefined && !same(binding[item.name], value)) return; yield* this.bindBody(rest, { ...rows }, { ...binding, [item.name]: value }); return; }
    const relationRows = rows.get(item.relation); if (!relationRows) throw new MrlRuleError("MRL-UNDEFINED", `undefined relation ${item.relation}`);
    if (item.negated) {
      const exists = relationRows.some((row) => this.atomMatches(item.args, row, binding));
      if (!exists) yield* this.bindBody(rest, rows, binding);
      return;
    }
    for (const row of relationRows) {
      if (row.length !== item.args.length) throw new MrlRuleError("MRL-ARITY", `relation ${item.relation} arity mismatch`);
      const next = { ...binding }; let valid = true;
      for (let index = 0; index < item.args.length; index += 1) {
        const term = item.args[index]; const value = row[index];
        if (term.kind === "variable") { if (next[term.name] !== undefined && !same(next[term.name], value)) valid = false; else next[term.name] = value; }
        else if (term.kind === "expression" && !same(termValue(term, next), value)) valid = false;
      }
      if (valid) yield* this.bindBody(rest, rows, next);
    }
  }
  private atomMatches(args: RuleTerm[], row: Row, binding: Binding): boolean {
    if (args.length !== row.length) return false;
    for (let index = 0; index < args.length; index += 1) {
      const term = args[index]; const value = row[index];
      if (term.kind === "variable" && binding[term.name] !== undefined && !same(binding[term.name], value)) return false;
      if (term.kind === "expression" && !same(termValue(term, binding), value)) return false;
    }
    return true;
  }
}

export class RuleModel {
  constructor(private readonly program: RuleProgram, private readonly rows: Map<string, Row[]>) {}
  rowsOf(name: string): Row[] { if (!this.program.relations.has(name)) throw new MrlRuleError("MRL-UNDEFINED", `undefined relation ${name}`); return (this.rows.get(name) ?? []).map((row) => [...row]); }
  contains(name: string, row: Row): boolean { return this.rowsOf(name).some((candidate) => JSON.stringify(candidate) === JSON.stringify(row)); }
  query(name: string, predicate = "true"): Row[] { return this.rowsOf(name).filter((row) => evaluate(predicate, Object.fromEntries(row.map((value, index) => [`_${index}`, value])) ) === true); }
}

export function buildRuleProgram(sources: Array<{ name: string; source: string }>): RuleProgram { return new RuleProgram(parseRuleModules(sources)); }
