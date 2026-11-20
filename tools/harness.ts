import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { runMrlConformance } from "./mrl-conformance";
import { acceptanceProblems, activePolicyProblems, inspectSpecifications, parseAcceptanceReport } from "./spec-consistency";

const root = fileURLToPath(new URL("..", import.meta.url));
const decoder = new TextDecoder();

function run(label: string, argv: string[], quiet = false): void {
  const output = quiet ? "pipe" : "inherit";
  const result = Bun.spawnSync(argv, { cwd: root, stdout: output, stderr: output });
  if (quiet && result.exitCode !== 0) {
    process.stdout.write(decoder.decode(result.stdout));
    process.stderr.write(decoder.decode(result.stderr));
  }
  if (result.exitCode !== 0) throw new Error(`${label} exit=${result.exitCode}`);
}

function reportProblems(problems: string[]): void {
  if (problems.length) throw new Error(problems.join("\n"));
}

function acceptance(spec: ReturnType<typeof inspectSpecifications>, json: boolean) {
  const directory = mkdtempSync(join(tmpdir(), "mao-acceptance-"));
  try {
    const report = join(directory, "bun.junit.xml");
    run("Bun 验收", [process.execPath, "test", "--reporter=junit", `--reporter-outfile=${report}`, "backend", "tests/fakes"], json);
    const cases = parseAcceptanceReport(root, readFileSync(report, "utf8"));
    reportProblems(acceptanceProblems(spec, cases));
    if (!json) console.error(`✔ Bun 验收全部通过（${cases.length} 项、${spec.mappedFiles.size} 个文件）`);
    return cases.length;
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

function verifyMrl(spec: ReturnType<typeof inspectSpecifications>) {
  const mrl = runMrlConformance(root, spec.kernelCodes);
  const unwitnessedRules = spec.diagnosticRules.filter((rule) => !mrl.coveredRules[rule.file + ":" + rule.line]);
  return { mrl, unwitnessedRules };
}

function verifyParity(spec: ReturnType<typeof inspectSpecifications>, caseCount: number, json: boolean): number {
  const unmatched = [...spec.obligations].filter((item) => !spec.evidence.has(item)).sort();
  const { mrl, unwitnessedRules } = verifyMrl(spec);
  const checks = {
    archivedAcceptance: {
      files: spec.archivedFiles,
      obligations: spec.obligations.size,
      mapped: spec.evidence.size,
      unmatched,
      specMappingProblems: spec.historical,
    },
    mrl,
    kernelRulesWithoutExecutionEvidence: unwitnessedRules,
    policyMigrationRemaining: [
      "时钟、随机数和工作目录边界",
      "模块私有成员、导入对象修改和日志边界",
      "异常处理、文件编码与数据库迁移约束",
    ],
    activeAcceptance: { files: spec.mappedFiles.size, passed: caseCount },
  };
  if (json) {
    console.log(JSON.stringify(checks, null, 2));
  } else {
    const total = Object.values(mrl.counts).reduce((sum, count) => sum + count, 0);
    const passed = Object.values(mrl.passed).reduce((sum, count) => sum + count, 0);
    console.error(`历史验收 ${spec.evidence.size}/${spec.obligations.size} 项有通过的 Bun 等价证据，规范映射问题 ${spec.historical.length} 项`);
    console.error(`MRL ${passed}/${total} 项通过，${mrl.unverifiedCodes.length} 个诊断码缺少通过的覆盖用例，${checks.kernelRulesWithoutExecutionEvidence.length} 条内核规则尚无触发证据`);
    console.error(`代码策略尚需迁移 ${checks.policyMigrationRemaining.join("、")}。逐条差异见 parity --json`);
  }
  return unmatched.length || spec.historical.length || mrl.failures.length || mrl.unverifiedCodes.length
    || checks.kernelRulesWithoutExecutionEvidence.length || checks.policyMigrationRemaining.length ? 1 : 0;
}

function main(): number {
  const [command = "verify", ...args] = Bun.argv.slice(2);
  const json = args.length === 1 && args[0] === "--json";
  if (!["acceptance", "verify", "parity"].includes(command) || (args.length > 0 && (command !== "parity" || !json))) {
    console.error("bun run tools/harness.ts <acceptance|verify|parity [--json]>");
    return 2;
  }
  const spec = inspectSpecifications(root);
  reportProblems([...spec.problems, ...activePolicyProblems(root)]);
  if (!json) console.error(`✔ 活动规范一致性（${spec.componentSpecs} 个组件、${spec.mappedFiles.size} 个验收文件）`);
  const caseCount = acceptance(spec, json);
  if (command === "parity") return verifyParity(spec, caseCount, json);
  if (command === "verify") {
    run("Registry 夹具策略回归", [process.execPath, "test", "tools/registry-policy.test.ts"]);
    const { mrl, unwitnessedRules } = verifyMrl(spec);
    reportProblems([
      ...mrl.failures.map(({ name, reason }) => name + " " + reason),
      ...mrl.unverifiedCodes.map((code) => "诊断码缺少通过的用例 " + code),
      ...unwitnessedRules.map(({ file, line }) => `内核诊断规则缺少触发证据 ${file}:${line}`),
    ]);
    const count = Object.values(mrl.passed).reduce((sum, value) => sum + value, 0);
    console.error(`✔ MRL 一致性（${count} 项，${spec.diagnosticRules.length} 条内核诊断规则有触发证据）`);
    run("后端 TypeScript", ["pnpm", "--filter", "@mao/web", "exec", "tsc", "--noEmit", "-p", "../backend/tsconfig.json", "--pretty", "false"]);
    run("Web TypeScript", ["pnpm", "check:web"]);
    console.error("✔ 后端与 Web TypeScript");
    console.error(`历史验收已有 ${spec.evidence.size}/${spec.obligations.size} 项 Bun 等价映射。运行 parity 检查完整迁移`);
  }
  return 0;
}

try {
  process.exitCode = main();
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
