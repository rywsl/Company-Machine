import { existsSync } from "node:fs";
import { join, relative } from "node:path";
import ts from "../frontend/node_modules/typescript/lib/typescript.js";

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
    const inspect = (node: ts.Node): void => {
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
