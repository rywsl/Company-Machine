import { join } from "node:path";
import { selectedContainerProvider } from "../runtime/selected";
import { Context, FactoryError, Gate, JobStore, LineRunner, LocalProvider, RunStore, Runner, canonicalJson, dumpJob, loadSnapshot, runTerminalStates, type RunState } from "./legacy";
import { compileDynamicRunPlan } from "./legacy/dynamic-switch";
import { PodmanError, type ContainerProvider } from "../runtime/podman";
import { Approvals, type ApprovalStatus } from "./legacy/approvals";
import { ContractRunner, type ContractKind, type ContractResult } from "./legacy/contract";
import { ReceiptStore } from "./legacy/connectors";
import { Scheduler } from "./legacy/scheduler";
import { connectorCommands, runConnectorCli } from "./connector-cli";
import { changesetCommands, runChangeSetCli, runGovernanceCli } from "./changeset-cli";
import { epochCommands, runEpochCli } from "./epoch-cli";
import { readGovernedRegistry } from "./legacy/registry-config";
import { RegistrySelection } from "./registry-selection";
import { createBackup, restoreBackup, verifyBackup } from "./backup";

export class CliUsageError extends Error {}

type Options = {
  root?: string;
  registry?: string;
  provider?: string;
  localCommands?: string;
  epoch?: string;
  json: boolean;
};

const commands = {
  ...connectorCommands,
  ...changesetCommands,
  ...epochCommands,
  bean: ["import --type TYPE --file PATH=HOST", "lineage BEAN --direction up|down"],
  job: ["run STATION [--input PORT=BEAN] [--param NAME=JSON]", "show JOB", "exec JOB"],
  line: ["plan LINE", "run LINE [--in PORT=BEAN] [--budget USD]", "exec RUN", "ls [--state STATE]", "show RUN", "nodes RUN", "cancel RUN", "retry RUN NODE", "rebuild RUN"],
  approval: ["ls [--job JOB] [--status STATUS]", "show APPROVAL", "approve APPROVAL [--note TEXT]", "deny APPROVAL [--note TEXT]"],
  schedule: ["tick [--config PATH]", "enqueue JOB...", "dlq ls", "quarantine BEAN --reason TEXT", "release BEAN"],
  contract: [
    "validator TYPE VALIDATOR --timeout SECONDS", "golden GOLDEN [--baseline BASELINE] --timeout SECONDS",
    "equivalence STATION [--timeout SECONDS]", "diff STATION [--timeout SECONDS]",
    "sink-dry-run STATION [--in PORT=BEAN] [--param NAME=JSON] [--timeout SECONDS]",
    "canary STATION [--timeout SECONDS]", "ls [--kind KIND] [--subject SUBJECT]", "show RESULT",
  ],
  backup: ["create DEST [--root ROOT] [--registry DIR] [--with-logs]", "verify DEST", "restore DEST --root NEW_ROOT"],
};

export function isStationScope(scope: string): scope is keyof typeof commands {
  return Object.hasOwn(commands, scope);
}

export function stationUsage(scope: keyof typeof commands): string {
  return `factory ${scope} commands\n  ${commands[scope].join("\n  ")}`;
}

function parse(argv: string[]): { options: Options; args: string[] } {
  const flags = { "--root": "root", "--registry": "registry", "--provider": "provider", "--local-commands": "localCommands", "--epoch": "epoch" } as const;
  const options: Options = { json: false };
  const args: string[] = [];
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--json") {
      options.json = true;
    } else if (Object.hasOwn(flags, token)) {
      const value = argv[++index];
      if (!value || value.startsWith("--")) throw new CliUsageError(`${token} requires a value`);
      options[flags[token as keyof typeof flags]] = value;
    } else {
      args.push(token);
    }
  }
  return { options, args };
}

function readOptions(argv: string[], allowed: string[]): Map<string, string[]> {
  const options = new Map<string, string[]>();
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!allowed.includes(key)) throw new CliUsageError(`unknown option: ${key}`);
    if (!value || value.startsWith("--")) throw new CliUsageError(`${key} requires a value`);
    options.set(key, [...(options.get(key) ?? []), value]);
  }
  return options;
}

