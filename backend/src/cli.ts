import { statSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { createApp, startServer, validateDag } from "./server";
import { SqliteStore } from "./db";
import { checkRegistry, type RegistryCheckResult } from "../mrl/factory";
import { Context } from "./legacy/context";
import { ConversionStore, conversionDocument } from "./legacy/conversion-requests";
import { FactoryError } from "./legacy/errors";
import { RandomIds } from "./legacy/foundation";
import { Warehouse } from "./legacy/warehouse";
import { businessPackageBlob, createBusinessPackage, publishBusinessDirectory, readBusinessPackage, registerVerifiedArtifactFiles } from "./business-package";
import { exportBusinessSystem, importBusinessSystem } from "./business-portability";
import { parseRepositoryRegistration } from "./project-registration";
import { projectCredentialName } from "./operator";
import { VaultStore } from "./vault";
import { CliUsageError, isStationScope, runStationCli, stationErrorExit, stationUsage } from "./station-cli";
import { selectedContainerProvider } from "../runtime/selected";
import { createStorageProviders, probeStorageStatus, storageStatus } from "./storage-factory";
import { ProviderError, migrateStorage, resolveStorageProviderConfig } from "./storage";
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
  if (scope === "project") return `factory project commands\n  register --repo URL [--name NAME] [--system ID]\n  intake --name NAME [--repo URL | --folder PATH | --file PATH | --file-ref REF]\n  fetch PROJECT SHA256 --output PATH\n  secret request PROJECT --name KEY --purpose PURPOSE`;
  if (scope === "business") return `factory business commands\n  units ID\n  bind ID UNIT --harness ID [--model ID]\n  run ID [--input REF]\n  runs ID\n  export ID [--package DEST]\n  verify PACKAGE\n  import PACKAGE --root NEW_ROOT\n  graph ID [--file PATH]\n  artifact PROJECT_ID --file PATH\n  conversion ID --kind KIND --summary TEXT [--input REF] [--output REF]`;
  if (scope === "foreman") return `factory foreman commands\n  inbox [--limit N]\n  claim ID\n  done ID\n  conversion ls [--status open|in_progress|resolved] [--root PATH]`;
  if (scope === "runtime") return `factory runtime commands\n  status\n  ensure-ready\n  doctor [--image DIGEST]`;
  if (scope === "storage") return `factory storage commands\n  status [--json]\n  verify [--json]\n  migrate [--dry-run] [--json] [--root PATH]`;
  if (scope === "registry") return "factory registry check PATH [--json] [--strict]";
  return `factory-bun commands\n  serve\n  status\n  runtime status\n  runtime ensure-ready\n  runtime doctor [--image REPOSITORY@sha256:DIGEST]\n  project register --repo URL [--name NAME] [--system ID]\n  project intake --name NAME [--repo URL | --folder PATH | --file-ref REF]\n  project secret request PROJECT --name KEY --purpose PURPOSE\n  business units ID\n  business bind ID UNIT --harness ID [--model ID]\n  business run ID [--input REF]\n  business runs ID\n  business export ID\n  business graph ID [--file PATH]\n  business artifact PROJECT_ID --file PATH\n  business conversion ID --kind KIND --summary TEXT [--input REF] [--output REF]\n  registry check PATH [--json] [--strict]\n  bean help\n  job help\n  foreman inbox [--limit N]\n  foreman claim ID\n  foreman done ID\n  talk send TEXT\n  talk reply TEXT --turn-id ID`;
}

function requireValue(args: string[], name: string): string {
  const result = value(args, name);
  if (!result) throw new CliUsageError(`${name} requires a value`);
  return result;
}

async function requestJson(app: ReturnType<typeof createApp>, path: string, init?: RequestInit): Promise<any> {
  const headers = new Headers(init?.headers);
  const turnId = process.env.MAO_FOREMAN_TURN_ID;
  if (turnId) headers.set("x-mao-foreman-turn", turnId);
  const result = await app.fetch(new Request(`http://localhost${path}`, { ...init, headers }));
  const body = await result.text();
  let parsed: unknown;
  try { parsed = body ? JSON.parse(body) : {}; } catch { parsed = { error: body }; }
  if (!result.ok) throw new Error(typeof parsed === "object" && parsed && "error" in parsed ? String((parsed as Record<string, unknown>).error) : `request failed with ${result.status}`);
  return parsed;
}

