import { createHash, randomBytes, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { FactoryError } from "./errors";

const DURATION = /^(\d+)(ms|s|m|h|d)$/;
const UNITS: Record<string, number> = { ms: 0.001, s: 1, m: 60, h: 3600, d: 86400 };

function reject(path: string): never { throw new FactoryError("JSON_INVALID", "规范 JSON 含不允许的值", { path }); }
function jsonValue(value: unknown, path = "$"): unknown {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") { if (!Number.isFinite(value)) return reject(path); return Object.is(value, -0) ? 0 : value; }
  if (Array.isArray(value)) return value.map((item, index) => jsonValue(item, `${path}[${index}]`));
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0);
    return Object.fromEntries(entries.map(([key, item]) => [key, jsonValue(item, `${path}.${key}`)]));
  }
  return reject(path);
}
export function canonicalJson(value: unknown): string { return JSON.stringify(jsonValue(value)); }
export function canonicalBytes(value: unknown): Uint8Array { return new TextEncoder().encode(canonicalJson(value)); }
export function digestBytes(value: Uint8Array): string { return `sha256:${createHash("sha256").update(value).digest("hex")}`; }
export function digestFileSync(path: string): string { return `sha256:${createHash("sha256").update(readFileSync(path)).digest("hex")}`; }
export async function digestFile(path: string): Promise<string> {
  const bun = (globalThis as { Bun?: { file: (path: string) => { stream(): AsyncIterable<Uint8Array> } } }).Bun;
  if (!bun) return digestFileSync(path);
  const hash = createHash("sha256");
  for await (const chunk of bun.file(path).stream()) hash.update(chunk);
  return `sha256:${hash.digest("hex")}`;
}
export function utcIso(date = new Date()): string { return date.toISOString(); }
export function parseDuration(value: string): number {
  const match = DURATION.exec(value);
  if (!match) throw new FactoryError("DURATION_INVALID", "时长字符串不合法", { text: value });
  return Number(match[1]) * UNITS[match[2]];
}
export interface Clock { now(): Date; monotonic(): number; sleep(seconds: number): Promise<void>; }
export class SystemClock implements Clock {
  now(): Date { return new Date(); }
  monotonic(): number { return performance.now() / 1000; }
  async sleep(seconds: number): Promise<void> { await new Promise((resolve) => setTimeout(resolve, seconds * 1000)); }
}
export class FakeClock implements Clock {
  private elapsed = 0;
  constructor(private readonly start = new Date("2026-01-01T00:00:00.000Z")) {}
  now(): Date { return new Date(this.start.getTime() + this.elapsed * 1000); }
  monotonic(): number { return this.elapsed; }
  async sleep(seconds: number): Promise<void> { this.advance(seconds); }
  advance(seconds: number): void { if (seconds < 0) throw new RangeError("clock cannot move backwards"); this.elapsed += seconds; }
}
export interface IdSource { new: (prefix: string) => string; }
export function randomUuid(): string { return randomUUID(); }
function checkPrefix(prefix: string): void { if (!/^[a-z]{1,8}$/.test(prefix)) throw new Error("ID prefix is invalid"); }
export class CounterIds implements IdSource {
  private counts = new Map<string, number>();
  new(prefix: string): string { checkPrefix(prefix); const next = (this.counts.get(prefix) ?? 0) + 1; this.counts.set(prefix, next); return `${prefix}-${String(next).padStart(4, "0")}`; }
}
export class RandomIds implements IdSource {
  new(prefix: string): string { checkPrefix(prefix); return `${prefix}-${randomBytes(8).toString("hex")}`; }
}
