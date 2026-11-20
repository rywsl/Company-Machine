/** Build the Bun-based test images used by the migrated fixtures. */

import { mkdtemp, mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { KiriProvider, resolveKiriClient } from "../backend/runtime/kiri";
import { parseImageId, resolvePodmanExecutable, runCommand } from "../backend/runtime/podman";

const repo = resolve(import.meta.dir, "..");
const imagesRoot = join(repo, "tests", "fixtures", "images");
const fakesRoot = join(repo, "tests", "fakes");
const names = ["tool", "studio", "operator"] as const;
const textExtensions = new Set([".ts", ".sh", ".txt", ".json", ".yaml", ".yml", ".md", ".factory", ""]);

type CommandResult = { code: number; stdout: string; stderr: string };

function usage(): void {
  console.log(`bun run build:test-images [--only NAME]... [--base IMAGE] [--executable PATH]
  [--save-to DIRECTORY | --load-from DIRECTORY] [--build-timeout SECONDS]`);
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

async function run(argv: string[], timeoutMs: number, env?: Record<string, string>): Promise<CommandResult> {
  const result = await runCommand(argv, { timeoutMs, env });
  const decoder = new TextDecoder();
  return { code: result.exitCode, stdout: decoder.decode(result.stdout), stderr: decoder.decode(result.stderr) };
}

async function checked(argv: string[], timeoutMs: number, env?: Record<string, string>): Promise<string> {
  const result = await run(argv, timeoutMs, env);
  if (result.code !== 0) throw new Error(`${argv.join(" ")} 失败\n${result.stdout}\n${result.stderr}`);
  return result.stdout;
}

async function build(name: string, commandPrefix: string[], base: string | undefined, timeoutMs: number, env?: Record<string, string>): Promise<void> {
  const context = await mkdtemp(join(tmpdir(), `mao-img-${name}-`));
  try {
    await copyTree(join(imagesRoot, name), context);
    await copyTree(fakesRoot, join(context, "fakes"));
    const argv = [...commandPrefix, "build", "--tag", `mao-test/${name}:dev`];
    if (base) argv.push("--build-arg", `BASE=${base}`);
    argv.push(context);
    await checked(argv, timeoutMs, env);
  } finally {
    await rm(context, { recursive: true, force: true });
  }
}

async function inspectImage(commandPrefix: string[], tag: string, env?: Record<string, string>): Promise<string> {
  const inspect = (name: string) => run([...commandPrefix, "image", "inspect", "--format", "{{.Id}}", name], 60_000, env);
  const inspected = await inspect(tag);
  const id = parseImageId(inspected.stdout);
  if (inspected.code !== 0 || !id) throw new Error(`找不到 ${tag} 的镜像摘要\n${inspected.stderr}`);
  return id;
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
  const base = one(argv, "--base");
  const save = one(argv, "--save-to");
  const load = one(argv, "--load-from");
  const seconds = one(argv, "--build-timeout");
  const timeout = seconds === undefined ? undefined : Number(seconds);
  if ((save && load) || (load && base) || (timeout !== undefined && (!Number.isFinite(timeout) || timeout <= 0 || timeout > 86_400))) {
    usage();
    return 2;
  }
  const targets = selected.length ? [...new Set(selected)] : [...names];
  const ids: Record<string, string> = {};
  try {
    const kiri = !explicit && process.env.MAO_CONTAINER_RUNTIME?.toLowerCase() === "kiri" ? resolveKiriClient() : undefined;
    const commandPrefix = kiri?.argv ?? [explicit ?? resolvePodmanExecutable()];
    const env = kiri && { ...kiri.env, ...(kiri.mode === "linux-socket" ? { DOCKER_BUILDKIT: "0" } : {}) };
    const buildTimeoutMs = 1000 * (timeout ?? (kiri?.mode === "linux-socket" ? 300 : 1800));
    if (kiri) {
      const health = await new KiriProvider().ensureReady();
      if (!health.available) throw new Error(`${health.executable} 不可用\n${health.reason ?? ""}`);
    } else {
      const health = await run([...commandPrefix, "version"], 60_000);
      if (health.code !== 0) throw new Error(`${commandPrefix.join(" ")} 不可用\n${health.stderr}`);
    }
    if (save) await mkdir(resolve(save), { recursive: true });
    for (const name of targets) {
      const tag = `mao-test/${name}:dev`;
      if (load) {
        const output = await checked([...commandPrefix, "load", "--input", join(resolve(load), `${name}.tar`)], 300_000, env);
        // Podman writes its local registry prefix into Docker-format archives.
        const localTag = `localhost/${tag}`;
        if (kiri && output.split(/\r?\n/).some((line) => line.trim() === `Loaded image: ${localTag}`)) {
          await checked([...commandPrefix, "tag", localTag, tag], 60_000, env);
        }
      } else {
        console.error(`正在构建 ${tag}`);
        try {
          await build(name, commandPrefix, base, buildTimeoutMs, env);
        } catch (error) {
          if (kiri?.mode === "linux-socket") {
            throw new Error(`构建 ${tag} 失败。可用 --load-from DIR 导入已构建的测试镜像。\n${error instanceof Error ? error.message : String(error)}`);
          }
          throw error;
        }
      }
      const id = await inspectImage(commandPrefix, tag, env);
      ids[name] = id;
      if (save) await checked([...commandPrefix, "save", "--output", join(resolve(save), `${name}.tar`), tag], 300_000, env);
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