async function runProductionBusinessExport(args: string[]): Promise<number> {
  const id = args[1];
  if (!id) throw new CliUsageError("business system id is required");
  if (args.length !== 2 && (args.length !== 4 || args[2] !== "--package" || args[3].startsWith("--"))) {
    throw new CliUsageError("business export expects ID [--package DEST]");
  }
  const root = resolve(process.env.FACTORY_ROOT ?? ".factory");
  const storage = createStorageProviders({ root });
  const store = new SqliteStore(":memory:", ":memory:", { database: storage.state.database });
  try {
    const system = store.getBusinessSystem(id);
    if (!system) throw new Error("business system was not found");
    const document = JSON.parse(JSON.stringify(exportBusinessSystem(store, id, () => system.metadata.graph ?? { nodes: [], edges: [] })));
    if (!document) throw new Error("business system was not found");
    if (args.length === 4) {
      const blobProvider = storage.blob.kind === "local" ? undefined : storage.blob;
      console.log(JSON.stringify(await createBusinessPackage(args[3], document, root, [], blobProvider), null, 2));
    } else console.log(JSON.stringify(document, null, 2));
    return 0;
  } finally {
    store.close();
    storage.state.close();
  }
}

function runRegistryCli(args: string[]): number {
  const [operation, root, ...options] = args;
  if (args.length === 1 && (operation === "help" || operation === "--help")) {
    console.log(usage("registry"));
    return 0;
  }
  if (operation !== "check" || !root || root.startsWith("--")
    || new Set(options).size !== options.length || options.some((option) => option !== "--json" && option !== "--strict")) {
    throw new CliUsageError("registry expects check PATH [--json] [--strict]");
  }
  const { ok, stats, diagnostics }: Pick<RegistryCheckResult, "ok" | "stats" | "diagnostics"> =
    statSync(root, { throwIfNoEntry: false })?.isDirectory()
      ? checkRegistry(root)
      : { ok: false, stats: {}, diagnostics: [{ severity: "error", code: "E-LAYOUT", subject: root, message: "Registry path must be an existing directory" }] };
  const counts = { error: 0, warning: 0, info: 0 };
  for (const diagnostic of diagnostics) counts[diagnostic.severity]++;
  const passed = ok && (!options.includes("--strict") || counts.warning === 0);
  if (options.includes("--json")) {
    console.log(JSON.stringify({ root, ok, counts, stats, diagnostics }, null, 2));
  } else {
    for (const diagnostic of diagnostics) {
      const location = diagnostic.file ? `${diagnostic.file}:${diagnostic.line ?? 0}` : "(registry)";
      console.log(`${diagnostic.severity === "error" ? "✖" : "⚠"} ${diagnostic.code} ${location} ${diagnostic.subject}\n    ${diagnostic.message}`);
    }
    console.log(passed ? "✔ registry passed" : "✖ registry failed");
  }
  return passed ? 0 : 1;
}