function required(options: Map<string, string[]>, key: string): string {
  const values = options.get(key);
  if (values?.length !== 1) throw new CliUsageError(`${key} requires exactly one value`);
  return values[0];
}

function pair(value: string, option: string): [string, string] {
  const index = value.indexOf("=");
  if (index < 1 || index === value.length - 1) throw new CliUsageError(`${option} requires NAME=VALUE`);
  return [value.slice(0, index), value.slice(index + 1)];
}

function materialInputs(arguments_: string[], flag: string, isList: (port: string) => boolean): Record<string, string | string[]> {
  const inputs: Record<string, string | string[]> = Object.create(null);
  for (const argument of arguments_) {
    const [port, bean] = pair(argument, flag);
    if (isList(port)) {
      const items = (inputs[port] ?? []) as string[];
      items.push(bean);
      inputs[port] = items;
    } else {
      if (Object.hasOwn(inputs, port)) throw new CliUsageError(`duplicate input: ${port}`);
      inputs[port] = bean;
    }
  }
  return inputs;
}

function parameters(arguments_: string[]): Record<string, unknown> {
  return Object.fromEntries(arguments_.map((argument) => {
    const [key, value] = pair(argument, "--param");
    try { return [key, JSON.parse(value)]; }
    catch { throw new CliUsageError(`--param requires valid JSON for ${key}`); }
  }));
}

async function provider(options: Options): Promise<ContainerProvider> {
  if (options.provider && !["local", "kiri", "podman"].includes(options.provider)) {
    throw new CliUsageError("--provider must be podman, kiri, or local");
  }
  if (options.provider !== "local") {
    if (options.localCommands) throw new CliUsageError("--local-commands requires --provider local");
    return selectedContainerProvider(options.provider);
  }
  if (!options.localCommands) return new LocalProvider();
  let data: unknown;
  try { data = JSON.parse(await Bun.file(options.localCommands).text()); }
  catch { throw new CliUsageError("--local-commands requires a readable JSON file"); }
  if (!data || typeof data !== "object" || Array.isArray(data) || Object.values(data).some(
    (entry) => !Array.isArray(entry) || entry.length === 0 || entry.some((item) => typeof item !== "string"),
  )) throw new CliUsageError("--local-commands must map names to argv arrays");
  return new LocalProvider(Object.assign(Object.create(null), data));
}

