import { ReceiptStore, receiptStatuses, verifyReceipt, type ReceiptStatus } from "./legacy/connectors";
import type { Context } from "./legacy/context";
import { FactoryError } from "./legacy/errors";
import { canonicalJson } from "./legacy/foundation";
import { dumpJob } from "./legacy/jobs";
import { Runner } from "./legacy/runner";
import type { Snapshot } from "./legacy/snapshot";
import type { ContainerProvider } from "../runtime/podman";

export const connectorCommands = {
  source: ["run STATION [--param NAME=VALUE]"],
  receipt: ["ls [--station STATION] [--status STATUS]", "show RECEIPT", "verify RECEIPT", "void RECEIPT --reason REASON [--by ACTOR]"],
};

type ConnectorOptions = {
  scope: keyof typeof connectorCommands;
  operation: string;
  args: string[];
  json: boolean;
  loadSnapshot: () => Promise<Snapshot>;
  provider: () => Promise<ContainerProvider>;
};

function usage(message: string): never {
  throw new FactoryError("CLI_USAGE", message);
}

function options(args: string[], allowed: string[]): Array<[string, string]> {
  const result: Array<[string, string]> = [];
  for (let index = 0; index < args.length; index += 2) {
    const [key, value] = [args[index], args[index + 1]];
    if (!allowed.includes(key)) usage(`unknown option: ${key}`);
    if (!value || value.startsWith("--")) usage(`${key} requires a value`);
    result.push([key, value]);
  }
  return result;
}

export async function runConnectorCli(context: Context, request: ConnectorOptions): Promise<number> {
  const { scope, operation, args, loadSnapshot, provider } = request;
  const [target, ...rest] = args;
  if (scope === "source") {
    if (operation !== "run") usage(`unknown source command: ${operation}`);
    if (!target || target.startsWith("--")) usage("Source station is required");
    const snapshot = await loadSnapshot();
    const key = snapshot.key("Station", target);
    const station = snapshot.spec(key);
    if (station.role !== "source") throw new FactoryError("ROLE_MISMATCH", "工位必须声明 Source 角色", { station: key, role: station.role, expected: "source" });
    const params = Object.fromEntries(options(rest, ["--param"]).map(([, value]) => {
      const split = value.indexOf("=");
      if (split < 1) usage("--param requires NAME=VALUE");
      const name = value.slice(0, split);
      const raw = value.slice(split + 1);
      if (station.params?.[name]?.type === "string" && !raw.startsWith('"')) return [name, raw];
      try { return [name, JSON.parse(raw)]; }
      catch { usage(`--param requires valid JSON for ${name}`); }
    }));
    const runner = new Runner(context, snapshot, await provider());
    const job = await runner.run(runner.submit(key, {}, params).id);
    console.log(canonicalJson(dumpJob(job)));
    return job.state === "SEALED" ? 0 : 1;
  }

  const receipts = new ReceiptStore(context);
  if (operation === "ls") {
    const fields = new Map(options(args, ["--station", "--status"]));
    const status = fields.get("--status");
    if (status && !receiptStatuses.includes(status as ReceiptStatus)) usage(`invalid receipt status: ${status}`);
    console.log(canonicalJson(receipts.list({ station: fields.get("--station"), status: status as ReceiptStatus | undefined })));
    return 0;
  }
  if (!target || target.startsWith("--")) usage("Receipt id is required");
  if (operation === "void") {
    const entries = options(rest, ["--reason", "--by"]);
    const fields = new Map(entries);
    const reason = fields.get("--reason");
    if (!reason || fields.size !== entries.length) usage("--reason and --by must appear at most once and --reason is required");
    const by = fields.get("--by") ?? process.env.FACTORY_HUMAN_BY ?? "human";
    console.log(canonicalJson(receipts.void(target, { by, reason })));
    return 0;
  }
  if (rest.length) usage(`unexpected argument: ${rest[0]}`);
  if (operation === "show") {
    console.log(canonicalJson(receipts.get(target)));
    return 0;
  }
  if (operation === "verify") {
    const existing = receipts.get(target);
    const result = await verifyReceipt(context, await loadSnapshot(), await provider(), existing);
    console.log(canonicalJson(receipts.settle(target, result)));
    return result.ok ? 0 : 1;
  }
  return usage(`unknown receipt command: ${operation}`);
}