async function runCommand(argv: string[]): Promise<number> {
  const [command, ...args] = argv;
  if (command === "storage") return runStorageCommand(args);
  if (command === "registry") return runRegistryCli(args);
  if (command === "foreman" && args[0] === "conversion") {
    return listConversions(args.slice(1));
  }
  if (command === "business" && args[0] === "verify") {
    if (args.length !== 2 || args[1].startsWith("--")) throw new CliUsageError("business verify expects a package directory");
    try {
      await readBusinessPackage(args[1]);
      console.log(JSON.stringify({ ok: true, problems: [], acceptance: "not_run" }, null, 2));
      return 0;
    } catch (error) {
      if (!(error instanceof FactoryError) || error.code !== "PACKAGE_CORRUPT") throw error;
      console.log(JSON.stringify({ ok: false, problems: [error.detail], acceptance: "not_run" }, null, 2));
      return 1;
    }
  }
  if (command === "business" && args[0] === "import") {
    if (args.length !== 4 || args[1].startsWith("--") || args[2] !== "--root" || args[3].startsWith("--")) {
      throw new CliUsageError("business import expects PACKAGE --root NEW_ROOT");
    }
    const result = await importBusinessPackage(args[1], args[3]);
    console.log(JSON.stringify(result, null, 2));
    return 0;
  }
  if (isStationScope(command) || ["--root", "--registry", "--provider", "--local-commands", "--epoch", "--json"].includes(command)) {
    return runStationCli(argv);
  }
  if (command === "business" && args[0] === "export" && process.env.MAO_STORAGE_MODE === "production") {
    return runProductionBusinessExport(args);
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
      if (args[0] === "doctor") {
        const image = value(args, "--image");
        if (image && (!/^\S+@sha256:[0-9a-f]{64}$/i.test(image) || args.filter((item) => item === "--image").length !== 1)) {
          throw new CliUsageError("runtime doctor --image requires one digest-pinned image");
        }
        const provider = selectedContainerProvider();
        const health = await provider.ensureReady();
        const result: Record<string, unknown> = { provider: provider.name, health };
        if (image) {
          try {
            const resolved = provider.ensureImage ? await provider.ensureImage(image) : await provider.resolveImage(image);
            result.image = { requested: image, resolved, state: "ready" };
          } catch (error) {
            result.image = { requested: image, state: "failed", reason: error instanceof Error ? error.message : String(error) };
          }
        }
        console.log(JSON.stringify(result, null, 2));
        return health.available && (!image || (result.image as { state: string }).state === "ready") ? 0 : 4;
      }
      throw new CliUsageError(`unknown runtime command: ${args[0] ?? "(missing)"}`);
    }
    if (command === "project") {
      if (args[0] === "secret") {
        const [, operation, projectId, ...flags] = args;
        const usage = "project secret request expects PROJECT --name KEY --purpose PURPOSE";
        if (operation !== "request" || !projectId || projectId.startsWith("--")) throw new CliUsageError(usage);
        const options = new Map<string, string>();
        for (let index = 0; index < flags.length; index += 2) {
          const [flag, argument] = [flags[index], flags[index + 1]];
          if ((flag !== "--name" && flag !== "--purpose") || !argument || argument.startsWith("--") || options.has(flag)) {
            throw new CliUsageError(usage);
          }
          options.set(flag, argument);
        }
        const name = options.get("--name");
        const purpose = options.get("--purpose")?.trim();
        if (!projectCredentialName(name) || !purpose || /[\u0000-\u001f\u007f]/.test(purpose)) {
          throw new CliUsageError("project secret request requires one valid credential name and a single-line purpose");
        }
        const { request_id, state } = await requestJson(app, "/api/v1/secrets/requests", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ project_id: projectId, name, purpose: redactText(purpose) }),
        });
        console.log(JSON.stringify({ request_id, state }, null, 2));
        return 0;
      }
      if (args[0] === "register") {
        const options = new Map<string, string>();
        for (let index = 1; index < args.length; index += 2) {
          const [flag, argument] = [args[index], args[index + 1]];
          if (!["--repo", "--name", "--system"].includes(flag) || !argument || argument.startsWith("--") || options.has(flag)) {
            throw new CliUsageError("project register expects --repo URL [--name NAME] [--system ID]");
          }
          options.set(flag, argument);
        }
        const input = parseRepositoryRegistration({ repoUrl: options.get("--repo"), name: options.get("--name"), systemId: options.get("--system") });
        if (!input) throw new CliUsageError("project register requires a repository URL without credentials");
        const result = app.registerRepository(input);
        if (!result) throw new Error("business system was not found");
        console.log(JSON.stringify(result, null, 2));
        return 0;
      }
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
        const document = await requestJson(app, route);
        if (subcommand === "export" && args.length > 2) {
          if (args.length !== 4 || args[2] !== "--package" || args[3].startsWith("--")) throw new CliUsageError("business export expects ID [--package DEST]");
          const root = resolve(process.env.FACTORY_ROOT ?? dirname(app.store.legacyPath));
          const secrets = app.store.listProjects(id).flatMap((project) => project.secretRequests.flatMap((request) => {
            const value = request.status === "available" ? app.vault.get(request.vaultRef) : undefined;
            return value ? [value] : [];
          }));
          const blobProvider = app.storage.blob.kind === "local" ? undefined : app.storage.blob;
          console.log(JSON.stringify(await createBusinessPackage(args[3], document, root, secrets, blobProvider), null, 2));
        } else console.log(JSON.stringify(document, null, 2));
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
        if (!id || id.startsWith("--")) throw new CliUsageError("target id is required");
        const route = subcommand === "graph" ? `/api/v1/business-systems/${encodeURIComponent(id)}/graph` : `/api/v1/projects/${encodeURIComponent(id)}/artifacts`;
        if (subcommand === "graph" && args.length === 2) {
          console.log(JSON.stringify(await requestJson(app, route), null, 2));
          return 0;
        }
        const file = requireValue(args, "--file");
        let document: unknown;
        try { document = JSON.parse(await Bun.file(file).text()); } catch (error) { throw new CliUsageError(`cannot read JSON file: ${error instanceof Error ? error.message : String(error)}`); }
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
        if (args[0] === "done" && result.status !== "done") throw new Error("厂长轮次已结束");
        console.log(JSON.stringify(result, null, 2));
        return 0;
      }
      throw new CliUsageError(`unknown foreman command: ${args[0] ?? "(missing)"}`);
    }
    if (command === "talk" && args[0] === "send") {
      const text = args.slice(1).join(" ").trim();
      if (!text) throw new CliUsageError("text is required");
      const { message } = await requestJson(app, "/api/v1/talk", {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text }),
      });
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

