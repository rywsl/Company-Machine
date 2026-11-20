import { object } from "./change-facts";
import { goldenKeys, type VerificationContracts } from "./change-verification";
import type { ChangeSet } from "./changesets";
import { FactoryError } from "./errors";
import { canonicalJson } from "./foundation";
import { LineRunner } from "./line-runner";
import type { Run } from "./runs";
import type { Runner } from "./runner";
import type { Snapshot } from "./snapshot";

type Regression = { kind: string; subject: string; detail: Record<string, unknown> };
export type ShadowReport = { ok: boolean; runs: number; spent_usd: number; regressions: Regression[] };
export type ShadowExecution = (cs: ChangeSet, candidate: Snapshot, active: Snapshot, goldenOnly: boolean) => Promise<ShadowReport>;

export function runShadowWith(runner: (snapshot: Snapshot) => Promise<Runner>, contracts: (snapshot: Snapshot) => Promise<VerificationContracts>): ShadowExecution {
  return async (cs, candidate, active, goldenOnly) => {
    const regressions: Regression[] = [];
    let spent = 0;
    let runs = 0;
    if (goldenOnly) {
      const stations = object(cs.report.impact).stations;
      const goldens = goldenKeys(candidate, Array.isArray(stations) && stations.length ? stations.map(String) : candidate.keys("Station"));
      const contract = await contracts(candidate);
      for (const key of goldens) {
        const result = await contract.runGolden(key);
        runs += 1;
        spent += Number(object(result.detail.observed).cost_usd ?? 0);
        if (result.verdict !== "pass") regressions.push({ kind: "golden", subject: key, detail: { result: result.id } });
      }
      return { ok: true, runs, spent_usd: Number(spent.toFixed(6)), regressions };
    }

    const line = new LineRunner(await runner(candidate));
    const { context } = line.runner;
    const shadow = object(object(object(cs.document.spec).rollout).shadow);
    const limit = shadow.runs ?? 0;
    if (typeof limit !== "number" || !Number.isSafeInteger(limit) || limit < 0) {
      throw new FactoryError("SHADOW_CONFIG_INVALID", "影子样本数量必须是非负整数", { changeset: cs.id });
    }
    const samples = context.db.query<{ id: string }>(`SELECT id FROM runs WHERE epoch = ? AND state = 'SUCCEEDED' AND sink_mode = 'execute' ORDER BY created_at DESC, id DESC LIMIT ?`).all(active.epoch, limit);
    const types = (outputs: Run["outputs"]) => Object.fromEntries(Object.entries(outputs).map(([port, ids]) => [port, ids.map((id) => context.warehouse.get(id).type).sort()]));

    for (const { id } of samples) {
      const original = line.runs.get(id);
      let candidateRun: Run;
      try {
        const prior = context.db.query<{ id: string }>(`SELECT id FROM runs WHERE epoch = ? AND sink_mode = 'dry-run'
          AND json_extract(trigger, '$.on') = 'shadow' AND json_extract(trigger, '$.changeset') = ?
          AND json_extract(trigger, '$.base_run') = ? ORDER BY id LIMIT 1`).get(candidate.epoch, cs.id, id);
        const started = prior ?? line.start(original.line, {
          inputs: original.inputs, variants: original.variants, budgetCapUsd: Math.min(original.budgetCapUsd, Math.max(0, Number(shadow.budget_usd ?? 0) - spent)),
          trigger: { on: "shadow", changeset: cs.id, base_run: id }, useCache: false, sinkMode: "dry-run",
        });
        candidateRun = await line.run(started.id);
      } catch (error) {
        if (!(error instanceof FactoryError)) throw error;
        regressions.push({ kind: "node_failed", subject: original.line, detail: { base_run: id, error: error.code } });
        continue;
      }
      runs += 1;
      spent += candidateRun.spentUsd;
      if (candidateRun.state !== "SUCCEEDED") {
        regressions.push({ kind: "node_failed", subject: original.line, detail: { base_run: id, candidate_run: candidateRun.id, state: candidateRun.state } });
        continue;
      }
      const previous = new Map(line.runs.nodes(id).map((node) => [node.nodeId, node]));
      for (const node of line.runs.nodes(candidateRun.id)) {
        const baseNode = previous.get(node.nodeId);
        if (baseNode?.state !== "DONE" || node.state !== "DONE") continue;
        const before = types(baseNode.outputs);
        const after = types(node.outputs);
        if (canonicalJson(before) !== canonicalJson(after)) regressions.push({ kind: "type_changed", subject: node.nodeId, detail: { base: before, candidate: after } });
      }
    }
    return { ok: true, runs, spent_usd: Number(spent.toFixed(6)), regressions };
  };
}
