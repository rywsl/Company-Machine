import {
  buildRuleProgram,
  compileExpr,
  compileTemplate,
  evaluate,
  MrlEvalError,
  MrlRuleError,
  MrlStaticError,
  parseType,
  type MrlValue,
} from "./index";

type JsonMap = Record<string, MrlValue>;

const usage = `mrl (Bun/TypeScript)

  eval EXPR [--env JSON|@FILE] [--types JSON|@FILE] [--budget N] [--json]
  template TEXT [--env JSON|@FILE] [--types JSON|@FILE] [--budget N] [--json]
  check FILE... [--json]
  run FILE... [--facts JSON|@FILE] [--rel NAME]... [--json]
`;

function flag(args: string[], name: string): string | undefined {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : undefined;
}

function flags(args: string[], name: string): string[] {
  return args.flatMap((value, index) => value === name && args[index + 1] ? [args[index + 1]] : []);
}

function hasFlag(args: string[], name: string): boolean {
  return args.includes(name);
}

function positional(args: string[], valueFlags: Set<string>): string[] {
  const values: string[] = [];
  for (let index = 0; index < args.length; index += 1) {
    const item = args[index];
    if (item.startsWith("--")) {
      if (valueFlags.has(item)) index += 1;
      continue;
    }
    values.push(item);
  }
  return values;
}

async function readMapping(value: string | undefined, label: string): Promise<JsonMap> {
  if (!value) return {};
  const source = value.startsWith("@") ? await Bun.file(value.slice(1)).text() : value;
  let parsed: unknown;
  try {
    parsed = JSON.parse(source);
  } catch (error) {
    throw new Error(`${label} must be valid JSON${error instanceof Error ? `: ${error.message}` : ""}`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(`${label} must be a JSON object`);
  return parsed as JsonMap;
}

function typeEnvironment(spec: JsonMap, values: JsonMap): Record<string, string> {
  const names = new Set([...Object.keys(values), ...Object.keys(spec)]);
  return Object.fromEntries([...names].map((name) => [name, typeof spec[name] === "string" ? spec[name] : "dyn"]));
}

function output(value: unknown, jsonMode: boolean): void {
  if (jsonMode) {
    console.log(JSON.stringify(value, null, 2));
    return;
  }
  if (typeof value === "string") console.log(value);
  else console.log(JSON.stringify(value, null, 2));
}

function diagnostics(error: MrlStaticError, jsonMode: boolean): number {
  output({ ok: false, diagnostics: error.diagnostics }, jsonMode);
  return 3;
}

function runtimeError(error: MrlEvalError | MrlRuleError, jsonMode: boolean): number {
  output({ ok: false, error: { code: error.code, message: error.message } }, jsonMode);
  return 4;
}

function ruleError(error: MrlRuleError, jsonMode: boolean, command: "check" | "run"): number {
  output({ ok: false, error: { code: error.code, message: error.message } }, jsonMode);
  return command === "check" ? 3 : 4;
}

async function expressionCommand(command: "eval" | "template", args: string[]): Promise<number> {
  const source = positional(args, new Set(["--env", "--types", "--budget"]))[0];
  if (!source) throw new Error(`${command} requires an expression or template`);
  const values = await readMapping(flag(args, "--env"), "--env");
  const typeSpec = await readMapping(flag(args, "--types"), "--types");
  const environment = typeEnvironment(typeSpec, values);
  const budgetText = flag(args, "--budget");
  const budget = budgetText === undefined ? undefined : Number(budgetText);
  if (budget !== undefined && (!Number.isInteger(budget) || budget < 1)) throw new Error("--budget must be a positive integer");
  const jsonMode = hasFlag(args, "--json");
  try {
    if (command === "template") {
      const compiled = compileTemplate(source, { environment });
      const value = compiled.render(values, { budget });
      output(jsonMode ? { ok: true, value, warnings: compiled.warnings } : value, jsonMode);
      return 0;
    }
    const compiled = compileExpr(source, { environment });
    const value = evaluate(compiled.ast, values, { budget });
    output(jsonMode ? { ok: true, value, type: compiled.type, warnings: compiled.warnings } : value, jsonMode);
    return 0;
  } catch (error) {
    if (error instanceof MrlStaticError) return diagnostics(error, jsonMode);
    if (error instanceof MrlEvalError) return runtimeError(error, jsonMode);
    throw error;
  }
}

async function sourceFiles(files: string[]): Promise<Array<{ name: string; source: string }>> {
  if (!files.length) throw new Error("a rule source file is required");
  return Promise.all(files.map(async (file) => ({ name: file, source: await Bun.file(file).text() })));
}

async function ruleCommand(command: "check" | "run", args: string[]): Promise<number> {
  const jsonMode = hasFlag(args, "--json");
  const files = positional(args, new Set(["--facts", "--rel"]));
  try {
    const program = buildRuleProgram(await sourceFiles(files));
    if (command === "check") {
      output(jsonMode ? { ok: true, relations: program.relations.size, rules: program.rules.length } : `✔ ${program.relations.size} relations, ${program.rules.length} rules`, jsonMode);
      return 0;
    }
    const facts = await readMapping(flag(args, "--facts"), "--facts");
    const model = program.evaluate(facts as Record<string, MrlValue[][]>);
    const names = flags(args, "--rel");
    const relations = names.length ? names : [...program.relations.keys()].sort();
    const rows = Object.fromEntries(relations.map((name) => [name, model.rowsOf(name)]));
    output(rows, jsonMode);
    return 0;
  } catch (error) {
    if (error instanceof MrlStaticError) return diagnostics(error, jsonMode);
    if (error instanceof MrlRuleError) return ruleError(error, jsonMode, command);
    if (error instanceof MrlEvalError) return runtimeError(error, jsonMode);
    throw error;
  }
}

export async function main(argv = Bun.argv.slice(2)): Promise<number> {
  const [command, ...args] = argv;
  if (!command || command === "help" || command === "--help") {
    console.log(usage);
    return 0;
  }
  if (command === "eval" || command === "template") return expressionCommand(command, args);
  if (command === "check" || command === "run") return ruleCommand(command, args);
  console.error(`unknown mrl command: ${command}`);
  console.error(usage);
  return 2;
}

if (import.meta.main) process.exit(await main());
