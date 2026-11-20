import { readFileSync } from "node:fs";
import { loadDocuments } from "../mrl/factory/yamlio";
import { ChangeSetStore } from "./legacy/changesets";
import type { Context } from "./legacy/context";
import { FactoryError } from "./legacy/errors";
import { canonicalJson } from "./legacy/foundation";
import { EpochStore } from "./legacy/registry-epochs";

export const changesetCommands = { changeset: ["propose FILE --commit COMMIT --by ACTOR", "ls [--state STATE]", "show ID"] };

export async function runChangeSetCli(context: Context, operation: string, args: string[], options: { registry?: string } = {}): Promise<number> {
  const usage = (message: string) => new FactoryError("CLI_USAGE", message);
  if (operation !== "ls" && operation !== "show" && operation !== "propose") throw usage(`Unknown changeset command: ${operation}`);
  const allowed = new Set(operation === "ls" ? ["--state"] : operation === "propose" ? ["--commit", "--by"] : []);
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
  if (operation !== "ls" && !target) throw usage(`${operation} requires ${operation === "show" ? "an ID" : "a file"}`);
  const commit = flags.get("--commit"), by = flags.get("--by");
  if (operation === "propose" && (!commit || !by)) throw usage("propose requires --commit and --by");
  const changesets = new ChangeSetStore(context, await EpochStore.open(context, options));
  if (operation === "propose") {
    let documents;
    try { documents = loadDocuments(readFileSync(target!, "utf8")); }
    catch { throw new FactoryError("CHANGESET_INVALID", "无法读取 ChangeSet 文档", { file: target }); }
    if (documents.length !== 1) throw new FactoryError("CHANGESET_INVALID", "文件必须含有一个 ChangeSet 文档", { file: target });
    console.log(canonicalJson(await changesets.propose(documents[0].data, commit!, { by: by! })));
  } else {
    console.log(canonicalJson(operation === "show" ? changesets.get(target!) : changesets.list(flags.get("--state"))));
  }
  return 0;
}