export async function runStationCli(argv: string[]): Promise<number> {
  const { options, args: [scope, operation, ...args] } = parse(argv);
  if (!isStationScope(scope)) {
    throw new CliUsageError("Unknown factory command");
  }
  if (!operation || operation === "help" || operation === "--help") {
    console.log(stationUsage(scope));
    return 0;
  }
  const root = options.root ?? process.env.FACTORY_ROOT ?? ".factory";
  const registry = options.registry ?? process.env.FACTORY_REGISTRY ?? "registry";
  if (scope === "backup") return await runBackupCli(operation, args, root, options);
  const isPlanning = scope === "line" && operation === "plan";
  if (isPlanning && (args.length !== 1 || args[0].startsWith("--"))) throw new CliUsageError("Line id is required");
  if (isPlanning && !options.epoch && !readGovernedRegistry(root)) return printPlan(await loadSnapshot(registry), args[0]);
  const context = Context.open(root);
  const snapshots = new RegistrySelection(context, registry, options.epoch, options.registry ?? process.env.FACTORY_REGISTRY);
  try {
    if (isPlanning) return printPlan(await snapshots.current(), args[0]);
    if (scope === "epoch") return await runEpochCli(context, operation, args, { registry: options.registry ?? process.env.FACTORY_REGISTRY });
    if (scope === "changeset") return await runChangeSetCli(context, operation, args, {
      registry: options.registry ?? process.env.FACTORY_REGISTRY, provider: () => provider(options),
    });
    if (scope === "governance") return await runGovernanceCli(context, operation, args, {
      registry: options.registry ?? process.env.FACTORY_REGISTRY, provider: () => provider(options),
    });
    if (scope === "source" || scope === "receipt") return await runConnectorCli(context, {
      scope, operation, args, snapshots, json: options.json, provider: () => provider(options),
    });
    if (scope === "approval") return await runApprovalCli(context, snapshots, operation, args);
    if (scope === "schedule") return await runScheduleCli(context, snapshots, operation, args, options);
    if (scope === "line") return await runLineCli(context, snapshots, operation, args, options);
    if (scope === "contract") return await runContractCli(context, snapshots, operation, args, options);
    if (scope === "bean" && operation === "lineage") {
      const [id, ...rest] = args;
      if (!id || id.startsWith("--")) throw new CliUsageError("Bean id is required");
      const direction = rest.length === 1 && rest[0] === "--down" ? "down" : required(readOptions(rest, ["--direction"]), "--direction");
      if (direction !== "up" && direction !== "down") throw new CliUsageError("--direction must be up or down");
      const lineage = context.warehouse.lineage(id, direction);
      console.log(canonicalJson(direction === "up" ? lineage : { ...lineage, receipts: new ReceiptStore(context).forBean(id).map(({ id }) => id) }));
      return 0;
    }
    if (scope === "job" && operation === "show") {
      if (args.length !== 1 || args[0].startsWith("--")) throw new CliUsageError("Job id is required");
      console.log(canonicalJson(dumpJob(new JobStore(context.db, context.clock, context.ids).get(args[0]))));
      return 0;
    }
    if (scope === "bean" && operation === "import") {
      const fields = readOptions(args, ["--type", "--file"]);
      const type = required(fields, "--type");
      const paths = fields.get("--file");
      if (!paths?.length) throw new CliUsageError("--file is required");
      const files = Object.fromEntries(paths.map((value) => pair(value, "--file")));
      const snapshot = await snapshots.current();
      const runtime = await provider(options);
      const gate = new Gate(snapshot, context.warehouse, join(context.root, "tmp", "gate"), runtime);
      const { report, bean } = await gate.admitExternal(type, files, {});
      if (!bean) {
        console.error(report.data.feedback || report.data.verdict);
        return report.data.verdict === "error" ? 4 : 1;
      }
      context.warehouse.sealMany([[bean.id, report.data.facts[bean.id] ?? {}]], null, report.digest());
      console.log(options.json ? canonicalJson({ id: bean.id }) : bean.id);
      return 0;
    }
    if (scope !== "job" || (operation !== "run" && operation !== "exec")) {
      throw new CliUsageError(`unknown ${scope} command: ${operation}`);
    }
    const [target, ...rest] = args;
    if (!target || target.startsWith("--")) throw new CliUsageError(operation === "run" ? "Station is required" : "Job id is required");
    const fields = readOptions(rest, operation === "run" ? ["--input", "--param"] : []);
    const snapshot = operation === "run"
      ? await snapshots.current()
      : await snapshots.saved(new JobStore(context.db, context.clock, context.ids).get(target).epoch);
    const runner = new Runner(context, snapshot, await provider(options));
    let id = target;
    if (operation === "run") {
      const station = snapshot.spec(snapshot.key("Station", target));
      const inputs = materialInputs(fields.get("--input") ?? [], "--input", (port) => station.inputs?.[port]?.type?.startsWith("list<") ?? false);
      const params = parameters(fields.get("--param") ?? []);
      id = runner.submit(target, inputs, params).id;
    }
    const job = await runner.run(id);
    if (options.json) console.log(canonicalJson(dumpJob(job)));
    else if (job.state === "AWAITING_APPROVAL") console.log(`等待审批 ${job.id}`);
    else if (job.state === "SEALED") {
      for (const port of Object.keys(job.outputs).sort()) {
        for (const bean of job.outputs[port]) console.log(`${port}\t${bean}`);
      }
    } else console.error(`${job.error?.code ?? job.state}: ${job.error?.message ?? job.feedback ?? "工位执行失败"}`);
    return job.state === "SEALED" || job.state === "AWAITING_APPROVAL" ? 0 : 1;
  } finally {
    context.close();
  }
}

