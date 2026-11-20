import type { Context } from "./legacy/context";
import { FactoryError } from "./legacy/errors";
import { canonicalJson } from "./legacy/foundation";
import { EpochStore } from "./legacy/registry-epochs";

export const epochCommands = {
  epoch: ["init COMMIT [--by ACTOR]", "ls", "show [COMMIT]", "rollback [COMMIT] [--by ACTOR]", "adopt COMMIT [--by ACTOR]"],
};

export async function runEpochCli(context: Context, operation: string, args: string[], options: { registry?: string } = {}): Promise<number> {
  if (!["init", "ls", "show", "rollback", "adopt"].includes(operation)) {
    throw new FactoryError("CLI_USAGE", `Unknown epoch command: ${operation}`);
  }
  const canSetActor = operation === "init" || operation === "rollback" || operation === "adopt";
  let actor: string | undefined;
  let commit: string | undefined;
  for (let index = 0; index < args.length; index++) {
    const value = args[index];
    if (value === "--by" && canSetActor && actor === undefined) {
      actor = args[++index];
      if (!actor?.trim() || actor.startsWith("-")) throw new FactoryError("CLI_USAGE", "--by requires an actor");
    } else if (!value.startsWith("-") && commit === undefined && operation !== "ls") commit = value;
    else throw new FactoryError("CLI_USAGE", `Unexpected epoch argument: ${value}`);
  }
  if ((operation === "init" || operation === "adopt") && !commit) throw new FactoryError("CLI_USAGE", `${operation} requires a commit`);
  const by = actor ?? `human:${process.env.USER || process.env.USERNAME || "local"}`;
  const epochs = await EpochStore.open(context, options);
  let result: unknown;
  switch (operation) {
    case "ls": result = epochs.list(); break;
    case "show": result = await epochs.show(commit); break;
    case "init": result = { commit: await epochs.init(commit!, by) }; break;
    case "rollback": result = { commit: await epochs.rollback(commit, by) }; break;
    case "adopt": result = { commit: await epochs.adopt(commit!, by) }; break;
  }
  console.log(canonicalJson(result));
  return 0;
}
