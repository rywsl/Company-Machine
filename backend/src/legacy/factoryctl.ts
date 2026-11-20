import { existsSync, mkdirSync, readFileSync, writeFileSync, renameSync } from "node:fs";
import { readdir } from "node:fs/promises";
import { dirname, join, basename } from "node:path";
import { FactoryError } from "./errors";

type JsonObject = Record<string, any>;
function ctlDir(): string { const value = process.env.FACTORY_CTL; if (!value) throw new FactoryError("CTL_UNAVAILABLE", "请设置 FACTORY_CTL"); mkdirSync(value, { recursive: true }); return value; }
function atomic(path: string, value: unknown): void { mkdirSync(dirname(path), { recursive: true }); const part = `${path}.${Date.now()}.part`; writeFileSync(part, `${JSON.stringify(value)}\n`, "utf8"); renameSync(part, path); }
function readJson(path: string): JsonObject { return JSON.parse(readFileSync(path, "utf8")) as JsonObject; }
function mapPath(value: string): string { for (const [prefix, key] of [["/factory/in", "FACTORY_IN"], ["/factory/out", "FACTORY_OUT"], ["/factory/ctl", "FACTORY_CTL"], ["/workspace", "FACTORY_WORKSPACE"]] as const) { const base = process.env[key]; if (base && (value === prefix || value.startsWith(`${prefix}/`))) return `${base}${value.slice(prefix.length)}`; } return value; }
export function inputs(): JsonObject { const job = readJson(join(ctlDir(), "job.json")); if (!job.job_id || job.attempt === undefined || !job.inputs) throw new FactoryError("CTL_REQUEST_INVALID", "job.json 缺字段"); return { job_id: job.job_id, attempt: job.attempt, inputs: job.inputs }; }
export function contract(): string { const path = join(ctlDir(), "contract.json"); if (!existsSync(path)) throw new FactoryError("CTL_REQUEST_INVALID", "contract.json 不存在"); return readFileSync(path, "utf8"); }
export function submit(port: string, sourcePath: string, meta: Record<string, string> = {}): JsonObject { const source = mapPath(sourcePath); if (!existsSync(source)) throw new FactoryError("CTL_REQUEST_INVALID", "提交的文件不存在", { path: sourcePath }); const out = join(process.env.FACTORY_OUT ?? join(ctlDir(), "..", "out"), port); mkdirSync(out, { recursive: true }); const name = basename(source); const target = join(out, name); writeFileSync(target, readFileSync(source)); if (Object.keys(meta).length) atomic(join(out, "meta.json"), meta); const body = { port, source: sourcePath, files: [name], meta, at: new Date().toISOString() }; atomic(join(ctlDir(), "submissions", `${port}.json`), body); return body; }
export function log(message: string): void { mkdirSync(ctlDir(), { recursive: true }); writeFileSync(join(ctlDir(), "log.jsonl"), `${JSON.stringify({ at: new Date().toISOString(), message })}\n`, { encoding: "utf8", flag: "a" }); }
export function fail(reason: string): never { atomic(join(ctlDir(), "fail.json"), { reason }); throw new FactoryError("AGENT_ERROR", reason); }

export interface CtlHandler { handle(request: JsonObject): JsonObject | Promise<JsonObject> | undefined; }
export class CtlServer {
  private running = false;
  private current = "";
  constructor(readonly directory: string, readonly handler: CtlHandler) {}
  async pollOnce(): Promise<number> {
    const requests = join(this.directory, "requests"); mkdirSync(requests, { recursive: true }); let written = 0;
    for (const name of await readdir(requests)) {
      if (!name.endsWith(".json")) continue;
      const id = name.slice(0, -5); const response = join(this.directory, "responses", name); if (existsSync(response)) continue;
      let body: JsonObject | undefined; try { body = readJson(join(requests, name)); } catch { body = undefined; }
      if (!body || body.id !== id || typeof body.op !== "string") { atomic(response, { id, ok: false, result: null, error: { code: "CTL_REQUEST_INVALID", message: "请求文件不合法" } }); written += 1; continue; }
      this.current = id;
      try { const result = await this.handler.handle(body); if (result !== undefined) { atomic(response, result); written += 1; } }
      catch (error) { atomic(response, { id, ok: false, result: null, error: { code: "CTL_HANDLER_ERROR", message: error instanceof Error ? error.message : String(error) } }); written += 1; }
    }
    return written;
  }
  start(intervalMs = 200): void { if (this.running) throw new Error("CtlServer already running"); this.running = true; void (async () => { while (this.running) { await this.pollOnce(); await new Promise((resolve) => setTimeout(resolve, intervalMs)); } })(); }
  stop(): void { this.running = false; }
}

export async function main(argv = Bun.argv.slice(2)): Promise<number> {
  try {
    const command = argv[0];
    if (command === "inputs") { const value = inputs(); process.stdout.write(`${JSON.stringify(value)}\n`); return 0; }
    if (command === "contract") { process.stdout.write(contract().replace(/\n?$/, "\n")); return 0; }
    if (command === "submit") { const port = argv[1]; const path = argv[2]; if (!port || !path) throw new FactoryError("CLI_USAGE", "submit 需要 port 和 path"); process.stdout.write(`${JSON.stringify(submit(port, path))}\n`); return 0; }
    if (command === "log") { log(argv.slice(1).join(" ")); return 0; }
    if (command === "fail") { fail(argv.slice(1).join(" ")); }
    throw new FactoryError("CLI_USAGE", "未知 factoryctl 命令");
  } catch (error) { process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`); return error instanceof FactoryError && error.code === "AGENT_ERROR" ? 1 : 4; }
}
if (import.meta.main) process.exit(await main());