function printPlan(snapshot: Awaited<ReturnType<typeof loadSnapshot>>, line: string): number {
  console.log(canonicalJson(compileDynamicRunPlan(snapshot, line).toJSON()));
  return 0;
}

async function runContractCli(context: Context, snapshots: RegistrySelection, operation: string, args: string[], options: Options): Promise<number> {
  if (!["validator", "golden", "equivalence", "diff", "sink-dry-run", "canary", "ls", "show"].includes(operation)) {
    throw new CliUsageError(`unknown contract command: ${operation}`);
  }
  const snapshot = await snapshots.current();
  const contract = new ContractRunner(new Runner(context, snapshot, await provider(options)));
  if (operation === "ls") {
    const fields = readOptions(args, ["--kind", "--subject"]);
    const kind = fields.has("--kind") ? required(fields, "--kind") : undefined;
    if (kind && !["validator", "golden", "equivalence", "diff", "sink_dry_run", "fixture_set"].includes(kind)) {
      throw new CliUsageError("Invalid contract result kind");
    }
    const subject = fields.has("--subject") ? required(fields, "--subject") : undefined;
    console.log(canonicalJson(contract.list({ kind: kind as ContractKind | undefined, subject })));
    return 0;
  }
  const [target, ...rest] = args;
  if (!target || target.startsWith("--")) throw new CliUsageError("Contract subject is required");
  if (operation === "show") {
    readOptions(rest, []);
    console.log(canonicalJson(contract.get(target)));
    return 0;
  }
  const validator = operation === "validator" ? rest.shift() : undefined;
  if (operation === "validator" && (!validator || validator.startsWith("--"))) throw new CliUsageError("Validator name is required");
  const extra = operation === "golden" ? ["--baseline"] : operation === "sink-dry-run" ? ["--in", "--param"] : [];
  const fields = readOptions(rest, ["--timeout", ...extra]);
  const requiresTimeout = operation === "validator" || operation === "golden";
  const timeoutS = requiresTimeout || fields.has("--timeout") ? Number(required(fields, "--timeout")) : undefined;
  if (timeoutS !== undefined && (!Number.isFinite(timeoutS) || timeoutS <= 0)) throw new CliUsageError("--timeout requires positive seconds");
  const timeout = { timeoutS };
  let result: ContractResult;
  switch (operation) {
    case "validator":
      result = await contract.runValidator(target, validator!, timeout);
      break;
    case "golden":
      result = await contract.runGolden(target, fields.has("--baseline") ? required(fields, "--baseline") : undefined, timeout);
      break;
    case "equivalence":
      result = await contract.runEquivalence(target, timeout);
      break;
    case "diff":
      result = await contract.runDiff(target, timeout);
      break;
    case "canary":
      result = await contract.canary(target, timeout);
      break;
    default: {
      const key = snapshot.key("Station", target);
      const station = snapshot.docs[key]?.spec;
      const inputs = materialInputs(fields.get("--in") ?? [], "--in", (port) => station?.inputs?.[port]?.type?.startsWith("list<") ?? false);
      result = await contract.runSinkDryRun(target, { ...timeout, inputs, params: parameters(fields.get("--param") ?? []) });
    }
  }
  console.log(canonicalJson(result));
  return result.verdict === "pass" ? 0 : 1;
}

