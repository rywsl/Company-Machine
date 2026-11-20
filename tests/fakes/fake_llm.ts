#!/usr/bin/env bun
/** Minimal deterministic Anthropic Messages and OpenAI Responses fixture. */
import { createHash } from "node:crypto";

type JsonObject = Record<string, any>;

function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.map((block: any) => block?.type === "tool_result" ? textOf(block.content) : ["text", "input_text", "output_text"].includes(block?.type) ? String(block.text ?? "") : "").filter(Boolean).join("\n");
}

function promptOf(content: unknown): string {
  if (!Array.isArray(content)) return textOf(content);
  return textOf(content.filter((block: any) => block?.type !== "tool_result"));
}

export class FakeLlm {
  readonly requests: JsonObject[] = [];
  private server?: ReturnType<typeof Bun.serve>;
  private readonly counters = new Map<string, number>();
  constructor(readonly scriptPath: string) {}

  start(host = "127.0.0.1", port = 0): number {
    this.server = Bun.serve({ hostname: host, port, fetch: (request) => this.handle(request) });
    return this.server.port;
  }

  stop(): void { this.server?.stop(); this.server = undefined; }

  private async handle(request: Request): Promise<Response> {
    if (request.method !== "POST") return Response.json({ error: { type: "not_found", message: request.url } }, { status: 404 });
    const path = new URL(request.url).pathname;
    let body: JsonObject;
    try { body = await request.json() as JsonObject; } catch { return Response.json({ error: { type: "invalid_request_error", message: "bad json" } }, { status: 400 }); }
    const headers = Object.fromEntries(request.headers.entries());
    if (path === "/v1/messages/count_tokens") {
      this.record(path, body, headers, null, false);
      return Response.json({ input_tokens: 10 });
    }
    const root = JSON.parse(await Bun.file(this.scriptPath).text()) as JsonObject;
    try {
      if (path === "/v1/messages") return this.anthropic(path, body, headers, root.anthropic ?? root);
      if (path === "/v1/responses" || path === "/responses") return this.openai(path, body, headers, root.openai ?? root);
    } catch (error) {
      return Response.json({ error: { type: "api_error", message: `fake-llm: ${error instanceof Error ? error.message : String(error)}` } }, { status: 500 });
    }
    return Response.json({ error: { type: "not_found", message: path } }, { status: 404 });
  }

  private record(path: string, body: JsonObject, headers: JsonObject, turn: number | null, stream: boolean, last = "", prompt = ""): void {
    this.requests.push({ path, model: body.model, headers, turn, stream, last_result: last, prompt });
  }

  private blocks(script: JsonObject, model: string, turn: number, tools: string[], last: string, prompt: string): JsonObject[] {
    const main = script.main_models ?? [];
    const turns = script.turns ?? [];
    if ((main.length && !main.includes(model)) || turn >= turns.length) return [{ type: "text", text: String(script.fallback ?? "ok") }];
    return (turns[turn] ?? []).map((raw: JsonObject) => {
      if (raw.text !== undefined) return { type: "text", text: String(raw.text) };
      const spec = raw.tool ?? {};
      const matches = tools.filter((name) => spec.name ? name === spec.name : name.endsWith(String(spec.suffix ?? "")));
      if (matches.length !== 1) throw new Error(`tool ${JSON.stringify(spec)} matched ${matches.join(",")}`);
      return { type: "tool", name: matches[0], input: this.substitute(raw.input ?? {}, last, prompt) };
    });
  }

