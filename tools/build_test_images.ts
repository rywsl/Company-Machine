/** Build the Bun-based test images used by the migrated fixtures. */

import { cp, mkdtemp, mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { resolveKiriExecutable } from "../backend/runtime/kiri";
import { resolvePodmanExecutable } from "../backend/runtime/podman";

const repo = resolve(import.meta.dir, "..");
const imagesRoot = join(repo, "tests", "fixtures", "images");
const fakesRoot = join(repo, "tests", "fakes");
const names = ["tool", "studio", "operator"] as const;
const textExtensions = new Set([".ts", ".sh", ".txt", ".json", ".yaml", ".yml", ".md", ".factory", ""]);

type CommandResult = { code: number; stdout: string; stderr: string };

function usage(): void {
  console.log(`bun run build:test-images [--only NAME]... [--base IMAGE] [--executable PATH]`);
}

function values(args: string[], flag: string): string[] {
  return args.flatMap((item, index) => item === flag && args[index + 1] ? [args[index + 1]] : []);
}

function one(args: string[], flag: string): string | undefined {
  return values(args, flag)[0];
}

async function copyTree(source: string, destination: string): Promise<void> {
  await mkdir(destination, { recursive: true });
  for (const entry of await readdir(source, { withFileTypes: true })) {
    if (entry.name === "__pycache__" || entry.name === ".git") continue;
    const from = join(source, entry.name);
    const to = join(destination, entry.name);
    if (entry.isDirectory()) {
      await copyTree(from, to);
      continue;
    }
    await mkdir(dirname(to), { recursive: true });
    const data = await readFile(from);
    if (textExtensions.has(entry.name.includes(".") ? entry.name.slice(entry.name.lastIndexOf(".")) : "") || entry.name === "Dockerfile") {
      await writeFile(to, data.toString("utf8").replaceAll("\r\n", "\n"), "utf8");
    } else {
      await writeFile(to, data);
    }
  }
}

async function run(argv: string[], timeoutMs: number): Promise<CommandResult> {
  const child = Bun.spawn(argv, { stdout: "pipe", stderr: "pipe" });
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      child.exited.then(async (code) => ({ code, stdout: await new Response(child.stdout).text(), stderr: await new Response(child.stderr).text() })),
      new Promise<CommandResult>((_, reject) => {
        timer = setTimeout(() => {
          child.kill();
          reject(new Error(`command timed out after ${timeoutMs}ms`));
        }, timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function build(name: string, commandPrefix: string[], base: string | undefined): Promise<string> {
  const context = await mkdtemp(join(tmpdir(), `mao-img-${name}-`));
  try {
    await copyTree(join(imagesRoot, name), context);
    await copyTree(fakesRoot, join(context, "fakes"));
    const argv = [...commandPrefix, "build", "--tag", `mao-test/${name}:dev`];
    if (base) argv.push("--build-arg", `BASE=${base}`);
    argv.push(context);
    const built = await run(argv, 1_800_000);
    if (built.code !== 0) throw new Error(`构建 mao-test/${name}:dev 失败\n${built.stdout}\n${built.stderr}`);
    const inspected = await run([...commandPrefix, "image", "inspect", "--format", "{{.Id}}", `mao-test/${name}:dev`], 60_000);
    if (inspected.code !== 0 || !/^sha256:[0-9a-f]{64}$/.test(inspected.stdout.trim())) throw new Error(`构建后找不到 mao-test/${name}:dev\n${inspected.stderr}`);
    return inspected.stdout.trim();
  } finally {
    await rm(context, { recursive: true, force: true });
  }
}

export async function main(argv = Bun.argv.slice(2)): Promise<number> {
  if (argv.includes("--help") || argv.includes("-h")) {
    usage();
    return 0;
  }
  const selected = values(argv, "--only");
  if (selected.some((name) => !names.includes(name as (typeof names)[number]))) {
    console.error(`--only must be one of ${names.join(", ")}`);
    return 2;
  }
  const explicit = one(argv, "--executable");
  const commandPrefix = explicit
    ? [explicit]
    : process.env.MAO_CONTAINER_RUNTIME?.toLowerCase() === "kiri"
      ? process.env.MAO_KIRI_MODE === "docker-context"
        ? [process.env.MAO_DOCKER_EXECUTABLE ?? "docker", "--context", process.env.MAO_KIRI_CONTEXT ?? "kiri"]
        : [resolveKiriExecutable(process.env.MAO_KIRI_EXECUTABLE)]
      : [resolvePodmanExecutable(process.env.MAO_PODMAN_EXECUTABLE)];
  const base = one(argv, "--base");
  const targets = selected.length ? [...new Set(selected)] : [...names];
  const ids: Record<string, string> = {};
  try {
    const health = await run([...commandPrefix, "version"], 60_000);
    if (health.code !== 0) throw new Error(`${commandPrefix.join(" ")} 不可用\n${health.stderr}`);
    for (const name of targets) {
      ids[name] = await build(name, commandPrefix, base);
      console.error(`✔ mao-test/${name}:dev ${ids[name]}`);
    }
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    return 1;
  }
  console.log(JSON.stringify(ids));
  return 0;
}

if (import.meta.main) process.exit(await main());
