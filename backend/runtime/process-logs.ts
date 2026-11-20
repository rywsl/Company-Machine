import { closeSync, mkdirSync, openSync } from "node:fs";
import { dirname } from "node:path";

export type LogPaths = { stdout: string; stderr: string };
export type LoggedProcess = { exited: Promise<number>; kill(signal?: number): void };

export function spawnLoggedProcess(argv: string[], logs: LogPaths, options: { cwd?: string; env?: Record<string, string> } = {}): LoggedProcess {
  const bun = Bun as unknown as { spawn(argv: string[], options: Record<string, unknown>): LoggedProcess };
  mkdirSync(dirname(logs.stdout), { recursive: true });
  mkdirSync(dirname(logs.stderr), { recursive: true });
  const stdout = openSync(logs.stdout, "w");
  try {
    const stderr = openSync(logs.stderr, "w");
    try {
      return bun.spawn(argv, { ...options, stdout, stderr });
    } finally {
      closeSync(stderr);
    }
  } finally {
    closeSync(stdout);
  }
}
