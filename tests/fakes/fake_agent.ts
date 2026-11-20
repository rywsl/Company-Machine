#!/usr/bin/env bun
/**
 * Deterministic headless Harness fixture. It mirrors the stream-json boundary
 * used by official agent CLIs without implementing a model loop.
 */
import { appendFile, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

type JsonObject = Record<string, any>;
type Action = JsonObject;

const virtualPaths: Array<[string, string]> = [
  ["/factory/in", "FACTORY_IN"],
  ["/factory/out", "FACTORY_OUT"],
  ["/factory/ctl", "FACTORY_CTL"],
  ["/workspace", "FACTORY_WORKSPACE"],
];

function realPath(value: string): string {
  for (const [prefix, variable] of virtualPaths) {
    if (value === prefix || value.startsWith(`${prefix}/`)) {
      const base = process.env[variable];
      if (base) return `${base}${value.slice(prefix.length)}`;
    }
  }
  return value;
}

function emit(value: unknown): void {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function parseArgs(): { task: string; model: string; resume?: string } {
  const args = Bun.argv.slice(2);
  const result = { task: "", model: "", resume: undefined as string | undefined };
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if ((arg === "-p" || arg === "--print") && args[index + 1]) result.task = args[++index];
    else if (arg === "--model" && args[index + 1]) result.model = args[++index];
    else if (arg === "--resume" && args[index + 1]) result.resume = args[++index];
    else if (["--append-system-prompt", "--output-format", "--permission-mode", "--mcp-config", "--max-turns"].includes(arg)) index += 1;
  }
  return result;
}

async function loadScript(): Promise<JsonObject> {
  const path = process.env.FAKE_AGENT_SCRIPT ?? join(process.env.FACTORY_IN ?? "/factory/in", "task", "task.json");
  const value = JSON.parse(await readFile(path, "utf8"));
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("脚本必须是 JSON 对象");
  return value;
}

function historyPath(session: string): string {
  const safe = session.replace(/[^A-Za-z0-9_.-]/g, "_");
  return join(realPath("/workspace/.home"), ".fake-agent", `${safe}.jsonl`);
}

async function loadHistory(session: string): Promise<string[]> {
  try {
    const text = await readFile(historyPath(session), "utf8");
    return text.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line).task ?? "");
  } catch {
    return [];
  }
}

async function saveHistory(session: string, task: string): Promise<void> {
  const path = historyPath(session);
  await mkdir(dirname(path), { recursive: true });
  await appendFile(path, `${JSON.stringify({ task })}\n`, "utf8");
}

async function substitute(value: unknown): Promise<unknown> {
  if (Array.isArray(value)) return Promise.all(value.map(substitute));
  if (value && typeof value === "object") return Object.fromEntries(await Promise.all(Object.entries(value).map(async ([key, item]) => [key, await substitute(item)] as const)));
  if (typeof value !== "string") return value;
  if (value.startsWith("@file:")) return (await Bun.file(realPath(value.slice(6))).text()).trim();
  if (value.startsWith("@json:")) {
    const [path, dotted] = value.slice(6).split("#", 2);
    let current: any = JSON.parse(await Bun.file(realPath(path)).text());
    for (const part of dotted?.split(".") ?? []) current = current[part];
    return current;
  }
  return value;
}

async function saveText(path: string, value: string): Promise<void> {
  const target = realPath(path);
  await mkdir(dirname(target), { recursive: true });
  await writeFile(target, value, "utf8");
}

