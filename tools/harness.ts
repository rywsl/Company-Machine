/**
 * Bun-native verification entry point for the TypeScript control plane.
 *
 * The legacy Python verifier remains available for the historical execution
 * plane. This command exercises the migrated backend without starting a shell
 * or calling a model provider.
 */

import { fileURLToPath } from "node:url";

type Step = { label: string; argv: string[] };

// URL.pathname leaves drive letters prefixed with `/` and keeps percent
// escapes on Windows. `fileURLToPath` handles both Windows and POSIX paths,
// including repositories whose directory names contain spaces.
const root = fileURLToPath(new URL("..", import.meta.url));

function run(step: Step): number {
  const result = Bun.spawnSync(step.argv, { cwd: root, stdout: "pipe", stderr: "pipe" });
  const stdout = new TextDecoder().decode(result.stdout);
  const stderr = new TextDecoder().decode(result.stderr);
  if (stdout) process.stdout.write(stdout);
  if (stderr) process.stderr.write(stderr);
  if (result.exitCode !== 0) console.error(`✖ ${step.label} exit=${result.exitCode}`);
  else console.error(`✔ ${step.label}`);
  return result.exitCode;
}

const suites: Record<string, Step[]> = {
  acceptance: [
    { label: "Bun business acceptance", argv: ["bun", "test", "backend/tests"] },
    { label: "Bun runtime acceptance", argv: ["bun", "test", "backend/runtime"] },
    { label: "Bun MRL acceptance", argv: ["bun", "test", "backend/mrl"] },
  ],
  verify: [
    { label: "Bun backend acceptance", argv: ["bun", "test", "backend"] },
    { label: "Backend strict TypeScript check", argv: ["pnpm", "--filter", "@mao/web", "exec", "tsc", "--noEmit", "-p", "../backend/tsconfig.json", "--pretty", "false"] },
    { label: "Web TypeScript check", argv: ["pnpm", "check:web"] },
  ],
};

function usage(): never {
  console.log("bun run tools/harness.ts <acceptance|verify>");
  process.exit(2);
}

const command = Bun.argv[2] ?? "verify";
const steps = suites[command];
if (!steps) usage();

for (const step of steps) {
  const code = run(step);
  if (code !== 0) process.exit(code);
}
