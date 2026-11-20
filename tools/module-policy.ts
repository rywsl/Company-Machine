import { existsSync } from "node:fs";
import { basename, join, relative } from "node:path";
import ts from "../frontend/node_modules/typescript/lib/typescript.js";
import { listFiles } from "../backend/mrl/factory/paths";

const stdoutEntrypoints = new Set([
  "backend/src/cli.ts", "backend/src/connector-cli.ts", "backend/src/changeset-cli.ts",
  "backend/src/epoch-cli.ts", "backend/src/station-cli.ts", "backend/src/index.ts",
  "backend/src/legacy/factoryctl.ts", "backend/mrl/cli.ts",
]);
const consoleOutput = new Set([
  "log", "info", "debug", "dir", "dirxml", "table", "count", "timeLog", "timeEnd", "group", "groupCollapsed",
]);
const objectMutators = new Set(["assign", "defineProperty", "defineProperties", "setPrototypeOf", "freeze", "seal", "preventExtensions"]);
const reflectMutators = new Set(["defineProperty", "deleteProperty", "preventExtensions", "set", "setPrototypeOf"]);
const reflectiveKeys = new Set(["get", "set", "has", "defineProperty", "deleteProperty", "getOwnPropertyDescriptor"]);
const nativeTime = new Set(["backend/src/legacy/foundation.ts", "backend/src/legacy/factoryctl.ts"]);
const secretEntropy = new Set(["backend/src/vault.ts", "backend/src/legacy/egress-forwarder.ts"]);
const randomFunctions = new Set(["randomUUID", "randomBytes", "randomInt", "randomFill", "randomFillSync", "getRandomValues"]);
const cwdEntrypoints = new Set(["backend/src/cli.ts", "backend/src/index.ts"]);
const fileReads = new Set(["readFile", "readFileSync"]);
const fileWrites = new Set(["writeFile", "writeFileSync", "appendFile", "appendFileSync"]);
const sqliteMethods = new Set(["exec", "run", "query", "prepare"]);

function unwrap(expression: ts.Expression): ts.Expression {
  while (ts.isParenthesizedExpression(expression) || ts.isAsExpression(expression) ||
    ts.isTypeAssertionExpression(expression) || ts.isNonNullExpression(expression) || ts.isSatisfiesExpression(expression)) {
    expression = expression.expression;
  }
  return expression;
}

function member(expression: ts.Node): { receiver: ts.Expression; name?: string } | undefined {
  if (ts.isPropertyAccessExpression(expression)) return { receiver: expression.expression, name: expression.name.text };
  if (ts.isElementAccessExpression(expression)) {
    const key = unwrap(expression.argumentExpression);
    return { receiver: expression.expression, name: ts.isStringLiteralLike(key) ? key.text : undefined };
  }
}

function importedFrom(identifier: ts.Identifier, checker: ts.TypeChecker): string | undefined {
  for (const part of checker.getSymbolAtLocation(identifier)?.declarations ?? []) {
    const statement = ts.isImportClause(part) ? part.parent : ts.isNamespaceImport(part) ? part.parent.parent
      : ts.isImportSpecifier(part) ? part.parent.parent.parent : undefined;
    if (statement && ts.isImportDeclaration(statement) && ts.isStringLiteral(statement.moduleSpecifier)) {
      return statement.moduleSpecifier.text;
    }
  }
}

function globalBinding(expression: ts.Expression, name: string, checker: ts.TypeChecker): boolean {
  expression = unwrap(expression);
  if (ts.isIdentifier(expression) && expression.text === name) {
    return (checker.getSymbolAtLocation(expression)?.declarations ?? []).every((declaration) => declaration.getSourceFile().isDeclarationFile);
  }
  const access = member(expression);
  return name !== "globalThis" && access?.name === name && globalBinding(access.receiver, "globalThis", checker);
}

function importedObject(expression: ts.Expression, checker: ts.TypeChecker): boolean {
  expression = unwrap(expression);
  for (let access = member(expression); access; access = member(expression)) expression = unwrap(access.receiver);
  return ts.isIdentifier(expression) && importedFrom(expression, checker) !== undefined;
}

function privateAccess(receiver: ts.Expression, name: string, use: ts.Node, checker: ts.TypeChecker): boolean {
  const type = checker.getTypeAtLocation(unwrap(receiver));
  const property = checker.getPropertyOfType(type, name);
  return (property?.declarations ?? []).some((declaration) => {
    if (!ts.canHaveModifiers(declaration) || !ts.getModifiers(declaration)?.some(({ kind }) => kind === ts.SyntaxKind.PrivateKeyword)) return false;
    for (let ancestor: ts.Node | undefined = use.parent; ancestor; ancestor = ancestor.parent) {
      if (ancestor === declaration.parent) return false;
    }
    return true;
  });
}

