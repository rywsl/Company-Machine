import { randomUUID } from "node:crypto";
import * as fsModule from "node:fs";
import { chmodSync, constants, copyFileSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { copyFile, readdir } from "node:fs/promises";
import { dirname, join, basename, resolve } from "node:path";
import { FactoryError } from "./errors";
import type { ReceiptItem, ReceiptPayload } from "./connectors";

type JsonObject = Record<string, any>;
function ctlDir(): string { const value = process.env.FACTORY_CTL; if (!value) throw new FactoryError("CTL_UNAVAILABLE", "请设置 FACTORY_CTL"); mkdirSync(value, { recursive: true }); return value; }
function atomic(path: string, value: unknown): void { mkdirSync(dirname(path), { recursive: true }); const part = `${path}.${randomUUID()}.part`; writeFileSync(part, `${JSON.stringify(value)}\n`, "utf8"); renameSync(part, path); }
function readJson(path: string): JsonObject { return JSON.parse(readFileSync(path, "utf8")) as JsonObject; }
function mapPath(value: string): string { for (const [prefix, key] of [["/factory/in", "FACTORY_IN"], ["/factory/out", "FACTORY_OUT"], ["/factory/ctl", "FACTORY_CTL"], ["/workspace", "FACTORY_WORKSPACE"]] as const) { const base = process.env[key]; if (base && (value === prefix || value.startsWith(`${prefix}/`))) return `${base}${value.slice(prefix.length)}`; } return value; }
export function inputs(): JsonObject { const job = readJson(join(ctlDir(), "job.json")); if (!job.job_id || job.attempt === undefined || !job.inputs) throw new FactoryError("CTL_REQUEST_INVALID", "job.json 缺字段"); return { job_id: job.job_id, attempt: job.attempt, inputs: job.inputs }; }
export function contract(): string { const path = join(ctlDir(), "contract.json"); if (!existsSync(path)) throw new FactoryError("CTL_REQUEST_INVALID", "contract.json 不存在"); return readFileSync(path, "utf8"); }
export function submit(port: string, sourcePath: string, meta: Record<string, string> = {}): JsonObject {
  if (!pathSegment(port)) throw new FactoryError("CLI_USAGE", "输出端口不合法");
  const ctl = ctlDir();
  const source = mapPath(sourcePath);
  if (!entry(source)?.isFile()) throw new FactoryError("SUBMIT_SOURCE_INVALID", "提交路径必须是普通文件", { path: sourcePath });
  const output = process.env.FACTORY_OUT ?? join(ctl, "..", "out");
  if (!entry(output)) mkdirSync(output, { recursive: true });
  directory(output);
  const portDirectory = join(output, port);
  directory(portDirectory, true);
  const name = basename(source);
  const target = join(portDirectory, name);
  if (resolve(source) !== resolve(target)) {
    const temporary = join(portDirectory, `.submit-${randomUUID()}.part`);
    try {
      copyFileSync(source, temporary, constants.COPYFILE_EXCL);
      renameSync(temporary, target);
    } finally {
      if (existsSync(temporary)) unlinkSync(temporary);
    }
  }
  if (Object.keys(meta).length) atomic(join(portDirectory, "meta.json"), meta);
  const body = { port, source: sourcePath, files: [name], meta, at: new Date().toISOString() };
  atomic(join(ctl, "submissions", `${port}.json`), body);
  return body;
}

function submitArguments(args: string[]): { port: string; path: string; meta: Record<string, string>; json: boolean } {
  const [port, path, ...options] = args;
  if (!port || !path || path.startsWith("--")) throw new FactoryError("CLI_USAGE", "submit 需要 port 和 path");
  const meta: Record<string, string> = Object.create(null);
  let json = false;
  for (let index = 0; index < options.length; index += 1) {
    if (options[index] === "--json") {
      json = true;
      continue;
    }
    if (options[index] !== "--meta") throw new FactoryError("CLI_USAGE", "submit 参数不合法");
    const assignment = options[++index];
    const split = assignment?.indexOf("=") ?? -1;
    if (split <= 0) throw new FactoryError("CLI_USAGE", "--meta 需要 KEY=VALUE");
    meta[assignment.slice(0, split)] = assignment.slice(split + 1);
  }
  return { port, path, meta, json };
}
export function log(message: string): void { mkdirSync(ctlDir(), { recursive: true }); writeFileSync(join(ctlDir(), "log.jsonl"), `${JSON.stringify({ at: new Date().toISOString(), message })}\n`, { encoding: "utf8", flag: "a" }); }
export function fail(reason: string): never { atomic(join(ctlDir(), "fail.json"), { reason }); throw new FactoryError("AGENT_ERROR", reason); }

function publishReceipt(args: string[]): void {
  const payload: ReceiptPayload = { target: "", locator: {}, items: [], note: "" };
  const locator: Array<[string, string]> = [];
  const items: ReceiptItem[] = [];
  for (let index = 0; index < args.length; index += 2) {
    const [flag, value] = [args[index], args[index + 1]];
    if (value === undefined || value.startsWith("--")) throw new FactoryError("CLI_USAGE", `${flag} 需要参数`);
    if (flag === "--target") payload.target = value;
    else if (flag === "--note") payload.note = value;
    else if (flag === "--locator" || flag === "--item") {
      const split = value.indexOf("=");
      if (split < 0) throw new FactoryError("CLI_USAGE", `${flag} 必须是 KEY=VALUE`);
      const key = value.slice(0, split);
      const result = value.slice(split + 1);
      if (flag === "--locator") locator.push([key, result]);
      else {
        const digestIndex = result.lastIndexOf("@sha256:");
        items.push(digestIndex < 0 ? { bean: key, locator: result } : {
          bean: key, locator: result.slice(0, digestIndex), digest: result.slice(digestIndex + 1),
        });
      }
    } else throw new FactoryError("CLI_USAGE", `未知 receipt 参数 ${flag}`);
  }
  if (!payload.target) throw new FactoryError("CLI_USAGE", "receipt 需要 --target");
  atomic(join(ctlDir(), "receipt.json"), { ...payload, locator: Object.fromEntries(locator), items });
}

export interface HarvestRequest {
  outputs: Record<string, { harvest: string; place_as?: string; optional?: boolean; list?: boolean }>;
}

interface HarvestOptions {
  ctl?: string;
  workspace?: string;
  out?: string;
  in?: string;
  request?: HarvestRequest;
}

interface HarvestPortResult {
  ok: boolean;
  files: string[];
  matches: string[];
  code?: "HARVEST_EMPTY" | "HARVEST_AMBIGUOUS" | "HARVEST_IO";
}

export interface HarvestResult {
  ok: boolean;
  ports: Record<string, HarvestPortResult>;
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

const RESERVED_WINDOWS_NAME = /^(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)/i;

function pathSegment(value: string): boolean {
  if (!value || value === "." || value === ".." || /[\\/:\x00-\x1f]/.test(value)) return false;
  return process.platform !== "win32" || (!/[. ]$/.test(value) && !RESERVED_WINDOWS_NAME.test(value));
}

function entry(path: string): ReturnType<typeof lstatSync> | undefined {
  try {
    return lstatSync(path);
  } catch (error) {
    if ((error as { code?: string }).code !== "ENOENT") throw error;
    return undefined;
  }
}

function directory(path: string, create = false): boolean {
  const current = entry(path);
  if (current) {
    if (current.isDirectory() && !current.isSymbolicLink()) return true;
    throw new Error("收货路径必须为普通目录");
  }
  if (!create) return false;
  mkdirSync(path);
  return true;
}

function matchesUnder(root: string, segments: string[]): string[][] {
  if (!directory(root)) return [];
  const walk = (parts: string[], depth: number): string[][] => {
    const dir = join(root, ...parts);
    const pattern = segments[depth];
    // Python's glob treats braces as literal filename characters.
    const glob = /[*?[\]]/.test(pattern) ? new Bun.Glob(pattern.replaceAll("{", "\\{").replaceAll("}", "\\}")) : undefined;
    const names = glob ? (readdirSync(dir, "utf8") as string[]).filter((name) => glob.match(name)) : [pattern];
    const found: string[][] = [];
    for (const name of names) {
      if (!pathSegment(name)) throw new Error("收货路径包含不合法的路径段");
      const candidate = entry(join(dir, name));
      if (!candidate) continue;
      if (candidate.isSymbolicLink()) throw new Error("收货路径不能经过符号链接");
      const path = [...parts, name];
      if (depth + 1 === segments.length) {
        if (candidate.isFile()) found.push(path);
      } else if (candidate.isDirectory()) {
        found.push(...walk(path, depth + 1));
      }
    }
    return found;
  };
  return walk([], 0);
}

function resolvePattern(pattern: string, roots: Record<string, string>): { root: string; parts: string[]; prefix: string } {
  const prefix = pattern.startsWith("/") ? Object.keys(roots).find((key) => pattern.startsWith(key + "/")) : "/workspace";
  if (!prefix) throw new Error("收货模式必须在工厂挂载路径下");
  const relativePath = pattern.startsWith("/") ? pattern.slice(prefix.length + 1) : pattern;
  const parts = relativePath.split("/").filter((part) => part !== ".");
  if (!parts.length || parts.some((part) => !pathSegment(part) || part.includes("**"))) {
    throw new Error("收货模式包含不合法的路径段");
  }
  return { root: roots[prefix], parts, prefix };
}

async function stagedWrite(path: string, write: (scratch: string) => void | Promise<void>): Promise<void> {
  const scratch = join(dirname(path), ".harvest-" + randomUUID() + ".part");
  let created = false;
  try {
    await write(scratch);
    created = true;
    renameSync(scratch, path);
  } finally {
    if (created && existsSync(scratch)) unlinkSync(scratch);
  }
}

async function copyMatch(source: string, out: string, port: string, file: string): Promise<void> {
  directory(out, true);
  const parts = [port, ...file.split("/")];
  let parent = out;
  for (const part of parts.slice(0, -1)) {
    parent = join(parent, part);
    directory(parent, true);
  }
  const target = join(out, ...parts);
  const prior = entry(target);
  if (prior && (prior.isSymbolicLink() || !prior.isFile())) throw new Error("收货目标必须为普通文件");
  const same = process.platform === "win32"
    ? source.toLowerCase() === target.toLowerCase()
    : source === target;
  if (!same) await stagedWrite(target, (scratch) => copyFile(source, scratch, constants.COPYFILE_EXCL));
}

function submitted(ctl: string, port: string): boolean {
  const submissions = join(ctl, "submissions");
  if (!directory(submissions)) return false;
  const record = entry(join(submissions, port + ".json"));
  if (!record) return false;
  if (record.isSymbolicLink() || !record.isFile()) throw new Error("显式提交记录必须为普通文件");
  return true;
}

export async function harvest(options: HarvestOptions = {}): Promise<HarvestResult> {
  let ctl: string;
  try {
    const configured = options.ctl ?? process.env.FACTORY_CTL;
    if (!configured) throw new Error("未配置");
    ctl = resolve(configured);
    if (!directory(ctl)) throw new Error("目录不存在");
  } catch {
    throw new FactoryError("CTL_UNAVAILABLE", "请设置可用的 FACTORY_CTL");
  }
  const roots = {
    "/workspace": resolve(options.workspace ?? process.env.FACTORY_WORKSPACE ?? "/workspace"),
    "/factory/in": resolve(options.in ?? process.env.FACTORY_IN ?? "/factory/in"),
    "/factory/out": resolve(options.out ?? process.env.FACTORY_OUT ?? join(ctl, "..", "out")),
    "/factory/ctl": ctl,
  };
  let request: unknown = options.request;
  if (request === undefined) {
    try {
      const path = join(ctl, "harvest.json");
      const record = entry(path);
      if (!record?.isFile() || record.isSymbolicLink()) throw new Error("需要普通文件");
      request = readJson(path);
    } catch {
      throw new FactoryError("CTL_REQUEST_INVALID", "无法读取 harvest.json");
    }
  }
  if (!object(request) || (Object.hasOwn(request, "outputs") && !object(request.outputs))) {
    throw new FactoryError("CTL_REQUEST_INVALID", "harvest.json 必须包含对象形式的 outputs");
  }
  const ports: Record<string, HarvestPortResult> = Object.create(null);
  for (const [port, declaration] of Object.entries(request.outputs ?? {}).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)) {
    let found: string[] = [];
    try {
      if (!/^[a-z][a-z0-9_]*$/.test(port)) throw new Error("收货端口名称不合法");
      if (submitted(ctl, port)) continue;
      if (!object(declaration) || typeof declaration.harvest !== "string") throw new Error("缺少收货模式");
      const file = declaration.place_as ?? "file";
      if (typeof file !== "string" || !pathSegment(file) || /[*?[\]{}<>|"]/.test(file)) throw new Error("收货文件名不合法");
      const { root, parts, prefix } = resolvePattern(declaration.harvest, roots);
      const matches = matchesUnder(root, parts).map((segments) => segments.join("/")).sort();
      found = matches.map((match) => prefix === "/workspace" ? match : prefix + "/" + match);
      if (!matches.length) {
        ports[port] = declaration.optional === true
          ? { ok: true, files: [], matches: found }
          : { ok: false, code: "HARVEST_EMPTY", files: [], matches: found };
        continue;
      }
      if (declaration.list !== true && matches.length > 1) {
        ports[port] = { ok: false, code: "HARVEST_AMBIGUOUS", files: [], matches: found };
        continue;
      }
      const files: string[] = [];
      for (const [index, match] of matches.entries()) {
        const target = declaration.list === true ? index + "/" + file : file;
        await copyMatch(join(root, ...match.split("/")), roots["/factory/out"], port, target);
        files.push(target);
      }
      ports[port] = { ok: true, files, matches: found };
    } catch {
      ports[port] = { ok: false, code: "HARVEST_IO", files: [], matches: found };
    }
  }
  const result = { ok: Object.values(ports).every((port) => port.ok), ports };
  await stagedWrite(join(ctl, "harvest-result.json"), (scratch) => {
    writeFileSync(scratch, JSON.stringify(result) + "\n", { encoding: "utf8", flag: "wx" });
  });
  return result;
}

export interface CtlRequest {
  id: string;
  op: string;
  args: Record<string, unknown>;
}
export interface CtlResponse {
  id: string;
  ok: boolean;
  result: unknown;
  error: { code: string; message: string } | null;
}
export interface CtlHandler {
  handle(request: CtlRequest): CtlResponse | undefined | Promise<CtlResponse | undefined>;
}
interface CtlServerOptions {
  afterResponse?: (request: CtlRequest, response: CtlResponse) => void | Promise<void>;
  responseMode?: number;
}

const REQUEST_NAME = /^(r-[0-9a-f]{8}(?:[0-9a-f]{24})?)\.json$/;
const MAX_MAILBOX_BYTES = 1024 * 1024;
const POLL_MS = 200;
const fs = fsModule as typeof fsModule & {
  constants: { O_RDONLY: number; O_DIRECTORY?: number; O_NOFOLLOW?: number; O_NONBLOCK?: number };
  openSync(path: string, flags: number): number;
  fstatSync(fd: number): ReturnType<typeof lstatSync>;
  readSync(fd: number, buffer: Uint8Array, offset: number, length: number, position: null): number;
  closeSync(fd: number): void;
};

function mailboxError(id: string, code: string, message: string): CtlResponse {
  return { id, ok: false, result: null, error: { code, message } };
}

export function readMailboxJson(path: string, allowMissing = false): unknown {
  const before = entry(path);
  if (!before && allowMissing) return undefined;
  if (!before?.isFile() || before.isSymbolicLink() || before.nlink !== 1 || before.size > MAX_MAILBOX_BYTES) {
    throw new FactoryError("CTL_REQUEST_INVALID", "邮箱文件必须为大小合规的普通文件");
  }
  const flags = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0);
  const fd = fs.openSync(path, flags);
  try {
    const found = fs.fstatSync(fd);
    if (!found.isFile() || found.nlink !== 1 || found.dev !== before.dev || found.ino !== before.ino || found.size > MAX_MAILBOX_BYTES) {
      throw new FactoryError("CTL_REQUEST_INVALID", "邮箱文件在打开时发生变化");
    }
    const data = new Uint8Array(found.size + 1);
    let offset = 0;
    while (offset < data.length) {
      const count = fs.readSync(fd, data, offset, data.length - offset, null);
      if (!count) break;
      offset += count;
    }
    if (offset !== found.size) throw new FactoryError("CTL_REQUEST_INVALID", "邮箱文件在读取时发生变化");
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(data.subarray(0, offset)));
  } finally {
    fs.closeSync(fd);
  }
}

interface MailboxDirectory { path: string; close(): void; }

function mailboxDirectory(path: string, create: boolean, responses = false): MailboxDirectory {
  try {
    if (!directory(path, create)) throw new Error("目录不存在");
  } catch {
    throw new FactoryError("CTL_UNAVAILABLE", "邮箱目录必须为可用的普通目录");
  }
  if (process.platform !== "linux") return { path, close() {} };

  const { O_RDONLY, O_DIRECTORY = 0, O_NOFOLLOW = 0 } = fs.constants;
  const fd = fs.openSync(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
  try {
    const info = fs.fstatSync(fd);
    const uid = (process as typeof process & { getuid(): number }).getuid();
    if (!info.isDirectory() || info.uid !== uid || (responses && (info.mode & 0o022) !== 0)) {
      throw new FactoryError("CTL_UNAVAILABLE", "邮箱目录权限不合法");
    }
    return { path: "/proc/self/fd/" + fd, close: () => fs.closeSync(fd) };
  } catch (error) {
    fs.closeSync(fd);
    throw error;
  }
}

function validResponse(value: unknown, id: string, allowMissingId = false): value is CtlResponse {
  if (!object(value) || (value.id !== id && !(allowMissingId && value.id === undefined)) || typeof value.ok !== "boolean") return false;
  return value.ok ? Object.hasOwn(value, "result") && (value.error === null || value.error === undefined)
    : object(value.error) && typeof value.error.code === "string" && typeof value.error.message === "string";
}

async function writeMailboxJson(path: string, body: object, mode?: number): Promise<void> {
  await stagedWrite(path, (scratch) => {
    writeFileSync(scratch, JSON.stringify(body) + "\n", { encoding: "utf8", flag: "wx" });
    if (mode !== undefined) chmodSync(scratch, mode);
  });
}

export class CtlServer {
  private loop?: Promise<void>;
  private polling?: Promise<number>;
  private cancel?: AbortController;
  constructor(readonly directory: string, readonly handler: CtlHandler, readonly options: CtlServerOptions = {}) {}

  pollOnce(): Promise<number> {
    if (!this.polling) this.polling = this.poll().finally(() => { this.polling = undefined; });
    return this.polling;
  }

  private async poll(): Promise<number> {
    const opened: MailboxDirectory[] = [];
    try {
      const ctl = mailboxDirectory(this.directory, true);
      opened.push(ctl);
      const responses = mailboxDirectory(join(ctl.path, "responses"), true, true);
      opened.push(responses);
      const requests = mailboxDirectory(join(ctl.path, "requests"), true);
      opened.push(requests);
      let written = 0;
      for (const name of (await readdir(requests.path)).sort()) {
        const id = REQUEST_NAME.exec(name)?.[1];
        if (!id) continue;
        const responsePath = join(responses.path, name);
        const previous = entry(responsePath);
        if (previous) {
          if (!previous.isFile() || previous.isSymbolicLink() || previous.nlink !== 1) {
            throw new FactoryError("CTL_UNAVAILABLE", "邮箱响应路径必须为普通文件");
          }
          continue;
        }

        let body: unknown;
        try { body = readMailboxJson(join(requests.path, name)); }
        catch { body = undefined; }
        if (!object(body) || body.id !== id || typeof body.op !== "string" || !body.op || !object(body.args)) {
          await writeMailboxJson(responsePath, mailboxError(id, "CTL_REQUEST_INVALID", "请求文件不合法"), this.options.responseMode);
          written += 1;
          continue;
        }
        const request = { id, op: body.op, args: body.args };
        let response: CtlResponse | undefined;
        try {
          response = await this.handler.handle(request);
          if (response === undefined) continue;
          if (!validResponse(response, id)) throw new Error("响应格式不合法");
        } catch {
          response = mailboxError(id, "CTL_HANDLER_ERROR", "宿主处理请求失败");
        }
        await writeMailboxJson(responsePath, response, this.options.responseMode);
        written += 1;
        await this.options.afterResponse?.(request, response);
      }
      return written;
    } finally {
      for (const dir of opened.reverse()) dir.close();
    }
  }

  start(intervalMs = POLL_MS): void {
    if (this.loop) throw new Error("CtlServer already running");
    if (!Number.isFinite(intervalMs) || intervalMs <= 0) throw new RangeError("intervalMs must be positive");
    const cancel = new AbortController();
    this.cancel = cancel;
    this.loop = this.pollLoop(cancel.signal, intervalMs);
    void this.loop.catch(() => {
      // stop() propagates a failed poll after the active operation has stopped.
    });
  }

  async stop(): Promise<void> {
    this.cancel?.abort();
    try {
      await (this.loop ?? this.polling);
    } finally {
      this.loop = undefined;
      this.cancel = undefined;
    }
  }

  private async pollLoop(signal: AbortSignal, intervalMs: number): Promise<void> {
    while (!signal.aborted) {
      await this.pollOnce();
      if (signal.aborted) break;
      await new Promise<void>((resolve) => {
        const finish = () => {
          clearTimeout(timer);
          signal.removeEventListener("abort", finish);
          resolve();
        };
        const timer = setTimeout(finish, intervalMs);
        signal.addEventListener("abort", finish, { once: true });
      });
    }
  }
}

interface RequestOptions {
  ctl?: string;
  timeoutMs?: number;
  pollIntervalMs?: number;
}

function requestTimeout(ctl: string): number {
  const path = join(ctl, "contract.json");
  if (!entry(path)) return 605_000;
  const contract = readMailboxJson(path);
  const approval = object(contract) && object(contract.approval) ? contract.approval : undefined;
  const seconds = approval?.timeout_s;
  return typeof seconds === "number" && Number.isFinite(seconds) && seconds >= 0 ? (seconds + 5) * 1000 : 605_000;
}

export async function request(op: string, args: Record<string, unknown>, options: RequestOptions = {}): Promise<CtlResponse> {
  const ctl = options.ctl ?? ctlDir();
  try {
    if (!directory(ctl)) throw new Error("目录不存在");
  } catch {
    throw new FactoryError("CTL_UNAVAILABLE", "控制通道目录必须为可用的普通目录");
  }
  const requests = join(ctl, "requests");
  const responses = join(ctl, "responses");
  directory(requests, true);
  const id = "r-" + randomUUID().replaceAll("-", "");
  const file = id + ".json";
  const timeoutMs = options.timeoutMs ?? requestTimeout(ctl);
  const intervalMs = options.pollIntervalMs ?? POLL_MS;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || !Number.isFinite(intervalMs) || intervalMs <= 0) {
    throw new RangeError("Mailbox timeout and interval must be positive");
  }
  await writeMailboxJson(join(requests, file), { id, op, args });
  const deadline = performance.now() + timeoutMs;
  while (true) {
    const response = join(responses, file);
    if (directory(responses) && entry(response)) {
      let value: unknown;
      try { value = readMailboxJson(response); }
      catch { throw new FactoryError("CTL_RESPONSE_INVALID", "响应文件不合法"); }
      if (!validResponse(value, id, true)) throw new FactoryError("CTL_RESPONSE_INVALID", "响应数据不合法");
      return { ...value, id, result: value.result ?? null, error: value.error ?? null };
    }
    const remaining = deadline - performance.now();
    if (remaining <= 0) throw new FactoryError("CTL_TIMEOUT", "等待宿主响应超时");
    await new Promise<void>((resolve) => setTimeout(resolve, Math.min(intervalMs, remaining)));
  }
}

function approvalArguments(argv: string[]): Record<string, unknown> {
  if (argv[0] !== "request") throw new FactoryError("CLI_USAGE", "approval 需要 request 子命令");
  const flags = new Map<string, string>();
  for (let index = 1; index < argv.length; index += 2) {
    const [flag, value] = argv.slice(index, index + 2);
    if (!["--kind", "--summary", "--cost-usd"].includes(flag) || flags.has(flag) || value === undefined || !value.trim()) {
      throw new FactoryError("CLI_USAGE", "approval request 参数不合法");
    }
    flags.set(flag, value);
  }
  const kind = flags.get("--kind");
  const summary = flags.get("--summary");
  const inputCost = flags.get("--cost-usd") ?? "0";
  const cost = Number(inputCost);
  if (!kind || !summary || !/^\+?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(inputCost) || !Number.isFinite(cost)) {
    throw new FactoryError("CLI_USAGE", "approval request 需要 --kind、--summary 和有限非负费用");
  }
  return { kind, summary, cost_usd: cost };
}

export function budget(): Record<string, unknown> {
  const ctl = ctlDir();
  const path = join(ctl, "budget.json");
  if (entry(path)) {
    const body = readMailboxJson(path);
    if (!object(body)) throw new FactoryError("CTL_RESPONSE_INVALID", "budget.json 必须为对象");
    return body;
  }
  let job: unknown;
  try { job = readJson(join(ctl, "job.json")); } catch { job = undefined; }
  if (!object(job) || typeof job.job_id !== "string") throw new FactoryError("CTL_UNAVAILABLE", "job.json 缺字段");
  return { budget_usd: job.budget_usd ?? 0, spent_usd: null, remaining_usd: null, note: "余额未知" };
}

export interface ProposalPayload {
  summary: string;
  cost_estimate_usd: number;
  time_estimate_s: number;
  plan?: Record<string, unknown>;
}

export function proposalPayload(value: unknown): ProposalPayload {
  if (!object(value) || typeof value.summary !== "string" || !value.summary.trim()
    || typeof value.cost_estimate_usd !== "number" || !Number.isFinite(value.cost_estimate_usd) || value.cost_estimate_usd < 0
    || typeof value.time_estimate_s !== "number" || !Number.isFinite(value.time_estimate_s) || value.time_estimate_s < 0
    || (value.plan !== undefined && !object(value.plan))) {
    throw new FactoryError("PROPOSAL_INVALID", "方案需要摘要、有限非负费用及时间，plan 必须为对象");
  }
  const result: ProposalPayload = { summary: value.summary, cost_estimate_usd: value.cost_estimate_usd, time_estimate_s: value.time_estimate_s };
  if (object(value.plan)) result.plan = value.plan;
  return result;
}

export function readProposal(ctl: string, jobId?: string): ProposalPayload {
  const path = join(ctl, "proposal.json");
  try {
    if (!directory(ctl) || !entry(path)) throw new FactoryError("PROPOSAL_MISSING", "没有 proposal.json", { jobId });
    return proposalPayload(readMailboxJson(path));
  } catch (error) {
    if (error instanceof FactoryError && (error.code === "PROPOSAL_MISSING" || error.code === "PROPOSAL_INVALID")) throw error;
    throw new FactoryError("PROPOSAL_INVALID", "无法读取方案文件", { jobId });
  }
}

export async function writeProposal(ctl: string, payload: ProposalPayload): Promise<void> {
  const validated = proposalPayload(payload);
  if (!directory(ctl) || new TextEncoder().encode(JSON.stringify(validated)).length >= MAX_MAILBOX_BYTES) {
    throw new FactoryError("PROPOSAL_INVALID", "方案目录或文件大小不合法");
  }
  await writeMailboxJson(join(ctl, "proposal.json"), validated);
}

function proposalArguments(argv: string[]): ProposalPayload {
  const fields = new Map<string, string>();
  for (let index = 0; index < argv.length; index += 2) {
    const [name, value] = argv.slice(index, index + 2);
    if (!["--summary", "--cost-usd", "--time-s", "--plan"].includes(name) || fields.has(name) || !value?.trim()) {
      throw new FactoryError("CLI_USAGE", "proposal 参数不合法");
    }
    fields.set(name, value);
  }
  const plan = fields.get("--plan");
  const cost = fields.get("--cost-usd") ?? "";
  const seconds = fields.get("--time-s") ?? "";
  const decimal = /^\+?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;
  if (!decimal.test(cost) || !decimal.test(seconds)) throw new FactoryError("CLI_USAGE", "方案费用和时间必须是有限非负数");
  try {
    return proposalPayload({
      summary: fields.get("--summary"), cost_estimate_usd: Number(cost), time_estimate_s: Number(seconds),
      plan: plan === undefined ? undefined : readMailboxJson(mapPath(plan)),
    });
  } catch {
    throw new FactoryError("CLI_USAGE", "方案摘要、费用、时间或 plan JSON 不合法");
  }
}

export async function main(argv = Bun.argv.slice(2)): Promise<number> {
  const command = argv[0];
  try {
    if (command === "proposal") { await writeProposal(ctlDir(), proposalArguments(argv.slice(1))); return 0; }
    if (command === "budget") {
      if (argv.length > 2 || (argv.length === 2 && argv[1] !== "--json")) throw new FactoryError("CLI_USAGE", "budget 参数不合法");
      process.stdout.write(JSON.stringify(budget()) + "\n");
      return 0;
    }
    if (command === "check" || command === "approval") {
      if (command === "check" && (argv.length !== 3 || !argv[1] || !argv[2])) {
        throw new FactoryError("CLI_USAGE", "check 需要 port 和 path");
      }
      const args = command === "check" ? { port: argv[1], path: argv[2] } : approvalArguments(argv.slice(1));
      const response = await request(command, args);
      if (response.ok) {
        process.stdout.write(JSON.stringify(response.result) + "\n");
        return 0;
      }
      process.stderr.write((response.error?.message ?? "宿主拒绝了请求") + "\n");
      return 1;
    }
    if (command === "inputs") { const value = inputs(); process.stdout.write(`${JSON.stringify(value)}\n`); return 0; }
    if (command === "contract") { process.stdout.write(contract().replace(/\n?$/, "\n")); return 0; }
    if (command === "submit") {
      const { port, path, meta, json } = submitArguments(argv.slice(1));
      const result = submit(port, path, meta);
      process.stdout.write((json ? JSON.stringify(result) : `submitted ${port} ${result.files[0]}`) + "\n");
      return 0;
    }
    if (command === "log") { log(argv.slice(1).join(" ")); return 0; }
    if (command === "receipt") { publishReceipt(argv.slice(1)); return 0; }
    if (command === "harvest") { await harvest(); return 0; }
    if (command === "fail") { fail(argv.slice(1).join(" ")); }
    throw new FactoryError("CLI_USAGE", "未知 factoryctl 命令");
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    if (error instanceof FactoryError && error.code === "CLI_USAGE") return 2;
    if (command === "check" || command === "approval" || command === "budget") {
      if (error instanceof FactoryError && error.code === "CLI_USAGE") return 2;
      return error instanceof FactoryError && error.code === "CTL_UNAVAILABLE" ? 4 : 1;
    }
    if (command === "harvest") {
      if (error instanceof FactoryError && error.code === "CTL_REQUEST_INVALID") return 2;
      return error instanceof FactoryError && error.code === "CTL_UNAVAILABLE" ? 4 : 1;
    }
    if (command === "receipt" && error instanceof FactoryError && error.code === "CLI_USAGE") return 2;
    return error instanceof FactoryError && (error.code === "AGENT_ERROR" || error.code === "SUBMIT_SOURCE_INVALID") ? 1 : 4;
  }
}
if (import.meta.main) process.exit(await main());
