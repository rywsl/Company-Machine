import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

export const REPO = join(import.meta.dir, "../..");
export const FAKES = join(REPO, "tests", "fakes");
export const TOOLS = join(FAKES, "tools");
export const FIXTURES = join(REPO, "tests", "fixtures");
export const REGISTRY_M1 = join(FIXTURES, "registry-m1");
export const IMAGE_NAMES = ["tool", "studio", "operator"] as const;
export const PLACEHOLDERS: Record<string, string> = {
  tool: "09b42648636a8405354592f2ae8985670dfda5b61377ac6c75e3cebbd22e0500",
  studio: "f6bf945c5ebf73ef9407b0a47f424feeccd5a1783675d678bf1b66278c5e81f3",
  operator: "cef52db3ca9921be9f4a7c0c1e2c5eab79aa09b827c55fcf60c8a353247d7433",
};
export const FAKE_AGENT_COMMAND = "/opt/operator/bin/fake-agent";

const toolScripts: Record<string, string> = {
  "upper-text": join(TOOLS, "upper_text.ts"),
  "count-text": join(TOOLS, "count_text.ts"),
  "check-task": join(TOOLS, "check_task.ts"),
  "exit-three": join(TOOLS, "exit_three.ts"),
  "exit-one": join(TOOLS, "exit_one.ts"),
  "write-proposal": join(TOOLS, "write_proposal.ts"),
  "publish-copy": join(TOOLS, "publish_copy.ts"),
  "publish-flaky": join(TOOLS, "publish_flaky.ts"),
  "pull-notes": join(TOOLS, "pull_notes.ts"),
  "local-file-check": join(TOOLS, "local_file_check.ts"),
};

export function imageTag(name: string): string { return `mao-test/${name}:dev`; }

export function localCommands(): Record<string, string[]> {
  const bun = Bun.argv[0] ?? "bun";
  return Object.fromEntries(Object.entries(toolScripts).map(([name, path]) => [name, [bun, path]]).concat([[FAKE_AGENT_COMMAND, [bun, join(FAKES, "fake_agent.ts")]]]));
}

export class MissingImages extends Error {}

export async function imageIds(executable = process.env.MAO_PODMAN_EXECUTABLE ?? "podman"): Promise<Record<string, string>> {
  const result: Record<string, string> = {};
  const missing: string[] = [];
  for (const name of IMAGE_NAMES) {
    const child = Bun.spawn([executable, "image", "inspect", "--format", "{{.Id}}", imageTag(name)], { stdout: "pipe", stderr: "pipe" });
    const code = await child.exited;
    const value = (await new Response(child.stdout).text()).trim();
    if (code !== 0 || !/^sha256:[0-9a-f]{64}$/.test(value)) missing.push(imageTag(name));
    else result[name] = value;
  }
  if (missing.length) throw new MissingImages(`测试镜像不存在：${missing.join("、")}。先运行 bun run build:test-images`);
  return result;
}

function setPath(document: any, path: string, value: unknown): void {
  const parts = path.match(/[^.\[\]]+|\[\d+\]/g) ?? [];
  let current = document;
  parts.forEach((part, index) => {
    const last = index === parts.length - 1;
    if (part.startsWith("[")) {
      const position = Number(part.slice(1, -1));
      if (last) current[position] = value;
      else current = current[position];
    } else if (last) {
      if (value === undefined) delete current[part];
      else current[part] = value;
    } else {
      current[part] ??= {};
      current = current[part];
    }
  });
}

/** Copy a registry fixture and apply deterministic text/YAML patches. */
export async function renderRegistry(destination: string, options: { images?: Record<string, string>; files?: Record<string, string>; patches?: Array<[string, string, unknown]> } = {}): Promise<string> {
  await rm(destination, { recursive: true, force: true });
  await cp(REGISTRY_M1, destination, { recursive: true });
  for (const path of await listFiles(destination)) {
    if (!path.endsWith(".yaml") && !path.endsWith(".yml")) continue;
    let text = await readFile(path, "utf8");
    for (const [name, digest] of Object.entries(options.images ?? {})) text = text.replaceAll(PLACEHOLDERS[name], digest.replace(/^sha256:/, ""));
    await writeFile(path, text, "utf8");
  }
  for (const [relative, dotted, value] of options.patches ?? []) {
    const path = join(destination, relative);
    const document = Bun.YAML.parse(await readFile(path, "utf8"));
    setPath(document, dotted, value);
    await writeFile(path, Bun.YAML.stringify(document), "utf8");
  }
  for (const [relative, text] of Object.entries(options.files ?? {})) {
    const path = join(destination, relative);
    await mkdir(dirname(path), { recursive: true });
    await writeFile(path, text, "utf8");
  }
  return destination;
}

async function listFiles(root: string): Promise<string[]> {
  const entries = await (await import("node:fs/promises")).readdir(root, { withFileTypes: true });
  const files: string[] = [];
  for (const entry of entries) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) files.push(...await listFiles(path));
    else files.push(path);
  }
  return files.sort();
}

export async function writeText(path: string, text: string): Promise<string> {
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, text.replaceAll("\r\n", "\n"), "utf8");
  return path;
}

export async function writeJson(path: string, value: unknown): Promise<string> {
  return writeText(path, JSON.stringify(value, null, 2));
}

export function taskScript(attempts?: Record<number, unknown[]>, values: Record<string, unknown> = {}): Record<string, unknown> {
  return { ...values, ...(attempts ? { attempts: Object.fromEntries(Object.entries(attempts).map(([key, actions]) => [String(key), actions])) } : {}) };
}

export async function hasCrlf(path: string): Promise<boolean> {
  return (await Bun.file(path).arrayBuffer()).byteLength > 0 && (await readFile(path)).includes(13);
}