function storageJsonError(error: unknown): Record<string, unknown> {
  if (error instanceof ProviderError) return { ok: false, error: { code: error.code, message: error.message, details: error.details } };
  return { ok: false, error: { code: "PROVIDER_UNAVAILABLE", message: error instanceof Error ? error.message : String(error) } };
}

async function runStorageCommand(args: string[]): Promise<number> {
  const operation = args[0];
  const json = args.includes("--json");
  if (!operation || operation === "help" || operation === "--help") { console.log(usage("storage")); return 0; }
  if (!["status", "verify", "migrate"].includes(operation)) throw new CliUsageError(`unknown storage command: ${operation}`);
  const rootFlag = args.indexOf("--root");
  if (rootFlag >= 0 && (!args[rootFlag + 1] || args[rootFlag + 1].startsWith("--") || args.filter((item) => item === "--root").length !== 1)) {
    throw new CliUsageError("storage --root requires one path");
  }
  if (args.some((item, index) => ![operation, "--json", "--dry-run", "--root", rootFlag >= 0 ? args[rootFlag + 1] : ""].includes(item) && index !== rootFlag + 1)) {
    throw new CliUsageError("storage expects status, verify, or migrate [--dry-run] [--json] [--root PATH]");
  }
  const root = rootFlag >= 0 ? args[rootFlag + 1]! : process.env.FACTORY_ROOT ?? ".factory";
  const providerEnvironment = rootFlag >= 0
    ? { ...process.env, FACTORY_ROOT: root }
    : process.env;
  let storage;
  try { storage = createStorageProviders({ root, env: providerEnvironment }); }
  catch (error) { console.log(JSON.stringify(storageJsonError(error), null, 2)); return 4; }
  try {
    if (operation === "status") {
      const status = await probeStorageStatus(storage);
      const providers = [status.state, status.blob, status.secret, status.kms] as Array<{ available?: boolean }>;
      const ok = providers.every((item) => item.available === true);
      console.log(JSON.stringify({ ok, ...status }, null, 2));
      return ok ? 0 : 4;
    }
    if (operation === "verify") {
      const problems = await storage.blob.verify();
      const stateProblems = storage.state.verify();
      console.log(JSON.stringify({ ok: problems.length === 0 && stateProblems.length === 0, ...storageStatus(storage), problems, state_problems: stateProblems }, null, 2));
      return problems.length || stateProblems.length ? 1 : 0;
    }
    const config = resolveStorageProviderConfig(providerEnvironment);
    if (config.state === "sqlite" && config.blob === "local" && config.secret === "local") {
      console.log(JSON.stringify({ ok: true, migrated: false, switched: false, dry_run: args.includes("--dry-run"), mode: "local", reason: "local provider is already active" }, null, 2));
      return 0;
    }
    const report = await migrateStorage({ root, target: storage, dryRun: args.includes("--dry-run") });
    console.log(JSON.stringify({ ok: true, ...report }, null, 2));
    return 0;
  } finally { storage.state.close(); }
}

