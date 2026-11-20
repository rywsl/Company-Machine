import { basename } from "node:path";
import { createApp, startServer } from "./server";
import { checkRegistry } from "../mrl/factory";
import { CliUsageError, isStationScope, runStationCli, stationErrorExit, stationUsage } from "./station-cli";
export { CliUsageError } from "./station-cli";

const redactText = (text: string) => text.replace(/(api[_-]?key|secret|token|password|authorization|cookie)\s*[:=]\s*\S+/gi, (match) => `${match.split(/[:=]/, 1)[0]}=[REDACTED]`);

function value(args: string[], name: string): string | undefined {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : undefined;
}

function values(args: string[], name: string): string[] {
  return args.flatMap((item, index) => item === name && args[index + 1] ? [args[index + 1]] : []);
}

export function usage(scope?: string): string {
  if (scope && isStationScope(scope)) return stationUsage(scope);
  if (scope === "project") return `factory project commands\n  intake --name NAME [--repo URL | --folder PATH | --file PATH | --file-ref REF]\n  fetch PROJECT SHA256 --output PATH`;
  if (scope === "business") return `factory business commands\n  units ID\n  bind ID UNIT --harness ID [--model ID]\n  run ID [--input REF]\n  runs ID\n  export ID\n  graph ID --file PATH\n  artifact PROJECT_ID --file PATH\n  conversion ID --kind KIND --summary TEXT [--input REF] [--output REF]`;
  if (scope === "foreman") return `factory foreman commands\n  inbox [--limit N]\n  claim ID\n  done ID`;
  if (scope === "runtime") return `factory runtime commands\n  status\n  ensure-ready`;
  return `factory-bun commands\n  serve\n  status\n  runtime status\n  runtime ensure-ready\n  project intake --name NAME [--repo URL | --folder PATH | --file-ref REF]\n  business units ID\n  business bind ID UNIT --harness ID [--model ID]\n  business run ID [--input REF]\n  business runs ID\n  business export ID\n  business graph ID --file PATH\n  business artifact PROJECT_ID --file PATH\n  business conversion ID --kind KIND --summary TEXT [--input REF] [--output REF]\n  registry check PATH [--json]\n  bean help\n  job help\n  foreman inbox [--limit N]\n  foreman claim ID\n  foreman done ID\n  talk send TEXT\n  talk reply TEXT --turn-id ID`;
}

function requireValue(args: string[], name: string): string {
  const result = value(args, name);
  if (!result) throw new CliUsageError(`${name} requires a value`);
  return result;
}

async function requestJson(app: ReturnType<typeof createApp>, path: string, init?: RequestInit): Promise<any> {
  const result = await app.fetch(new Request(`http://localhost${path}`, init));
  const body = await result.text();
  let parsed: unknown;
  try { parsed = body ? JSON.parse(body) : {}; } catch { parsed = { error: body }; }
  if (!result.ok) throw new Error(typeof parsed === "object" && parsed && "error" in parsed ? String((parsed as Record<string, unknown>).error) : `request failed with ${result.status}`);
  return parsed;
}

