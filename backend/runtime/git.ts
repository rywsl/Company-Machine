import { existsSync } from "node:fs";
import { basename, dirname, join } from "node:path";

export function resolveGitExecutable(): string {
  const executable = Bun.which("git") ?? "git";
  if (process.platform !== "win32" || basename(dirname(executable)).toLowerCase() !== "cmd") return executable;

  // Git for Windows ships the real executable alongside its PATH launcher.
  const installation = dirname(dirname(executable));
  return ["mingw64", "mingw32"].map((platform) => join(installation, platform, "bin", "git.exe")).find(existsSync) ?? executable;
}