async function importBusinessPackage(source: string, destination: string) {
  const verified = await readBusinessPackage(source);
  if (process.env.MAO_STORAGE_MODE === "production") {
    const root = resolve(destination);
    const environment = { ...process.env, FACTORY_ROOT: root };
    const storage = createStorageProviders({ root, env: environment });
    const store = new SqliteStore(":memory:", ":memory:", { database: storage.state.database });
    try {
      for (const { digest, size } of verified.blobs) {
        const actual = await storage.blob.putFile(businessPackageBlob(verified.root, digest));
        if (actual !== digest) throw new FactoryError("PACKAGE_CORRUPT", "安装时内容摘要不匹配", { digest });
        store.db.query("INSERT OR IGNORE INTO intake_uploads(digest,size) VALUES (?,?)").run(digest, size);
      }
      const system = importBusinessSystem(store, verified.document as Record<string, unknown>, validateDag);
      registerVerifiedArtifactFiles(store, system.id, verified.blobs);
      return { root, system, acceptance: "not_run", storage: storage.config };
    } finally {
      store.close();
      storage.state.close();
    }
  }
  return publishBusinessDirectory(destination, async (stage) => {
    const database = join(stage, "factory.db");
    const vault = new VaultStore(join(stage, "vault.json"), join(stage, "vault.key"));
    const buyer = createApp({ storageRoot: stage, dbPath: database, legacyPath: database, vault, dispatchWork: false });
    try {
      const warehouse = new Warehouse(stage, buyer.store.db, buyer.store.clock, new RandomIds());
      for (const { digest, size } of verified.blobs) {
        const actual = await warehouse.putFile(businessPackageBlob(verified.root, digest));
        if (actual !== digest) throw new FactoryError("PACKAGE_CORRUPT", "安装时内容摘要不匹配", { digest });
        buyer.store.db.query("INSERT INTO intake_uploads(digest,size) VALUES (?,?)").run(digest, size);
      }
      const document = await requestJson(buyer, "/api/v1/business-systems/import", {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(verified.document),
      });
      registerVerifiedArtifactFiles(buyer.store, document.system.id, verified.blobs);
      return { root: resolve(destination), system: document.system, acceptance: "not_run" };
    } finally { buyer.stop(); }
  });
}

function listConversions(args: string[]): number {
  const [operation, ...flags] = args;
  if (operation !== "ls") throw new CliUsageError("foreman conversion expects ls");
  const options = new Map<string, string>();
  for (let index = 0; index < flags.length; index++) {
    const key = flags[index];
    if (key === "--json") continue;
    const argument = flags[++index];
    if ((key !== "--status" && key !== "--root") || !argument || argument.startsWith("--") || options.has(key)) {
      throw new CliUsageError("expected --status or --root with one value");
    }
    options.set(key, argument);
  }
  const status = options.get("--status");
  if (status !== undefined && status !== "open" && status !== "in_progress" && status !== "resolved") {
    throw new CliUsageError("expected status open, in_progress, or resolved");
  }
  const context = Context.open(options.get("--root") ?? process.env.FACTORY_ROOT ?? ".factory");
  try {
    console.log(JSON.stringify(new ConversionStore(context).list(status).map(conversionDocument), null, 2));
    return 0;
  } finally { context.close(); }
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
