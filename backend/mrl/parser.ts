import { tokenize, Token, MrlSyntaxError } from "./lexer";

export type Expr =
  | { kind: "literal"; value: unknown }
  | { kind: "identifier"; name: string }
  | { kind: "unary"; op: string; operand: Expr }
  | { kind: "binary"; op: string; left: Expr; right: Expr }
  | { kind: "conditional"; condition: Expr; then: Expr; otherwise: Expr }
  | { kind: "list"; items: Expr[] }
  | { kind: "record"; entries: Array<{ key: Expr; value: Expr }> }
  | { kind: "member"; object: Expr; field: string }
  | { kind: "index"; object: Expr; index: Expr }
  | { kind: "call"; name: string; args: Expr[]; receiver?: Expr }
  | { kind: "macro"; name: "all" | "exists" | "exists_one" | "map" | "filter"; target: Expr; variable: string; predicate?: Expr; body?: Expr };

export class Parser {
  private pos = 0;
  constructor(private readonly tokens: Token[], private readonly source = "") {}
  private get token() { return this.tokens[this.pos]; }
  private at(text: string) { return this.token.kind === "op" && this.token.text === text; }
  private fail(message: string): never { throw new MrlSyntaxError(`${message} near ${JSON.stringify(this.token.text)}`, this.token.offset); }
  private take(text: string) { if (!this.at(text)) this.fail(`expected ${text}`); this.pos++; }
  parse(): Expr {
    const result = this.expression();
    if (this.token.kind !== "eof") this.fail("trailing input");
    return result;
  }
  private expression() { return this.conditional(); }
  private conditional(): Expr {
    const condition = this.or();
    if (!this.at("?")) return condition;
    this.pos++; const then = this.or(); this.take(":");
    return { kind: "conditional", condition, then, otherwise: this.conditional() };
  }
  private or(): Expr { let left = this.and(); while (this.at("||")) { this.pos++; left = { kind: "binary", op: "||", left, right: this.and() }; } return left; }
  private and(): Expr { let left = this.relation(); while (this.at("&&")) { this.pos++; left = { kind: "binary", op: "&&", left, right: this.relation() }; } return left; }
  private relation(): Expr {
    let left = this.addition();
    if (this.token.kind === "ident" && this.token.text === "in" || (this.token.kind === "op" && ["==", "!=", "<", "<=", ">", ">="].includes(this.token.text))) {
      const op = this.token.text; this.pos++; const right = this.addition();
      if ((this.token.kind === "ident" && this.token.text === "in") || (this.token.kind === "op" && ["==", "!=", "<", "<=", ">", ">="].includes(this.token.text))) this.fail("comparison operators cannot be chained");
      left = { kind: "binary", op, left, right };
    }
    return left;
  }
  private addition(): Expr { let left = this.mult(); while (this.at("+") || this.at("-")) { const op = this.token.text; this.pos++; left = { kind: "binary", op, left, right: this.mult() }; } return left; }
  private mult(): Expr { let left = this.unary(); while (["*", "/", "%"].includes(this.token.text)) { const op = this.token.text; this.pos++; left = { kind: "binary", op, left, right: this.unary() }; } return left; }
  private unary(): Expr {
    if (this.at("!") || this.at("-")) { const op = this.token.text; this.pos++; return { kind: "unary", op, operand: this.unary() }; }
    return this.member();
  }
  private member(): Expr {
    let node = this.primary();
    while (true) {
      if (this.at(".")) {
        this.pos++;
        if (this.token.kind !== "ident") this.fail("expected field name");
        const name = this.token.text; this.pos++;
        if (!this.at("(")) { node = { kind: "member", object: node, field: name }; continue; }
        const args = this.arguments();
        if (["all", "exists", "exists_one", "map", "filter"].includes(name)) {
          if (args.length < 2 || args[0].kind !== "identifier") this.fail(`${name} requires a variable and expression`);
          const variable = (args[0] as { kind: "identifier"; name: string }).name;
          if (name === "map" && args.length === 2) node = { kind: "macro", name: "map", target: node, variable, body: args[1] };
          else if (name === "map" && args.length === 3) node = { kind: "macro", name: "map", target: node, variable, predicate: args[1], body: args[2] };
          else if (args.length === 2) node = { kind: "macro", name: name as "all" | "exists" | "exists_one" | "filter", target: node, variable, predicate: args[1] };
          else this.fail(`${name} has invalid arity`);
        } else node = { kind: "call", name, args, receiver: node };
      } else if (this.at("[")) {
        this.pos++; const index = this.expression(); this.take("]"); node = { kind: "index", object: node, index };
      } else return node;
    }
  }
  private arguments(): Expr[] {
    this.take("("); const args: Expr[] = [];
    while (!this.at(")")) {
      args.push(this.expression());
      if (this.at(",")) { this.pos++; if (this.at(")")) break; } else break;
    }
    this.take(")"); return args;
  }
  private primary(): Expr {
    const t = this.token;
    if (["int", "float", "string"].includes(t.kind)) { this.pos++; return { kind: "literal", value: t.value }; }
    if (t.kind === "ident") {
      this.pos++;
      if (t.text === "true" || t.text === "false") return { kind: "literal", value: t.text === "true" };
      if (t.text === "null") return { kind: "literal", value: null };
      if (t.text === "in") this.fail("in is reserved");
      if (this.at("(")) return { kind: "call", name: t.text, args: this.arguments() };
      return { kind: "identifier", name: t.text };
    }
    if (this.at("(")) { this.pos++; const value = this.expression(); this.take(")"); return value; }
    if (this.at("[")) {
      this.pos++; const items: Expr[] = [];
      while (!this.at("]")) { items.push(this.expression()); if (this.at(",")) this.pos++; else break; }
      this.take("]"); return { kind: "list", items };
    }
    if (this.at("{")) {
      this.pos++; const entries: Array<{ key: Expr; value: Expr }> = [];
      while (!this.at("}")) { const key = this.expression(); this.take(":"); const value = this.expression(); entries.push({ key, value }); if (this.at(",")) this.pos++; else break; }
      this.take("}"); return { kind: "record", entries };
    }
    this.fail("expected expression");
  }
}

export function parseExpression(source: string): Expr { return new Parser(tokenize(source), source).parse(); }