async function runApprovalCli(context: Context, snapshots: RegistrySelection, operation: string, args: string[]): Promise<number> {
  const approvals = new Approvals(context);
  if (operation === "ls") {
    const options = readOptions(args, ["--job", "--status"]);
    const status = options.has("--status") ? required(options, "--status") : undefined;
    const jobId = options.has("--job") ? required(options, "--job") : undefined;
    if (status !== undefined && !["pending", "approved", "denied"].includes(status)) throw new CliUsageError("Invalid approval status");
    console.log(canonicalJson(approvals.list({ jobId, status: status as ApprovalStatus | undefined })));
    return 0;
  }
  const [id, ...rest] = args;
  if (!id || id.startsWith("--")) throw new CliUsageError("Approval id is required");
  if (operation === "show" && !rest.length) console.log(canonicalJson(approvals.get(id)));
  else if (operation === "approve" || operation === "deny") {
    const options = readOptions(rest, ["--note"]);
    const note = options.has("--note") ? required(options, "--note") : undefined;
    const jobs = new JobStore(context.db, context.clock, context.ids);
    const job = jobs.get(approvals.get(id).jobId);
    const snapshot = await snapshots.saved(job.epoch);
    console.log(canonicalJson(new Approvals(context, snapshot, jobs).applyDecision(id, operation, { by: process.env.FACTORY_HUMAN_BY ?? "human", note })));
  } else throw new CliUsageError(`unknown approval command: ${operation}`);
  return 0;
}

async function runBackupCli(operation: string, args: string[], root: string, options: Options): Promise<number> {
  const [destination, ...rest] = args;
  if (!destination || destination.startsWith("--")) throw new CliUsageError("Backup directory is required");
  if (operation === "create" && (rest.length === 0 || (rest.length === 1 && rest[0] === "--with-logs"))) {
    const businessDatabase = process.env.BUSINESS_DB_PATH ?? process.env.FACTORY_DB_PATH;
    const result = await createBackup({ root, businessDatabase, registry: options.registry, withLogs: rest.length === 1, by: process.env.FACTORY_HUMAN_BY }, destination);
    console.log(canonicalJson(result));
    return 0;
  }
  if (operation === "verify" && rest.length === 0) {
    const result = await verifyBackup(destination);
    console.log(canonicalJson(result));
    return result.ok ? 0 : 1;
  }
  const restoreRoot = options.root === undefined && rest.length === 1 && !rest[0].startsWith("--") ? rest[0] : undefined;
  if (operation === "restore" && ((rest.length === 0 && options.root !== undefined) || restoreRoot)) {
    console.log(canonicalJson(await restoreBackup(destination, restoreRoot ?? root)));
    return 0;
  }
  throw new CliUsageError("Use backup create, backup verify, or backup restore DEST --root NEW_ROOT");
}

async function runScheduleCli(context: Context, snapshots: RegistrySelection, operation: string, args: string[], options: Options): Promise<number> {
  const snapshot = await snapshots.current();
  const runtime = await provider(options);
  const runner = new Runner(context, snapshot, runtime);
  const scheduler = new Scheduler(context, snapshot, runner, {
    resolveRunner: async (epoch) => new Runner(context, await snapshots.saved(epoch), runtime),
  });
  const by = process.env.FACTORY_HUMAN_BY ?? "human";
  if (operation === "tick") {
    const fields = readOptions(args, ["--config"]);
    scheduler.loadConfig(fields.has("--config") ? required(fields, "--config") : undefined);
    console.log(canonicalJson(await scheduler.tick()));
  } else if (operation === "enqueue" && args.length && args.every((arg) => !arg.startsWith("--"))) {
    scheduler.enqueue(args);
    console.log(canonicalJson(scheduler.queued()));
  } else if (operation === "dlq" && args.length === 1 && args[0] === "ls") {
    console.log(canonicalJson(scheduler.listDead()));
  } else if (operation === "quarantine" || operation === "release") {
    const [id, ...rest] = args;
    if (!id || id.startsWith("--")) throw new CliUsageError("Bean id is required");
    if (operation === "quarantine") {
      scheduler.quarantine(id, { by, reason: required(readOptions(rest, ["--reason"]), "--reason") });
    } else {
      readOptions(rest, []);
      scheduler.release(id, { by });
    }
    if (options.json) console.log(canonicalJson({ id, quarantined: scheduler.isQuarantined(id) }));
  } else throw new CliUsageError(`unknown schedule command or invalid arguments: ${operation}`);
  return 0;
}

