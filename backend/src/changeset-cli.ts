import { readFileSync } from "node:fs";
import { loadDocuments } from "../mrl/factory/yamlio";
import type { ContainerProvider } from "../runtime/podman";
import { ChangeSetStages } from "./legacy/change-stages";
import { GovernanceSafeMode } from "./legacy/change-rollout";
import { ChangeSetStore } from "./legacy/changesets";
import type { Context } from "./legacy/context";
import { FactoryError } from "./legacy/errors";
import { canonicalJson } from "./legacy/foundation";
import { EpochStore } from "./legacy/registry-epochs";

type GovernanceCliOptions = {
  registry?: string;
  provider?: () => ContainerProvider | Promise<ContainerProvider>;
};

export const changesetCommands = { governance: ["tick", "safe-mode"], changeset: [
  "propose FILE --commit COMMIT --by ACTOR", "ls [--state STATE]", "show ID", "advance ID",
  "review ID --runtime RUNTIME --verdict approve|deny --by ACTOR [--note TEXT]", "withdraw ID --by ACTOR",
] };

export async function runGovernanceCli(context: Context, operation: string, args: string[], options: GovernanceCliOptions = {}): Promise<number> {
  if (args.length || !changesetCommands.governance.includes(operation)) {
    throw new FactoryError("CLI_USAGE", "governance expects tick or safe-mode");
  }
  const epochs = await EpochStore.open(context, options);
  const result = operation === "safe-mode"
    ? new GovernanceSafeMode(context).current()
    : await new ChangeSetStages(new ChangeSetStore(context, epochs), { provider: options.provider }).tick();
  console.log(canonicalJson(result));
  return 0;
}

export async function runChangeSetCli(context: Context, operation: string, args: string[], options: GovernanceCliOptions = {}): Promise<number> {
  const usage = (message: string) => new FactoryError("CLI_USAGE", message);
  const optionNames: Record<string, string[]> = {
    ls: ["--state"], propose: ["--commit", "--by"], review: ["--runtime", "--verdict", "--by", "--note"],
    withdraw: ["--by"], advance: [], show: [],
  };
  if (!Object.hasOwn(optionNames, operation)) throw usage(`Unknown changeset command: ${operation}`);
  const allowed = new Set(optionNames[operation]);
  const flags = new Map<string, string>();
  let target: string | undefined;
  for (let i = 0; i < args.length; i++) {
    const token = args[i];
    if (!token.startsWith("-") && target === undefined && operation !== "ls") { target = token; continue; }
    if (!allowed.has(token) || flags.has(token)) throw usage(`Unexpected changeset argument: ${token}`);
    const value = args[++i];
    if (!value || value.startsWith("-")) throw usage(`${token} requires a value`);
    flags.set(token, value);
  }
  if (operation !== "ls" && !target) throw usage(`${operation} requires ${operation === "propose" ? "a file" : "an ID"}`);
  const commit = flags.get("--commit"), by = flags.get("--by");
  if (operation === "propose" && (!commit || !by)) throw usage("propose requires --commit and --by");
  const runtime = flags.get("--runtime"), verdict = flags.get("--verdict");
  if (operation === "review" && (!runtime || !verdict || !by)) throw usage("review requires --runtime, --verdict, and --by");
  if (operation === "withdraw" && !by) throw usage("withdraw requires --by");
  const changesets = new ChangeSetStore(context, await EpochStore.open(context, options));
  if (operation === "propose") {
    let documents;
    try { documents = loadDocuments(readFileSync(target!, "utf8")); }
    catch { throw new FactoryError("CHANGESET_INVALID", "无法读取 ChangeSet 文档", { file: target }); }
    if (documents.length !== 1) throw new FactoryError("CHANGESET_INVALID", "文件必须含有一个 ChangeSet 文档", { file: target });
    console.log(canonicalJson(await changesets.propose(documents[0].data, commit!, { by: by! })));
  } else if (operation === "advance") {
    console.log(canonicalJson(await new ChangeSetStages(changesets, { provider: options.provider }).advance(target!)));
  } else if (operation === "review") {
    const id = await changesets.addReview(target!, { runtime: runtime!, verdict: verdict!, by: by!, note: flags.get("--note") ?? "" });
    console.log(canonicalJson({ id }));
  } else if (operation === "withdraw") {
    console.log(canonicalJson(changesets.withdraw(target!, by!)));
  } else {
    console.log(canonicalJson(operation === "show" ? changesets.get(target!) : changesets.list(flags.get("--state"))));
  }
  return 0;
}