async function runCommand(argv: string[]): Promise<number> {
  const [command, ...args] = argv;
  if (isStationScope(command) || ["--root", "--registry", "--provider", "--local-commands", "--epoch", "--json"].includes(command)) {
    return runStationCli(argv);
  }
  if (!command || command === "help" || command === "--help") {
    console.log(usage());
    return 0;
  }
  if (["project", "business", "foreman", "runtime"].includes(command) && (args[0] === "help" || args[0] === "--help")) {
    console.log(usage(command));
    return 0;
  }
  if (command === "serve") {
    const started = startServer({ port: Number(process.env.PORT ?? 3000) });
    console.log(`Bun control plane listening on ${started.server.url}`);
    return 0;
  }

  const app = createApp({ dispatchWork: false });
  try {
    if (command === "status") {
      console.log(JSON.stringify({ ok: true, businessSystems: app.store.listBusinessSystems().length, foremanInbox: app.store.listForemanInbox().length }, null, 2));
      return 0;
    }
    if (command === "runtime") {
      if (args[0] === "status") {
        console.log(JSON.stringify(await requestJson(app, "/api/v1/runtime"), null, 2));
        return 0;
      }
      if (args[0] === "ensure-ready") {
        const result = await app.fetch(new Request("http://localhost/api/v1/runtime/ensure-ready", { method: "POST", headers: { "content-type": "application/json" } }));
        const body = await result.json();
        console.log(JSON.stringify(body, null, 2));
        return result.ok ? 0 : 4;
      }
      throw new CliUsageError(`unknown runtime command: ${args[0] ?? "(missing)"}`);
    }
    if (command === "project") {
      if (args[0] === "fetch") {
        const [, projectId, digest] = args;
        if (!projectId || !/^sha256:[0-9a-f]{64}$/.test(digest ?? "")) throw new CliUsageError("project id and sha256 digest are required");
        const output = requireValue(args, "--output");
        const result = await app.fetch(new Request(`http://localhost/api/v1/projects/${encodeURIComponent(projectId)}/files/${digest}`));
        if (!result.ok) throw new Error("project upload was not found");
        await Bun.write(output, result);
        console.log(output);
        return 0;
      }
      if (args[0] !== "intake") throw new CliUsageError(`unknown project command: ${args[0] ?? "(missing)"}`);
      const repo = value(args, "--repo");
      const folder = value(args, "--folder");
      const fileRef = value(args, "--file-ref");
      const files = values(args, "--file");
      if (!repo && !folder && !fileRef && !files.length) throw new CliUsageError("one source is required");
      const name = value(args, "--name") ?? "新项目";
      let request: RequestInit = { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name, repo_url: repo, local_folder: folder, uploaded_file_refs: fileRef ? [fileRef] : [] }) };
      if (files.length) {
        const form = new FormData();
        form.set("name", name);
        if (repo) form.set("repo_url", repo);
        if (folder) form.set("local_folder", folder);
        if (fileRef) form.append("uploaded_file_refs", fileRef);
        for (const file of files) form.append("files", Bun.file(file), basename(file));
        request = { method: "POST", body: form };
      }
      const body = await requestJson(app, "/api/v1/projects/intake", request);
      console.log(JSON.stringify(body, null, 2));
      return 0;
    }
    if (command === "business") {
      const subcommand = args[0];
      if (subcommand === "bind") {
        const [, systemId, unitId, ...flags] = args;
        if (!systemId || !unitId || systemId.startsWith("--") || unitId.startsWith("--")) throw new CliUsageError("business system and Operator ids are required");
        const options = new Map<string, string>();
        for (let index = 0; index < flags.length; index += 2) {
          const [key, value] = [flags[index], flags[index + 1]];
          if ((key !== "--harness" && key !== "--model") || !value || value.startsWith("--") || options.has(key)) {
            throw new CliUsageError("business bind expects --harness ID [--model ID]");
          }
          options.set(key, value);
        }
        const harness = options.get("--harness");
        if (!harness) throw new CliUsageError("--harness requires a value");
        const route = `/api/v1/business-systems/${encodeURIComponent(systemId)}/units/${encodeURIComponent(unitId)}/binding`;
        const binding = await requestJson(app, route, {
          method: "PATCH", headers: { "content-type": "application/json" },
          body: JSON.stringify({ harness_runtime: harness, model: options.get("--model") ?? null }),
        });
        console.log(JSON.stringify(binding, null, 2));
        return 0;
      }
      if (["units", "runs", "export"].includes(subcommand ?? "")) {
        const id = args[1];
        if (!id) throw new CliUsageError("business system id is required");
        const route = subcommand === "units" ? `/api/v1/business-systems/${encodeURIComponent(id)}/units` : subcommand === "runs" ? `/api/v1/business-systems/${encodeURIComponent(id)}/runs` : `/api/v1/business-systems/${encodeURIComponent(id)}/export`;
        console.log(JSON.stringify(await requestJson(app, route), null, 2));
        return 0;
      }
      if (subcommand === "run") {
        const id = args[1];
        if (!id) throw new CliUsageError("business system id is required");
        console.log(JSON.stringify(await requestJson(app, `/api/v1/business-systems/${encodeURIComponent(id)}/runs`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ inputs: values(args, "--input") }) }), null, 2));
        return 0;
      }
      if (subcommand === "graph" || subcommand === "artifact") {
        const id = args[1];
        const file = requireValue(args, "--file");
        if (!id) throw new CliUsageError("target id is required");
        let document: unknown;
        try { document = JSON.parse(await Bun.file(file).text()); } catch (error) { throw new CliUsageError(`cannot read JSON file: ${error instanceof Error ? error.message : String(error)}`); }
        const route = subcommand === "graph" ? `/api/v1/business-systems/${encodeURIComponent(id)}/graph` : `/api/v1/projects/${encodeURIComponent(id)}/artifacts`;
        const body = subcommand === "graph" && document && typeof document === "object" && "graph" in document ? (document as Record<string, unknown>).graph : document;
        console.log(JSON.stringify(await requestJson(app, route, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(subcommand === "graph" ? { graph: body, by: "official-harness" } : body) }), null, 2));
        return 0;
      }
      if (subcommand === "conversion") {
        const id = args[1];
        if (!id) throw new CliUsageError("business system id is required");
        const kind = requireValue(args, "--kind");
        const summary = requireValue(args, "--summary");
        console.log(JSON.stringify(await requestJson(app, `/api/v1/business-systems/${encodeURIComponent(id)}/conversions`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ kind, summary, inputs: values(args, "--input"), outputs: values(args, "--output") }) }), null, 2));
        return 0;
      }
      throw new CliUsageError(`unknown business command: ${subcommand ?? "(missing)"}`);
    }
    if (command === "registry" && args[0] === "check") {
      const root = args[1];
      if (!root) throw new CliUsageError("registry path is required");
      const result = checkRegistry(root);
      if (args.includes("--json")) console.log(JSON.stringify({ root: result.root, ok: result.ok, stats: result.stats, diagnostics: result.diagnostics }, null, 2));
      else {
        for (const diagnostic of result.diagnostics) {
          const location = diagnostic.file ? `${diagnostic.file}:${diagnostic.line ?? 0}` : "(registry)";
          console.log(`${diagnostic.severity === "error" ? "✖" : "⚠"} ${diagnostic.code} ${location} ${diagnostic.subject}\n    ${diagnostic.message}`);
        }
        console.log(result.ok ? "✔ registry passed" : "✖ registry failed");
      }
      return result.ok ? 0 : 1;
    }
    if (command === "foreman") {
      if (args[0] === "inbox") {
        console.log(JSON.stringify(app.store.listForemanInbox(Number(value(args, "--limit") ?? 50)), null, 2));
        return 0;
      }
      if (args[0] === "claim" || args[0] === "done") {
        const id = args[1];
        if (!id) throw new CliUsageError("message id is required");
        const result = args[0] === "claim" ? app.store.claimForemanMessage(id) : app.store.completeForemanMessage(id);
        if (!result) throw new Error("message not found");
        console.log(JSON.stringify(result, null, 2));
        return 0;
      }
      throw new CliUsageError(`unknown foreman command: ${args[0] ?? "(missing)"}`);
    }
    if (command === "talk" && args[0] === "send") {
      const text = args.slice(1).join(" ").trim();
      if (!text) throw new CliUsageError("text is required");
      const message = app.store.addTalkMessage({ body: redactText(text) });
      app.store.bridgeHumanTalk(message.id, message.body);
      const envelope = app.store.queueForemanMessage(message.id, message.body);
      app.events.publish("talk.message.created", message);
      app.events.publish("foreman.message.queued", envelope);
      console.log(JSON.stringify(message, null, 2));
      return 0;
    }
    if (command === "talk" && args[0] === "reply") {
      const turnId = requireValue(args, "--turn-id");
      const text = args.filter((item) => item !== "--turn-id" && item !== turnId).join(" ").trim();
      if (!text) throw new CliUsageError("text is required");
      const message = app.store.addTalkMessage({ role: "foreman", body: redactText(text), metadata: { turn_id: turnId } });
      app.events.publish("talk.foreman.replied", message);
      console.log(JSON.stringify(message, null, 2));
      return 0;
    }
    throw new CliUsageError(`unknown factory command: ${command}`);
  } finally {
    app.stop();
  }
}

export async function main(argv = Bun.argv.slice(2)): Promise<number> {
  try {
    return await runCommand(argv);
  } catch (error) {
    const code = error instanceof CliUsageError ? 2 : stationErrorExit(error);
    console.error(error instanceof Error ? error.message : String(error));
    if (error instanceof CliUsageError) console.error(usage(argv[0]));
    return code;
  }
}

if (import.meta.main) {
  const code = await main();
  if (Bun.argv[2] !== "serve" || code !== 0) process.exit(code);
}
