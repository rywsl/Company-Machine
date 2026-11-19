export type TokenKind = "int" | "float" | "string" | "ident" | "op" | "eof";

export interface Token {
  kind: TokenKind;
  text: string;
  value?: unknown;
  offset: number;
}

export class MrlSyntaxError extends Error {
  readonly code = "MRL-SYNTAX";
  constructor(message: string, readonly offset = 0) {
    super(message);
    this.name = "MrlSyntaxError";
  }
}

const operators = ["==", "!=", "<=", ">=", "&&", "||", "<", ">", "!", "+", "-", "*", "/", "%", "?", ":", ".", ",", "(", ")", "[", "]", "{", "}"];

export function tokenize(source: string): Token[] {
  if (source.length > 1_048_576) throw new MrlSyntaxError("source exceeds MRL-LIMIT", 0);
  const out: Token[] = [];
  let i = 0;
  const push = (kind: TokenKind, text: string, value?: unknown, offset = i) => out.push({ kind, text, value, offset });
  while (i < source.length) {
    const c = source[i];
    if (/\s/.test(c)) { i++; continue; }
    if (source.startsWith("//", i)) {
      const end = source.indexOf("\n", i + 2);
      i = end < 0 ? source.length : end + 1;
      continue;
    }
    const start = i;
    if (/[A-Za-z_]/.test(c)) {
      i++;
      while (i < source.length && /[A-Za-z0-9_]/.test(source[i])) i++;
      push("ident", source.slice(start, i), source.slice(start, i), start);
      continue;
    }
    if (/[0-9]/.test(c)) {
      i++;
      while (i < source.length && /[0-9]/.test(source[i])) i++;
      let floating = false;
      if (source[i] === "." && /[0-9]/.test(source[i + 1] ?? "")) {
        floating = true; i++;
        while (i < source.length && /[0-9]/.test(source[i])) i++;
      }
      if (source[i] === "e" || source[i] === "E") {
        floating = true; i++;
        if (source[i] === "+" || source[i] === "-") i++;
        const exponentStart = i;
        while (i < source.length && /[0-9]/.test(source[i])) i++;
        if (i === exponentStart) throw new MrlSyntaxError("incomplete exponent", i);
      }
      if (/[A-Za-z_]/.test(source[i] ?? "")) throw new MrlSyntaxError("number followed by identifier", i);
      const text = source.slice(start, i);
      const integerDigits = text.match(/^[0-9]+/)?.[0] ?? "";
      if (integerDigits.length > 1 && integerDigits[0] === "0") throw new MrlSyntaxError("leading zero", start);
      const value = Number(text);
      if (!Number.isFinite(value)) throw new MrlSyntaxError("float literal out of range", start);
      push(floating ? "float" : "int", text, value, start);
      continue;
    }
    if (c === "\"" || c === "'") {
      const quote = c;
      i++;
      let value = "";
      while (i < source.length && source[i] !== quote) {
        if (source[i] === "\n" || source[i] === "\r") throw new MrlSyntaxError("unterminated string", start);
        if (source[i] !== "\\") { value += source[i++]; continue; }
        i++;
        if (i >= source.length) throw new MrlSyntaxError("unterminated string", start);
        const esc = source[i++];
        const simple: Record<string, string> = { "\\": "\\", '"': '"', "'": "'", n: "\n", r: "\r", t: "\t" };
        if (esc in simple) { value += simple[esc]; continue; }
        if (esc === "u" || esc === "U") {
          const width = esc === "u" ? 4 : 8;
          const hex = source.slice(i, i + width);
          if (hex.length !== width || !/^[0-9a-fA-F]+$/.test(hex)) throw new MrlSyntaxError("invalid unicode escape", i);
          const cp = Number.parseInt(hex, 16);
          if (cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) throw new MrlSyntaxError("invalid unicode scalar", i);
          value += String.fromCodePoint(cp); i += width; continue;
        }
        throw new MrlSyntaxError(`unknown escape \\${esc}`, i - 1);
      }
      if (i >= source.length) throw new MrlSyntaxError("unterminated string", start);
      i++; push("string", source.slice(start, i), value, start); continue;
    }
    const op = operators.find((candidate) => source.startsWith(candidate, i));
    if (op) { i += op.length; push("op", op, op, start); continue; }
    throw new MrlSyntaxError(`illegal character ${JSON.stringify(c)}`, i);
  }
  push("eof", "", undefined, source.length);
  return out;
}