async function runLineCli(context: Context, snapshots: RegistrySelection, operation: string, args: string[], options: Options): Promise<number> {
  const store = new RunStore(context.db, context.clock, context.ids);
  const [target, ...rest] = args;
  const print = (value: unknown) => console.log(canonicalJson(value));
  if (operation === "ls") {
    const values = readOptions(args, ["--state"]);
    const state = values.has("--state") ? required(values, "--state") : undefined;
    if (state !== undefined && !["PENDING", "RUNNING", "FAILING", "CANCELLING", "SUCCEEDED", "FAILED", "CANCELLED"].includes(state)) {
      throw new CliUsageError("Invalid run state");
    }
    print(store.list(state as RunState | undefined));
    return 0;
  }
  if (!target || target.startsWith("--")) throw new CliUsageError("Run or Line id is required");
  const by = process.env.FACTORY_HUMAN_BY ?? "human";
  if (operation === "retry") {
    if (rest.length !== 1 || rest[0].startsWith("--")) throw new CliUsageError("Node id is required");
    print(store.retryNode(target, rest[0], by));
    return 0;
  }
  if (["show", "nodes", "cancel"].includes(operation)) {
    if (rest.length) throw new CliUsageError(`Unexpected argument: ${rest[0]}`);
    print(operation === "show" ? store.get(target) : operation === "nodes" ? store.nodes(target) : store.cancel(target, by));
    return 0;
  }
  if (!["run", "exec", "rebuild"].includes(operation)) throw new CliUsageError(`unknown line command: ${operation}`);
  const noCache = operation === "run" && rest.includes("--no-cache");
  const fields = readOptions(rest.filter((word) => !noCache || word !== "--no-cache"), operation === "run" ? ["--in", "--budget", "--variant"] : []);
  const lineInputs = (snapshot: Awaited<ReturnType<RegistrySelection["current"]>>) => {
    const spec = snapshot.spec(snapshot.key("Line", target));
    return materialInputs(fields.get("--in") ?? [], "--in", (port) => spec.interface?.inputs?.[port]?.type.startsWith("list<") ?? false);
  };
  const snapshot = operation === "exec"
    ? await snapshots.saved(store.get(target).epoch)
    : operation === "run" ? await snapshots.forLine(target, lineInputs) : await snapshots.current();
  const lines = new LineRunner(new Runner(context, snapshot, await provider(options)));
  if (operation === "rebuild") {
    const previous = store.get(target);
    if (!runTerminalStates.has(previous.state)) throw new FactoryError("RUN_STATE_INVALID", "只可重建已结束的运行", { run: target, state: previous.state });
    print(lines.start(previous.line, {
      inputs: previous.inputs, variants: previous.variants, budgetCapUsd: previous.budgetCapUsd || undefined,
      rebuiltFrom: previous.id, sinkMode: previous.sinkMode, trigger: { on: "manual", by },
    }));
    return 0;
  }
  let id = target;
  if (operation === "run") {
    id = lines.start(target, {
      inputs: lineInputs(snapshot), budgetCapUsd: fields.has("--budget") ? Number(required(fields, "--budget")) : undefined,
      variants: Object.fromEntries((fields.get("--variant") ?? []).map((value) => pair(value, "--variant"))),
      useCache: !noCache,
      trigger: { on: "manual", by },
    }).id;
  }
  const run = await lines.run(id);
  print(run);
  return run.state === "SUCCEEDED" ? 0 : 1;
}

export function stationErrorExit(error: unknown): number {
  if (!(error instanceof FactoryError || error instanceof PodmanError)) return 1;
  if (error.code === "CLI_USAGE") return 2;
  if (error.code === "REGISTRY_INVALID") return 3;
  if (["IMAGE_NOT_FOUND", "PODMAN_UNAVAILABLE", "PODMAN_FAILED", "RUNTIME_UNAVAILABLE", "GIT_FAILED", "REGISTRY_NOT_CONFIGURED"].includes(error.code)) return 4;
  return 1;
}