function standardModule(expression: ts.Expression, name: string, checker: ts.TypeChecker): boolean {
  const target = unwrap(expression);
  return globalBinding(target, name, checker) || (ts.isIdentifier(target) && importedFrom(target, checker) === `node:${name}`);
}

function nodeBuiltinMember(target: ts.Expression, module: string, checker: ts.TypeChecker): string | undefined {
  const access = member(target);
  if (access && standardModule(access.receiver, module, checker)) return access.name;
  if (!ts.isIdentifier(target) || importedFrom(target, checker) !== `node:${module}`) return;
  const binding = checker.getSymbolAtLocation(target)?.declarations?.find(ts.isImportSpecifier);
  return binding && (binding.propertyName ?? binding.name).text;
}

function fileEncoding(options: ts.Expression | undefined, checker: ts.TypeChecker): string | undefined {
  if (!options) return undefined;
  const value = unwrap(options);
  if (ts.isStringLiteralLike(value)) return value.text;
  if (ts.isObjectLiteralExpression(value)) {
    const property = value.properties.find((entry) => ts.isPropertyAssignment(entry) &&
      (ts.isIdentifier(entry.name) || ts.isStringLiteralLike(entry.name)) && entry.name.text === "encoding");
    if (property && ts.isPropertyAssignment(property)) return fileEncoding(property.initializer, checker);
  }
  const type = checker.getTypeAtLocation(value);
  const property = checker.getPropertyOfType(type, "encoding");
  const encoding = property ? checker.getTypeOfSymbolAtLocation(property, value) : type;
  if (encoding.isStringLiteral()) return encoding.value;
}

export function archivedMigrationProblems(root: string): string[] {
  const numbers = new Map<string, string>();
  const problems: string[] = [];
  for (const file of listFiles(join(root, "removed/python/src/mao/migrations")).sort()) {
    if (!file.endsWith(".sql")) continue;
    const path = relative(root, file).replaceAll("\\", "/");
    const number = /^(\d{3})_[a-z][a-z0-9_]*\.sql$/.exec(basename(file))?.[1];
    if (!number) problems.push(`${path}: 历史 SQL 迁移文件名应使用 NNN_<名字>.sql`);
    else if (numbers.has(number)) problems.push(`${path}: 历史 SQL 迁移编号 ${number} 与 ${numbers.get(number)} 重复`);
    else numbers.set(number, path);
  }
  return problems;
}

