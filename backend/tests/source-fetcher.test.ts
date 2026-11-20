import { expect, test } from "bun:test";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { SourceFetcher, SourceToolUnavailableError, githubArchiveApiUrl } from "../src/source-fetcher";

test("source fetcher uses a persistent GitHub archive cache when Git is unavailable", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-source-fetcher-"));
  const calls: string[] = [];
  try {
    const fetcher = new SourceFetcher({
      cacheRoot: root,
      gitExecutable: "",
      fetcher: async (input) => {
        calls.push(String(input));
        if (String(input).includes("api.github.com")) return new Response(JSON.stringify({ default_branch: "main" }), { status: 200 });
        return new Response(new Uint8Array([1, 2, 3]), { status: 200 });
      },
      runner: async (argv) => {
        if (argv.includes("-tzf")) return { exitCode: 0, stdout: new TextEncoder().encode("owner-repo-main/README.md\n"), stderr: new Uint8Array() };
        const target = argv[argv.indexOf("-C") + 1];
        await Bun.write(join(target, "README.md"), "source");
        return { exitCode: 0, stdout: new Uint8Array(), stderr: new Uint8Array() };
      },
    });
    const first = await fetcher.fetch("https://github.com/owner/repo.git");
    expect(first.tool).toBe("github-archive");
    expect(first.revision).toMatch(/^archive:sha256:/);
    expect(readFileSync(join(first.checkout, "README.md"), "utf8")).toBe("source");
    const callCount = calls.length;
    const second = await fetcher.fetch("https://github.com/owner/repo.git");
    expect(second).toEqual(first);
    expect(calls.length).toBe(callCount);
    expect(existsSync(join(first.checkout, ".mao-source.json"))).toBe(true);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("source fetcher rejects unsafe archive paths and unsupported repositories without Git", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-source-fetcher-"));
  try {
    const unsafe = new SourceFetcher({
      cacheRoot: root,
      gitExecutable: "",
      fetcher: async (input) => String(input).includes("api.github.com")
        ? new Response(JSON.stringify({ default_branch: "main" }), { status: 200 })
        : new Response(new Uint8Array([1]), { status: 200 }),
      runner: async (argv) => argv.includes("-tzf")
        ? { exitCode: 0, stdout: new TextEncoder().encode("../escape\n"), stderr: new Uint8Array() }
        : { exitCode: 0, stdout: new Uint8Array(), stderr: new Uint8Array() },
    });
    await expect(unsafe.fetch("https://github.com/owner/repo")).rejects.toThrow("unsafe path");

    const unsupported = new SourceFetcher({ cacheRoot: root, gitExecutable: "", fetcher: async () => new Response(null, { status: 500 }) });
    await expect(unsupported.fetch("https://git.example.com/owner/repo.git")).rejects.toBeInstanceOf(SourceToolUnavailableError);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("source fetcher exposes a canonical GitHub metadata endpoint", () => {
  expect(githubArchiveApiUrl("https://github.com/openai/example.git")).toBe("https://api.github.com/repos/openai/example");
  expect(githubArchiveApiUrl("https://git.example.com/openai/example")).toBeUndefined();
});
