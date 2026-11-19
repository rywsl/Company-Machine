import { evaluate, type EvaluateOptions, type EvalEnvironment, type MrlValue } from "./evaluator";
import { checkExpression, MrlStaticError, type CheckOptions, type CheckResult } from "./checker";
import { BOOL, DYN, FLOAT, INT, NUM, STRING } from "./types";

export type TemplatePart = { kind: "text"; value: string } | { kind: "expression"; source: string; check: CheckResult };

export class MrlTemplateError extends Error {
  constructor(readonly code: string, message: string) { super(message); this.name = "MrlTemplateError"; }
}

function findExpressionEnd(source: string, start: number): number {
  let depth = 0;
  let quote = "";
  for (let index = start; index < source.length - 1; index += 1) {
    const character = source[index];
    if (quote) {
      if (character === "\\") { index += 1; continue; }
      if (character === quote) quote = "";
      continue;
    }
    if (character === "\"" || character === "'") { quote = character; continue; }
    if (character === "}" && source[index + 1] === "}" && depth === 0) return index;
    if (character === "{" || character === "[" || character === "(") { depth += 1; continue; }
    if (character === "}" || character === "]" || character === ")") { depth = Math.max(0, depth - 1); continue; }
  }
  return -1;
}

function splitTemplate(source: string): Array<{ kind: "text" | "expression"; value: string }> {
  const parts: Array<{ kind: "text" | "expression"; value: string }> = [];
  let cursor = 0;
  while (cursor < source.length) {
    const open = source.indexOf("{{", cursor);
    if (open < 0) { parts.push({ kind: "text", value: source.slice(cursor) }); break; }
    if (open > cursor) parts.push({ kind: "text", value: source.slice(cursor, open) });
    const close = findExpressionEnd(source, open + 2);
    if (close < 0) throw new MrlTemplateError("MRL-SYNTAX", "template interpolation is not closed");
    const expression = source.slice(open + 2, close).trim();
    if (!expression) throw new MrlTemplateError("MRL-SYNTAX", "template interpolation is empty");
    parts.push({ kind: "expression", value: expression });
    cursor = close + 2;
  }
  if (source.length === 0) parts.push({ kind: "text", value: "" });
  return parts;
}

function renderValue(value: MrlValue): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  throw new MrlTemplateError("MRL-TYPE", "template interpolation must produce string, number, bool, or dyn");
}

export interface CompiledTemplate {
  source: string;
  parts: TemplatePart[];
  warnings: ReturnType<typeof checkExpression>["warnings"];
  render(values: EvalEnvironment, options?: EvaluateOptions): string;
}

export function compileTemplate(source: string, options: CheckOptions = {}): CompiledTemplate {
  const parts = splitTemplate(source).map((part) => {
    if (part.kind === "text") return { kind: "text", value: part.value } as TemplatePart;
    const check = checkExpression(part.value, options);
    if (![STRING, INT, FLOAT, NUM, BOOL, DYN].some((type) => type.kind === check.type.kind && type.kind === "primitive" && check.type.kind === "primitive" && type.name === check.type.name)) {
      throw new MrlStaticError([{ code: "MRL-TYPE", severity: "error", message: "template interpolation must produce string, number, bool, or dyn" }]);
    }
    return { kind: "expression", source: part.value, check } as TemplatePart;
  });
  return {
    source,
    parts,
    warnings: parts.flatMap((part) => part.kind === "expression" ? part.check.warnings : []),
    render(values, evaluateOptions = {}) {
      return parts.map((part) => part.kind === "text" ? part.value : renderValue(evaluate(part.check.ast, values, evaluateOptions))).join("");
    },
  };
}

export function renderTemplate(source: string, values: EvalEnvironment = {}, checkOptions: CheckOptions = {}, evaluateOptions: EvaluateOptions = {}): string {
  return compileTemplate(source, checkOptions).render(values, evaluateOptions);
}
