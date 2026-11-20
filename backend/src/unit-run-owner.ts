import { readFileSync, statSync } from "node:fs";
import { randomUuid } from "./legacy/foundation";

export interface UnitRunOwner {
  token: string;
  scope: string | null;
  pid: number;
}

function linuxProcessScope(): string | null {
  if (process.platform !== "linux") return null;
  try {
    const bootId = readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim();
    const namespace = statSync("/proc/self/ns/pid").ino;
    if (/^[0-9a-f-]{36}$/.test(bootId) && Number.isSafeInteger(namespace) && namespace > 0) {
      return `${bootId}:${namespace}`;
    }
  } catch { /* A missing procfs cannot prove process ownership. */ }
  return null;
}

export const unitRunProcessScope = linuxProcessScope();

export function createUnitRunOwner(): UnitRunOwner {
  return { token: randomUuid(), scope: unitRunProcessScope, pid: process.pid };
}

export function unitRunOwnerExited(owner: UnitRunOwner, scope: string): boolean {
  if (owner.scope !== scope || !Number.isSafeInteger(owner.pid) || owner.pid <= 0 || owner.pid > 2147483647) return false;
  try {
    process.kill(owner.pid, 0);
    return false;
  } catch (error) {
    return (error as { code?: string }).code === "ESRCH";
  }
}