  private substitute(value: unknown, last: string, prompt: string): unknown {
    if (Array.isArray(value)) return value.map((item) => this.substitute(item, last, prompt));
    if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, this.substitute(item, last, prompt)]));
    if (value === "@last_result") return last.trim();
    if (typeof value === "string" && value.startsWith("@prompt:")) return prompt.match(new RegExp(value.slice(8)))?.[1] ?? (() => { throw new Error(`prompt does not match ${value}`); })();
    return value;
  }

  private anthropic(path: string, body: JsonObject, headers: JsonObject, script: JsonObject): Response {
    const messages = body.messages ?? [];
    const model = String(body.model ?? "");
    const turn = messages.filter((message: JsonObject) => message.role === "assistant").length;
    const tools = (body.tools ?? []).map((tool: JsonObject) => String(tool.name));
    const users = messages.filter((message: JsonObject) => message.role === "user");
    const prompt = users.map((message: JsonObject) => promptOf(message.content)).join("\n");
    const lastContent = users.at(-1)?.content;
    const last = Array.isArray(lastContent) ? textOf(lastContent.filter((block: JsonObject) => block.type === "tool_result").at(-1)?.content) : "";
    const stream = Boolean(body.stream);
    this.record(path, body, headers, turn, stream, last, prompt);
    const blocks = this.blocks(script, model, turn, tools, last, prompt);
    const usage = script.usage ?? { input_tokens: 100, output_tokens: 50 };
    const content = blocks.map((block, index) => block.type === "text" ? { type: "text", text: block.text } : { type: "tool_use", id: `toolu_${turn}_${index}`, name: block.name, input: block.input });
    const stop = content.some((item) => item.type === "tool_use") ? "tool_use" : "end_turn";
    if (!stream) return Response.json({ id: `msg_${turn}`, type: "message", role: "assistant", model, content, stop_reason: stop, stop_sequence: null, usage });
    const events: string[] = [`event: message_start\ndata: ${JSON.stringify({ type: "message_start", message: { id: `msg_${turn}`, type: "message", role: "assistant", model, content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: usage.input_tokens, output_tokens: 1 } } })}\n\n`];
    content.forEach((item, index) => {
      const start = item.type === "text" ? { type: "text", text: "" } : { type: "tool_use", id: item.id, name: item.name, input: {} };
      const delta = item.type === "text" ? { type: "text_delta", text: item.text } : { type: "input_json_delta", partial_json: JSON.stringify(item.input) };
      events.push(`event: content_block_start\ndata: ${JSON.stringify({ type: "content_block_start", index, content_block: start })}\n\n`);
      events.push(`event: content_block_delta\ndata: ${JSON.stringify({ type: "content_block_delta", index, delta })}\n\n`);
      events.push(`event: content_block_stop\ndata: ${JSON.stringify({ type: "content_block_stop", index })}\n\n`);
    });
    events.push(`event: message_delta\ndata: ${JSON.stringify({ type: "message_delta", delta: { stop_reason: stop, stop_sequence: null }, usage: { output_tokens: usage.output_tokens } })}\n\n`);
    events.push(`event: message_stop\ndata: ${JSON.stringify({ type: "message_stop" })}\n\n`);
    return new Response(events.join(""), { headers: { "content-type": "text/event-stream" } });
  }

  private openai(path: string, body: JsonObject, headers: JsonObject, script: JsonObject): Response {
    const items = typeof body.input === "string" ? [{ type: "message", role: "user", content: body.input }] : body.input ?? [];
    const model = String(body.model ?? "");
    const users = items.filter((item: JsonObject) => (item.type ?? "message") === "message" && item.role === "user");
    const prompt = users.map((item: JsonObject) => promptOf(item.content)).join("\n");
    const outputs = items.filter((item: JsonObject) => item.type === "function_call_output");
    const last = outputs.at(-1)?.output ?? "";
    const key = createHash("sha256").update(prompt).digest("hex");
    const turn = this.counters.get(key) ?? 0;
    if (!(script.main_models?.length) || script.main_models.includes(model)) this.counters.set(key, turn + 1);
    const tools = (body.tools ?? []).map((tool: JsonObject) => String(tool.name)).filter(Boolean);
    const stream = Boolean(body.stream);
    this.record(path, body, headers, turn, stream, String(last), prompt);
    const blocks = this.blocks(script, model, turn, tools, String(last), prompt);
    const usage = script.usage ?? { input_tokens: 100, output_tokens: 50 };
    const output = blocks.map((block, index) => block.type === "text" ? { id: `msg_${turn}_${index}`, type: "message", status: "completed", role: "assistant", content: [{ type: "output_text", text: block.text, annotations: [] }] } : { id: `fc_${turn}_${index}`, type: "function_call", status: "completed", call_id: `call_${turn}_${index}`, name: block.name, arguments: JSON.stringify(block.input) });
    const response = { id: `resp_${turn}`, object: "response", status: "completed", model, output, usage: { ...usage, input_tokens_details: { cached_tokens: 0 }, output_tokens_details: { reasoning_tokens: 0 }, total_tokens: usage.input_tokens + usage.output_tokens } };
    if (!stream) return Response.json(response);
    const events = [`event: response.created\ndata: ${JSON.stringify({ type: "response.created", response: { ...response, status: "in_progress", output: [], usage: null } })}\n\n`];
    output.forEach((item, index) => {
      const pending: JsonObject = { ...item, status: "in_progress" };
      if (item.type === "message") pending.content = [];
      else pending.arguments = "";
      events.push(`event: response.output_item.added\ndata: ${JSON.stringify({ type: "response.output_item.added", output_index: index, item: pending })}\n\n`);
      if (item.type === "message") events.push(`event: response.output_text.delta\ndata: ${JSON.stringify({ type: "response.output_text.delta", item_id: item.id, output_index: index, content_index: 0, delta: item.content[0].text })}\n\n`);
      else events.push(`event: response.function_call_arguments.delta\ndata: ${JSON.stringify({ type: "response.function_call_arguments.delta", item_id: item.id, output_index: index, delta: item.arguments })}\n\n`);
      events.push(`event: response.output_item.done\ndata: ${JSON.stringify({ type: "response.output_item.done", output_index: index, item })}\n\n`);
    });
    events.push(`event: response.completed\ndata: ${JSON.stringify({ type: "response.completed", response })}\n\n`);
    return new Response(events.join(""), { headers: { "content-type": "text/event-stream" } });
  }
}

if (import.meta.main) {
  if (Bun.argv.length !== 4) {
    console.error("usage: fake_llm.ts SCRIPT PORT");
    process.exit(2);
  }
  const fake = new FakeLlm(Bun.argv[2]);
  const port = fake.start("127.0.0.1", Number(Bun.argv[3]));
  console.log(port);
  await new Promise(() => {});
}