async function runProcess(argv: string[], options: { cwd?: string; input?: string; timeoutMs?: number } = {}): Promise<{ code: number; stdout: string; stderr: string }> {
  const child = Bun.spawn(argv, { cwd: options.cwd, stdin: options.input === undefined ? "ignore" : "pipe", stdout: "pipe", stderr: "pipe" });
  if (options.input !== undefined) {
    await child.stdin.write(options.input);
    await child.stdin.end();
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const result = await Promise.race([
      child.exited.then(async (code) => ({ code, stdout: await new Response(child.stdout).text(), stderr: await new Response(child.stderr).text() })),
      new Promise<never>((_, reject) => { timer = setTimeout(() => { child.kill(); reject(new Error("command timed out")); }, options.timeoutMs ?? 600_000); }),
    ]);
    return result;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function commandArgv(tool: string, args: string[]): string[] {
  return tool.endsWith(".ts") ? [Bun.argv[0], tool, ...args] : [tool, ...args];
}

class AgentError extends Error {}

class Agent {
  private cost: number;
  private readonly turns: number;
  private inputTokens: number | undefined;
  constructor(private readonly opts: { task: string; model: string }, private readonly script: JsonObject, private readonly session: string, private history: string[]) {
    this.cost = Number(script.cost_usd ?? 0);
    this.turns = Number(script.turns ?? 1);
  }
  private say(text: string): void {
    emit({ type: "assistant", session_id: this.session, message: { role: "assistant", content: [{ type: "text", text }] } });
  }
  private finish(ok: boolean, text: string): void {
    const result: JsonObject = { type: "result", subtype: ok ? "success" : "error_during_execution", is_error: !ok, duration_ms: 0, num_turns: this.turns, result: text, session_id: this.session, total_cost_usd: this.cost };
    if (this.inputTokens !== undefined) result.usage = { input_tokens: this.inputTokens };
    emit(result);
  }
  async run(actions: unknown): Promise<number> {
    if (!Array.isArray(actions)) throw new AgentError("动作列表格式不对");
    for (const action of actions) {
      if (!action || typeof action !== "object" || typeof (action as JsonObject).do !== "string") throw new AgentError(`动作格式不对：${JSON.stringify(action)}`);
      this.say(`执行动作 ${(action as JsonObject).do}`);
      const code = await this.step(action as Action);
      if (code !== undefined) return code;
    }
    return 0;
  }
  private async step(action: Action): Promise<number | undefined> {
    switch (action.do) {
      case "write": await saveText(action.path, String(action.text ?? "")); return undefined;
      case "copy": { const target = realPath(String(action.to)); await mkdir(dirname(target), { recursive: true }); await Bun.write(target, await Bun.file(realPath(String(action.from))).arrayBuffer()); return undefined; }
      case "delete": await rm(realPath(String(action.path)), { force: true }); return undefined;
      case "require": if (!(await Bun.file(realPath(String(action.path))).exists())) throw new AgentError(`需要的文件不存在：${action.path}`); return undefined;
      case "ctl": await this.ctl(action); return undefined;
      case "sleep": await Bun.sleep(Number(action.seconds) * 1000); return undefined;
      case "cost": this.cost = Number(action.usd); return undefined;
      case "emit": emit(action.event); return undefined;
      case "garbage": process.stdout.write(`${String(action.text ?? "not json")}\n`); return undefined;
      case "echo_task": await saveText(action.path, this.opts.task); return undefined;
      case "exit": return Number(action.code ?? 0);
      case "run": await this.runCommand(action); return undefined;
      case "history": await saveText(action.path, JSON.stringify(this.history)); return undefined;
      case "usage": this.inputTokens = Number(action.input_tokens); return undefined;
      case "mcp": await this.mcp(action); return undefined;
      default: throw new AgentError(`未知动作 ${action.do}`);
    }
  }
  private async runCommand(action: Action): Promise<void> {
    const argv = (await substitute(action.argv) as unknown[]).map((item) => realPath(String(item)));
    if (!argv.length) throw new AgentError("run 动作需要 argv");
    const result = await runProcess(argv, { cwd: action.cwd ? realPath(String(action.cwd)) : undefined, timeoutMs: Number(action.timeout ?? 600_000) });
    if (action.save) await saveText(action.save, result.stdout);
    const allowed = (action.allow_exit ?? [0]) as number[];
    if (!allowed.includes(result.code)) throw new AgentError(`${argv.join(" ")} 退出码 ${result.code}：${result.stderr.trim()}`);
  }
  private async ctl(action: Action): Promise<void> {
    const tool = process.env.FACTORYCTL;
    if (!tool) throw new AgentError("环境变量 FACTORYCTL 没有设置");
    const result = await runProcess(commandArgv(tool, (action.args ?? []).map(String)), { timeoutMs: Number(action.timeout ?? 600_000) });
    if (action.save) await saveText(action.save, result.stdout);
    const allowed = (action.allow_exit ?? [0]) as number[];
    if (!allowed.includes(result.code)) throw new AgentError(`factoryctl 退出码 ${result.code}：${result.stderr.trim()}`);
  }
  private async mcp(action: Action): Promise<void> {
    const tool = process.env.FACTORYCTL;
    if (!tool) throw new AgentError("环境变量 FACTORYCTL 没有设置");
    const calls = (action.calls ?? []) as JsonObject[];
    const messages = [
      { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "fake-agent", version: "1" } } },
      { jsonrpc: "2.0", method: "notifications/initialized" },
      ...calls.map((call, index) => ({ jsonrpc: "2.0", id: index + 2, method: "tools/call", params: { name: call.name, arguments: call.arguments ?? {} } })),
    ];
    for (const message of messages.slice(2)) message.params.arguments = await substitute(message.params.arguments);
    const result = await runProcess(commandArgv(tool, ["mcp"]), { input: `${messages.map((item) => JSON.stringify(item)).join("\n")}\n`, timeoutMs: Number(action.timeout ?? 600_000) });
    if (result.code !== 0) throw new AgentError(`factoryctl mcp 退出码 ${result.code}：${result.stderr.trim()}`);
    const replies = new Map<number, JsonObject>();
    for (const line of result.stdout.split(/\r?\n/).filter(Boolean)) { const reply = JSON.parse(line) as JsonObject; if (typeof reply.id === "number") replies.set(reply.id, reply); }
    const values = calls.map((call, index) => { const reply = replies.get(index + 2); const value = reply?.result; if (!value || (value.isError && !action.allow_error)) throw new AgentError(`MCP 调用 ${call.name} 失败`); return value; });
    if (action.save) await saveText(action.save, JSON.stringify(values));
  }
  finishWith(code: number): number { this.finish(code === 0, code === 0 ? "完成" : `退出码 ${code}`); return code; }
  finishFailure(error: unknown): number { this.finish(false, error instanceof Error ? error.message : String(error)); return 1; }
}

const opts = parseArgs();
const attempt = process.env.FACTORY_ATTEMPT ?? "1";
const session = opts.resume ?? `fake-${process.env.FACTORY_JOB_ID ?? "job"}-${attempt}`;
const history = await loadHistory(session);
await saveHistory(session, opts.task);
emit({ type: "system", subtype: "init", session_id: session, model: opts.model, tools: [] });
let script: JsonObject;
try { script = await loadScript(); } catch (error) {
  const failed = new Agent(opts, {}, session, history);
  process.exit(failed.finishWith(1));
}
const agent = new Agent(opts, script!, session, history);
const role = process.env.FACTORY_ROLE;
const turn = process.env.FACTORY_TURN;
const actions = role && script!.roles?.[role] !== undefined ? script!.roles[role] : turn && script!.by_turn?.[turn] !== undefined ? script!.by_turn[turn] : script!.attempts?.[attempt] ?? script!.default ?? [];
try { process.exit(agent.finishWith(await agent.run(actions))); }
catch (error) { process.exit(agent.finishFailure(error)); }