export function modulePolicyProblems(root: string, files: string[]): string[] {
  if (!files.length) return [];
  const shim = join(root, "backend/bun-shim.d.ts");
  const program = ts.createProgram(existsSync(shim) ? [...files, shim] : files, {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.Bundler,
    skipLibCheck: true, strict: true, types: [],
  });
  const checker = program.getTypeChecker();
  const problems: string[] = [];
  for (const file of files) {
    const source = program.getSourceFile(file)!;
    const path = relative(root, file).replaceAll("\\", "/");
    const allowStdout = stdoutEntrypoints.has(path);
    const report = (node: ts.Node, message: string) => {
      const { line, character } = source.getLineAndCharacterOfPosition(node.getStart(source));
      problems.push(`${path}:${line + 1}:${character + 1} ${message}`);
    };
    const mutation = (target: ts.Expression) => {
      const access = member(unwrap(target));
      if (access && importedObject(access.receiver, checker)) report(target, "控制面禁止修改导入对象");
    };
    const stdout = (receiver: ts.Expression, name: string) => (
      (name === "stdout" && (standardModule(receiver, "process", checker) || globalBinding(receiver, "Bun", checker))) ||
      (consoleOutput.has(name) && standardModule(receiver, "console", checker))
    );
    const checkDeterminism = (node: ts.CallExpression | ts.NewExpression) => {
      const target = unwrap(node.expression);
      const processMember = nodeBuiltinMember(target, "process", checker);
      if (!cwdEntrypoints.has(path) && (processMember === "cwd" || processMember === "chdir")) {
        report(node, "控制面工作目录来自配置或调用方参数");
      }
      if (nativeTime.has(path)) return;
      const access = member(target);
      if ((globalBinding(target, "Date", checker) && (!ts.isNewExpression(node) || !node.arguments?.length)) ||
        (access?.name === "now" && (globalBinding(access.receiver, "Date", checker) || globalBinding(access.receiver, "performance", checker))) ||
        (access?.name === "nanoseconds" && globalBinding(access.receiver, "Bun", checker)) || processMember === "hrtime") {
        report(node, "控制面时间来自注入的 Clock");
      }
      const random = nodeBuiltinMember(target, "crypto", checker);
      if ((random && randomFunctions.has(random) && (random !== "randomBytes" || !secretEntropy.has(path))) ||
        (access?.name === "random" && globalBinding(access.receiver, "Math", checker))) {
        report(node, "控制面 ID 来自 IdSource 或 foundation 中的 UUID 生成器");
      }
    };
    const checkFileEncoding = (node: ts.CallExpression) => {
      const target = unwrap(node.expression);
      const method = nodeBuiltinMember(target, "fs", checker) ?? nodeBuiltinMember(target, "fs/promises", checker);
      if (!method) return;
      if (fileReads.has(method)) {
        const encoding = fileEncoding(node.arguments[1], checker);
        const expected = checker.getContextualType(node);
        const expectsText = ((expected?.flags ?? 0) & ts.TypeFlags.StringLike) !== 0;
        if ((expectsText || encoding !== undefined) && !/^utf-?8$/i.test(encoding ?? "")) report(node, "控制面文件文本须使用 UTF-8 编码");
      } else if (fileWrites.has(method) && node.arguments[1]) {
        const data = checker.getTypeAtLocation(unwrap(node.arguments[1]));
        const encoding = fileEncoding(node.arguments[2], checker);
        const knownText = (data.flags & ts.TypeFlags.StringLike) !== 0;
        if ((knownText || encoding !== undefined) && !/^utf-?8$/i.test(encoding ?? "")) report(node, "控制面文件文本须显式使用 UTF-8 编码");
      }
    };
    const inspect = (node: ts.Node): void => {
      if (ts.isCallExpression(node) || ts.isNewExpression(node)) checkDeterminism(node);
      if (ts.isCallExpression(node)) {
        checkFileEncoding(node);
        const method = member(unwrap(node.expression))?.name;
        const statement = node.arguments[0];
        if (method && sqliteMethods.has(method) && statement && ts.isStringLiteralLike(statement) &&
          /\bPRAGMA\s+(?:[\w]+\.)?user_version\s*(?:=|\()/i.test(statement.text)) {
          report(node, "Bun 数据表初始化保留历史 SQLite user_version");
        }
      }
      if (ts.isCatchClause(node) && node.block.statements.every(ts.isEmptyStatement) &&
        !/\/\/[ \t]*[^\s/]|\/\*\s*[^\s*/]/.test(source.text.slice(node.block.getStart(source) + 1, node.block.end - 1))) {
        report(node, "空 catch 必须说明可忽略该错误的原因");
      }
      const access = member(node);
      if (access?.name) {
        const { receiver, name } = access;
        if ((ts.isElementAccessExpression(node) || unwrap(receiver) !== receiver) && privateAccess(receiver, name, node, checker)) {
          report(node, `控制面禁止绕过私有成员 ${name} 的访问边界`);
        }
        if (!allowStdout && stdout(receiver, name)) report(node, "控制面标准输出仅用于命令行和进程入口");
      }
      if (!allowStdout && ts.isImportSpecifier(node)) {
        const name = (node.propertyName ?? node.name).text;
        const module = importedFrom(node.name, checker);
        if ((module === "node:console" && consoleOutput.has(name)) || (module === "node:process" && name === "stdout")) {
          report(node, "控制面标准输出仅用于命令行和进程入口");
        }
      }
      if (!allowStdout && ts.isVariableDeclaration(node) && node.initializer && ts.isObjectBindingPattern(node.name)) {
        for (const item of node.name.elements) {
          const key = item.propertyName ?? item.name;
          if ((ts.isIdentifier(key) || ts.isStringLiteralLike(key)) && stdout(node.initializer, key.text)) {
            report(item, "控制面标准输出仅用于命令行和进程入口");
          }
        }
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment && node.operatorToken.kind <= ts.SyntaxKind.LastAssignment) {
        mutation(node.left);
      } else if (ts.isDeleteExpression(node)) {
        mutation(node.expression);
      } else if ((ts.isPrefixUnaryExpression(node) || ts.isPostfixUnaryExpression(node)) &&
        (node.operator === ts.SyntaxKind.PlusPlusToken || node.operator === ts.SyntaxKind.MinusMinusToken)) {
        mutation(node.operand);
      } else if (ts.isCallExpression(node) && node.arguments.length) {
        const method = member(unwrap(node.expression));
        if (method?.name) {
          const { receiver, name } = method;
          const reflect = globalBinding(receiver, "Reflect", checker);
          const object = globalBinding(receiver, "Object", checker);
          const target = node.arguments[0];
          if (((reflect && reflectMutators.has(name)) || (object && objectMutators.has(name))) && importedObject(target, checker)) {
            report(node, "控制面禁止修改导入对象");
          }
          const key = node.arguments[1];
          if (key && ts.isStringLiteralLike(key) && reflectiveKeys.has(name) &&
            (reflect || (object && (name === "defineProperty" || name === "getOwnPropertyDescriptor"))) &&
            privateAccess(target, key.text, node, checker)) report(node, `控制面禁止绕过私有成员 ${key.text} 的访问边界`);
        }
      }
      ts.forEachChild(node, inspect);
    };
    inspect(source);
  }
  return problems;
}
