import { expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runChangeSetCli } from "../src/changeset-cli";
import { main as runFactoryCli } from "../src/cli";
import { diffChangeFacts, scoreChangeRisk } from "../src/legacy/change-facts";
import { ChangeSetStore } from "../src/legacy/changesets";
import { ChangeSetStages } from "../src/legacy/change-stages";
import type { VerificationContracts } from "../src/legacy/change-verification";
import { ContractRunner } from "../src/legacy/contract";
import { Context } from "../src/legacy/context";
import { CounterIds, FakeClock, canonicalJson, utcIso } from "../src/legacy/foundation";
import { EpochStore } from "../src/legacy/registry-epochs";
import { Runner } from "../src/legacy/runner";
import { RunStore } from "../src/legacy/runs";
import { GovernanceSafeMode, runMetrics } from "../src/legacy/change-rollout";
import { RegistrySelection } from "../src/registry-selection";
import { LocalProvider } from "../src/legacy/sandbox";

async function git(root: string, ...args: string[]): Promise<string> {
  const bun = Bun as unknown as { spawn(argv: string[], options: object): { stdout: ReadableStream; stderr: ReadableStream; exited: Promise<number> } };
  const child = bun.spawn(["git", "-C", root, "-c", "user.name=Acceptance", "-c", "user.email=acceptance@example.invalid", "-c", "commit.gpgsign=false", ...args], { stdout: "pipe", stderr: "pipe" });
  const [status, output, error] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()]);
  if (status !== 0) throw new Error(`${args[0]} failed: ${error}`);
  return output.trim();
}

async function world() {
  const root = mkdtempSync(join(tmpdir(), "mao-changeset-"));
  const context = Context.open(root, { clock: new FakeClock(new Date("2026-11-10T00:00:00Z")), ids: new CounterIds() });
  const registry = join(root, "registry");
  const config = (freeze: object[] = []) => writeFileSync(join(root, "factory.json"), JSON.stringify({ governance: { registry: "registry", freeze } }));
  const close = () => { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 80 }); };
  try {
    cpSync("tests/fixtures/registry-m2", registry, { recursive: true });
    await git(registry, "init", "--initial-branch=main", "--template=");
    await git(registry, "add", ".");
    await git(registry, "commit", "-m", "Initial Registry");
    const base = await git(registry, "rev-parse", "HEAD");
    config();
    const epochs = await EpochStore.open(context);
    await epochs.init(base, "human:acceptance");
    await git(registry, "switch", "-c", "candidate-work");
    return { root, registry, context, base, epochs, changesets: new ChangeSetStore(context, epochs), config, close };
  } catch (error) { close(); throw error; }
}

function document(base: string, fields: object = {}) {
  return { apiVersion: "mao/v1", kind: "ChangeSet", metadata: { name: "cs-draft", proposer: "acceptance" }, spec: {
    base_epoch: base, intent: "验收测试", changes: [{ op: "upsert", kind: "Station", name: "upper" }],
    rollout: { shadow: { runs: 1, budget_usd: 1 }, canary: { percent: 50, min_runs: 2 }, observe: "1h", slo: { success_rate: ">= 0.9" } },
    ...fields,
  } };
}

async function commitFile(registry: string, path: string, data: string) {
  writeFileSync(join(registry, path), data);
  await git(registry, "add", "--", path);
  await git(registry, "commit", "-m", `Change ${path}`);
  return git(registry, "rev-parse", "HEAD");
}

function recordSample(context: Context, options: { id: string; epoch: string; state: string; cost?: number; at?: string; dryRun?: boolean }): void {
  new RunStore(context.db, context.clock, context.ids);
  const { id, epoch, state, cost = 1, at = utcIso(context.clock.now()), dryRun = false } = options;
  context.db.query(`INSERT INTO runs (id, line, epoch, state, plan, plan_digest, inputs, budget_cap_usd, spent_usd, trigger, created_at, updated_at, sink_mode)
    VALUES (?, 'Line:note-pipeline@1', ?, ?, '{}', 'digest', '{}', 10, ?, '{}', ?, ?, ?)`)
    .run(id, epoch, state, cost, at, at, dryRun ? "dry-run" : "execute");
}

test("proposes a distinct control-plane commit and persists draft history", { timeout: 90_000 }, async () => {
  const w = await world();
  try {
    const original = readFileSync(join(w.registry, "stations", "upper.yaml"), "utf8");
    const source = await commitFile(w.registry, "stations/upper-v2.yaml", original.replace("version: 1", "version: 2").replace("60s", "30s"));
    const proposed = document(w.base.slice(0, 12), { risk_tier: "T0", status: "active" });
    const file = join(w.root, "changeset.yaml");
    writeFileSync(file, Bun.YAML.stringify(proposed));
    const log = console.log;
    const outputs: unknown[] = [];
    try {
      console.log = (value) => outputs.push(JSON.parse(String(value)));
      expect(await runChangeSetCli(w.context, "propose", [file, "--commit", source, "--by", "human:a"])).toBe(0);
      expect(await runChangeSetCli(w.context, "show", ["cs-0001"])).toBe(0);
      expect(await runChangeSetCli(w.context, "ls", ["--state", "draft"])).toBe(0);
    } finally { console.log = log; }
    const cs = w.changesets.get("cs-0001");
    expect(outputs).toEqual([cs, cs, [cs]]);
    expect(cs).toMatchObject({ id: "cs-0001", state: "draft", base_epoch: w.base, source_commit: source, risk_tier: null, proposer_runtime: null });
    expect(cs.commit).not.toBe(source);
    expect(cs.document.spec).not.toHaveProperty("risk_tier");
    expect(cs.document.spec).not.toHaveProperty("status");
    expect(await git(w.registry, "rev-parse", `${cs.commit}^{tree}`)).toBe(await git(w.registry, "rev-parse", `${source}^{tree}`));
    expect(await git(w.registry, "rev-parse", `${cs.commit}^`)).toBe(w.base);
    expect(await git(w.registry, "show", "-s", "--format=%B", cs.commit)).toStartWith("changeset: cs-0001");
    expect(await git(w.registry, "rev-parse", "refs/changesets/cs-0001")).toBe(cs.commit);
    expect(await git(w.registry, "rev-parse", "refs/heads/main")).toBe(w.base);
    const [base, candidate] = await Promise.all([w.epochs.materialize(w.base), w.epochs.materialize(cs.commit)]);
    const classified = diffChangeFacts(base, candidate);
    expect(classified.immutable).toEqual([]);
    expect(classified.facts.field_changed).toEqual([["c0", "limits"]]);
    expect(scoreChangeRisk(classified.facts)).toMatchObject({ tier: "T1", reviews_required: 0, stages: ["shadow"] });
  } finally { w.close(); }
});

test("rejects constitution edits and a stale base before publishing a draft", { timeout: 90_000 }, async () => {
  const w = await world();
  try {
    const path = "constitution/invariants.mrl";
    const changed = await commitFile(w.registry, path, readFileSync(join(w.registry, path), "utf8") + "\n// attempted edit\n");
    await expect(w.changesets.propose(document(w.base), changed, { by: "fake-agent@1" })).rejects.toMatchObject({ code: "CONSTITUTION_FORBIDDEN", detail: { path } });
    const child = await commitFile(w.registry, "README.md", "new\n");
    await expect(w.changesets.propose(document(changed), child, { by: "human:acceptance" })).rejects.toMatchObject({ code: "CHANGESET_STALE", detail: { base_epoch: changed, active: w.base } });
    expect(w.changesets.list()).toEqual([]);
    expect(await git(w.registry, "for-each-ref", "--format=%(refname)", "refs/changesets")).toBe("");
  } finally { w.close(); }
});

test("rejects frozen, rate-limited, empty and malformed proposals", { timeout: 90_000 }, async () => {
  const w = await world();
  try {
    const source = await commitFile(w.registry, "README.md", "new\n");
    const by = "human:a";
    const until = "2026-11-20T00:00:00Z";
    w.config([{ start: "2026-11-01T00:00:00Z", until }]);
    await expect(w.changesets.propose(document(w.base), source, { by })).rejects.toMatchObject({ code: "FREEZE_WINDOW", detail: { until } });
    w.config();
    await expect(w.changesets.propose(document(w.base), w.base, { by })).rejects.toMatchObject({ code: "CHANGESET_INVALID", detail: { reason: "empty" } });
    await expect(w.changesets.propose(document(w.base, { rollout: { shadow: { budget_usd: -1 }, canary: { percent: 101, min_runs: 0 } } }), source, { by })).rejects.toMatchObject({ code: "CHANGESET_INVALID", detail: { problems: ["shadow.budget_usd", "canary.percent", "canary.min_runs"] } });
    const now = "2026-11-09T12:00:00.000Z";
    for (let n = 0; n < 10; n++) w.context.db.query(`INSERT INTO changesets
      (id, base_epoch, source_commit, "commit", state, proposer, document, activated_at, created_at, updated_at)
      VALUES (?, ?, ?, ?, 'active', 'x', '{}', ?, ?, ?)`)
      .run(`cs-old${n}`, w.base, w.base, String(n).padStart(40, "0"), now, now, now);
    await expect(w.changesets.propose(document(w.base), source, { by })).rejects.toMatchObject({ code: "RATE_LIMITED", detail: { count: 10 } });
    expect(w.changesets.list("draft")).toEqual([]);
  } finally { w.close(); }
});

test("classifies MRL risk without defaulting unscored facts to a low tier", () => {
  expect(() => scoreChangeRisk({ change: [["c0", "Station", "modify"]], field_changed: [["c0", "bogus"]] })).toThrow("风险规则给不出完整等级");
  let fault: unknown;
  try { scoreChangeRisk({ change: [["c0", "Station", "modify"]], field_changed: [["c0", "bogus"]] }); } catch (error) { fault = error; }
  expect(fault).toMatchObject({ code: "RISK_UNSCORED", detail: { changes: [["c0", "field bogus"]] } });
  let ungoverned: unknown;
  try { scoreChangeRisk({ change: [["c0", "Plan", "add"]] }); } catch (error) { ungoverned = error; }
  expect(ungoverned).toMatchObject({ code: "RISK_UNSCORED" });
  expect(scoreChangeRisk({ change: [["c0", "Machine", "modify"]], field_changed: [["c0", "runtime.image"]] }).tier).toBe("T2");
  expect(scoreChangeRisk({ change: [["c0", "Station", "modify"]], field_changed: [["c0", "limits"]], station_role: [["c0", "sink"]] }).tier).toBe("T3");
});

function contracts(verdict: "pass" | "fail", calls: string[] = []): VerificationContracts {
  const result = (kind: "diff" | "golden" | "equivalence" | "validator", subject: string) => {
    calls.push(`${kind}:${subject}`);
    return { id: `ct-${calls.length}`, kind, subject, verdict, detail: {}, at: "2026-11-10T00:00:00.000Z" };
  };
  return {
    runDiff: async (key) => result("diff", key), runGolden: async (key) => result("golden", key),
    runEquivalence: async (key) => result("equivalence", key), runValidator: async (key) => result("validator", key),
  };
}

test("verifies a T1 Station with contract results and rejects a shadow regression", { timeout: 120_000 }, async () => {
  const w = await world();
  try {
    const upper = readFileSync(join(w.registry, "stations", "upper.yaml"), "utf8");
    const source = await commitFile(w.registry, "stations/upper-v2.yaml", upper.replace("version: 1", "version: 2").replace("60s", "30s"));
    const draft = await w.changesets.propose(document(w.base), source, { by: "human:a" });
    const calls: string[] = [];
    const stage = new ChangeSetStages(w.changesets, {
      contracts: () => contracts("pass", calls),
      async shadow(cs, candidate, active, goldenOnly) {
        expect(cs.state).toBe("shadow");
        expect(candidate.epoch).toBe(cs.commit);
        expect(active.epoch).toBe(w.base);
        expect(w.epochs.pointers().candidate).toBe(cs.commit);
        expect(goldenOnly).toBe(false);
        return { ok: true, runs: 1, spent_usd: 0, regressions: [{ kind: "verdict", subject: "upper", detail: {} }] };
      },
    });
    const verified = await stage.advance(draft.id);
    expect(verified).toMatchObject({ state: "verified", risk_tier: "T1", reviews_required: 0, report: { code: null, impact: { stations: ["Station:upper@2"] } } });
    expect(calls).toContain("diff:Station:upper@2");
    const rejected = await stage.advance(draft.id);
    expect(rejected).toMatchObject({ state: "rejected", report: { code: "SHADOW_FAILED" } });
    expect(w.epochs.pointers().candidate).toBeNull();
    expect(w.epochs.current()).toBe(w.base);
    expect(w.context.db.query<{ data: string }>("SELECT data FROM events WHERE kind = 'changeset.state' ORDER BY seq").all().map(({ data }) => JSON.parse(data).to))
      .toEqual(["verifying", "verified", "shadow", "rejected"]);

    const failingDraft = await w.changesets.propose(document(w.base), source, { by: "human:a" });
    const v4 = new ChangeSetStages(w.changesets, { contracts: () => contracts("fail") });
    expect(await v4.advance(failingDraft.id)).toMatchObject({ state: "rejected", report: { code: "V4_FAILED", detail: { results: ["ct-1", "ct-2"] } } });
  } finally { w.close(); }
});

test("rejects undeclared, immutable and backward-incompatible Registry changes", { timeout: 120_000 }, async () => {
  const w = await world();
  try {
    const by = "human:acceptance";
    const stage = new ChangeSetStages(w.changesets, { contracts() { throw new Error("V4 must follow structural checks"); } });
    const machinePath = "machines/toolbox/machine.yaml";
    const machine = readFileSync(join(w.registry, machinePath), "utf8");
    const changed = machine.replace("version: 1", "version: 2").replace("09b42648636a8405", "19b42648636a8405");
    const source = await commitFile(w.registry, machinePath, machine + "\n---\n" + changed);
    const upperPath = "stations/upper.yaml";
    const upper = readFileSync(join(w.registry, upperPath), "utf8");
    const withUpper = await commitFile(w.registry, "stations/upper-v2.yaml", upper.replace("version: 1", "version: 2").replace("60s", "30s"));
    expect(source).not.toBe(withUpper);
    const mismatch = await w.changesets.propose(document(w.base), withUpper, { by });
    expect(await stage.advance(mismatch.id)).toMatchObject({ state: "rejected", report: { code: "CHANGESET_MISMATCH", detail: { actual_only: [["Machine", "toolbox", "upsert"]] } } });

    const claimed = await w.changesets.propose(document(w.base, { changes: [{ op: "upsert", kind: "Machine", name: "toolbox" }], risk_tier: "T0" }), source, { by });
    const risk = new ChangeSetStages(w.changesets, { contracts: () => contracts("pass") });
    const pending = await risk.advance(claimed.id);
    expect(pending).toMatchObject({ state: "verified", risk_tier: "T2", reviews_required: 1 });
    expect(pending.document.spec).not.toHaveProperty("risk_tier");
    expect(await risk.advance(claimed.id)).toMatchObject({ state: "verified", risk_tier: "T2", report: { reason: "reviews" } });
    expect(w.epochs.pointers().candidate).toBeNull();

    await git(w.registry, "switch", "-C", "candidate-work", w.base);
    const immutable = await commitFile(w.registry, upperPath, upper.replace("60s", "90s"));
    const inPlace = await w.changesets.propose(document(w.base), immutable, { by });
    expect(await stage.advance(inPlace.id)).toMatchObject({ state: "rejected", report: { code: "V3_IMMUTABLE", detail: { key: "Station:upper@1" } } });

    await git(w.registry, "switch", "-C", "candidate-work", w.base);
    const typePath = "types/note.yaml";
    const note = readFileSync(join(w.registry, typePath), "utf8");
    const minor = note.replace('version: "1.0"', 'version: "1.1"').replace("{path: note.txt, media_type: text/plain, required: true}", "{path: text.txt, media_type: text/plain, required: true}");
    const newMinor = await commitFile(w.registry, typePath, note + "\n---\n" + minor);
    const incompatible = await w.changesets.propose(document(w.base, { changes: [{ op: "upsert", kind: "Type", name: "note.text" }] }), newMinor, { by });
    expect(await stage.advance(incompatible.id)).toMatchObject({ state: "rejected", report: { code: "V3_INCOMPATIBLE" } });
  } finally { w.close(); }
});

test("executes real diff and GoldenSet jobs before recording a successful shadow", { timeout: 120_000 }, async () => {
  const w = await world();
  try {
    const local = new LocalProvider(Object.fromEntries(["count-text", "upper-text"].map((command) => [
      command, [Bun.argv[0], join(import.meta.dir, `../../tests/fakes/tools/${command.replaceAll("-", "_")}.ts`)],
    ])));
    const baseline = new ContractRunner(new Runner(w.context, await w.epochs.materialize(w.base), local));
    expect(await baseline.runGolden("upper-golden@1")).toMatchObject({ verdict: "pass", detail: { cases: [{ sealed: true }] } });
    const upper = readFileSync(join(w.registry, "stations", "upper.yaml"), "utf8");
    const source = await commitFile(w.registry, "stations/upper-v2.yaml", upper.replace("version: 1", "version: 2").replace("60s", "30s"));
    const draft = await w.changesets.propose(document(w.base, { rollout: { shadow: { budget_usd: 1 }, observe: "1h" } }), source, { by: "human:a" });
    const stage = new ChangeSetStages(w.changesets, { provider: () => local });
    expect(await stage.advance(draft.id)).toMatchObject({ state: "verified", risk_tier: "T1" });
    expect(baseline.list({ kind: "diff", subject: "Station:upper@2" })[0]).toMatchObject({ verdict: "pass", detail: { cases: [{ old_sealed: true, new_sealed: true, facts_equal: true }] } });
    const shadow = await stage.advance(draft.id);
    expect(shadow).toMatchObject({ state: "shadow", report: { ok: true, detail: { runs: 1, regressions: [] } } });
    expect(w.epochs.pointers()).toMatchObject({ active: w.base, candidate: draft.commit });
    w.config([{ start: "2026-11-01T00:00:00Z", until: "2026-11-20T00:00:00Z" }]);
    expect(await stage.advance(draft.id)).toMatchObject({ state: "shadow", report: { reason: "freeze" } });
    expect(w.epochs.current()).toBe(w.base);
    w.config();
    expect(await stage.advance(draft.id)).toMatchObject({ state: "active" });
    expect(w.epochs.pointers()).toEqual({ active: draft.commit, candidate: null, previous: w.base });
    expect(await git(w.registry, "rev-parse", "refs/factory/active")).toBe(draft.commit);
    expect(await git(w.registry, "rev-parse", "refs/heads/main")).toBe(w.base);
    recordSample(w.context, { id: "observation-good", epoch: draft.commit, state: "SUCCEEDED" });
    (w.context.clock as FakeClock).advance(7200);
    const observed = await stage.advance(draft.id);
    expect(observed).toMatchObject({ state: "active", report: { stage: "observe", ok: true } });
    expect(await stage.advance(draft.id)).toEqual(observed);
  } finally { w.close(); }
});

test("records independent reviews and uses canary SLO samples before promotion", { timeout: 120_000 }, async () => {
  const w = await world();
  try {
    const path = "machines/toolbox/machine.yaml";
    const machine = readFileSync(join(w.registry, path), "utf8");
    const source = await commitFile(w.registry, path, machine + "\n---\n" + machine.replace("version: 1", "version: 2").replace("09b42648636a8405", "19b42648636a8405"));
    const stage = new ChangeSetStages(w.changesets, {
      contracts: () => contracts("pass"), shadow: async () => ({ ok: true, runs: 0, regressions: [], spent_usd: 0 }),
    });
    const proposal = () => w.changesets.propose(document(w.base, { changes: [{ op: "upsert", kind: "Machine", name: "toolbox" }] }), source, { by: "fake-agent@1" });
    const draft = await proposal();
    const review = (runtime: string, verdict = "approve") => w.changesets.addReview(draft.id, { runtime, verdict, note: "", by: "independent" });
    await expect(review("script@1")).rejects.toMatchObject({ code: "CHANGESET_STATE_INVALID" });
    expect(await stage.advance(draft.id)).toMatchObject({ state: "verified", risk_tier: "T2", proposer_runtime: "OperatorRuntime:fake-agent@1" });
    await expect(review("fake-agent@1")).rejects.toMatchObject({ code: "REVIEW_SAME_RUNTIME" });
    await expect(review("unknown@1")).rejects.toMatchObject({ code: "REVIEW_INVALID" });
    await expect(review("Station:upper@1")).rejects.toMatchObject({ code: "REVIEW_INVALID" });
    const candidate = await w.epochs.materialize(draft.commit);
    const denied = await review("script@1", "deny");
    expect(w.changesets.approvals(draft.id, candidate)).toEqual([]);
    expect(await stage.advance(draft.id)).toMatchObject({ state: "verified", report: { reason: "reviews" } });
    expect(await review("OperatorRuntime:script@1")).toBe(denied);
    expect(w.changesets.approvals(draft.id, candidate)).toEqual(["OperatorRuntime:script@1"]);
    w.context.db.query("UPDATE reviews SET runtime_digest='tampered' WHERE id=?").run(denied);
    expect(await stage.advance(draft.id)).toMatchObject({ state: "verified", report: { reason: "reviews" } });
    expect(await review("script@1")).toBe(denied);

    expect(await stage.advance(draft.id)).toMatchObject({ state: "shadow" });
    expect(w.epochs.pointers()).toMatchObject({ active: w.base, candidate: draft.commit });
    const canary = await stage.advance(draft.id);
    expect(canary).toMatchObject({ state: "canary", stage_started_at: utcIso(w.context.clock.now()) });
    expect(w.epochs.current()).toBe(w.base);
    const router = new RegistrySelection(w.context, w.registry);
    const line = "Line:note-pipeline@1";
    const picks = Object.fromEntries([w.base, draft.commit].map((commit) => [commit, 0]));
    for (let index = 0; index < 12; index += 1) {
      const inputs = { task: "task-" + index };
      const hash = createHash("sha256").update(line + ":" + canonicalJson(inputs)).digest("hex");
      const expected = Number.parseInt(hash.slice(0, 8), 16) % 100 < 50 ? draft.commit : w.base;
      expect((await router.forLine("note-pipeline@1", () => inputs)).epoch).toBe(expected);
      picks[expected]++;
    }
    expect(picks[w.base]).toBeGreaterThan(0);
    expect(picks[draft.commit]).toBeGreaterThan(0);

    recordSample(w.context, { id: "active-good", epoch: w.base, state: "SUCCEEDED" });
    recordSample(w.context, { id: "canary-running", epoch: draft.commit, state: "RUNNING" });
    recordSample(w.context, { id: "shadow-good", epoch: draft.commit, state: "SUCCEEDED", dryRun: true });
    recordSample(w.context, { id: "canary-first", epoch: draft.commit, state: "SUCCEEDED" });
    expect(await stage.advance(draft.id)).toMatchObject({ state: "canary", report: { reason: "samples" } });
    recordSample(w.context, { id: "canary-second", epoch: draft.commit, state: "SUCCEEDED" });
    expect(runMetrics(w.context, draft.commit, canary.stage_started_at!)).toEqual({
      runs: 2, jobs: 0, success_rate: 1, gate_reject_rate: null, cost_per_run_usd: 1,
    });
    expect(await stage.advance(draft.id)).toMatchObject({ state: "active" });
    expect(w.epochs.pointers()).toEqual({ active: draft.commit, candidate: null, previous: w.base });
    expect(await git(w.registry, "rev-parse", "refs/factory/active")).toBe(draft.commit);
  } finally { w.close(); }
});

test("keeps withdrawn ChangeSets from advancing and retains their Git audit refs", { timeout: 90_000 }, async () => {
  const w = await world();
  try {
    const upper = readFileSync(join(w.registry, "stations", "upper.yaml"), "utf8");
    const source = await commitFile(w.registry, "stations/upper-v2.yaml", upper.replace("version: 1", "version: 2").replace("60s", "30s"));
    const draft = await w.changesets.propose(document(w.base), source, { by: "human:a" });
    expect(w.changesets.withdraw(draft.id, "human:a").state).toBe("withdrawn");
    await expect(new ChangeSetStages(w.changesets).advance(draft.id)).rejects.toMatchObject({ code: "CHANGESET_STATE_INVALID" });
    expect(() => w.changesets.get("cs-9999")).toThrow("找不到变更");
    expect(await git(w.registry, "rev-parse", `refs/changesets/${draft.id}`)).toBe(draft.commit);
  } finally { w.close(); }
});

test("rolls back failed canary and observation SLOs without moving unrelated epoch pointers", { timeout: 120_000 }, async () => {
  const w = await world();
  try {
    const stage = new ChangeSetStages(w.changesets, {
      contracts: () => contracts("pass"), shadow: async () => ({ ok: true, runs: 0, regressions: [], spent_usd: 0 }),
    });
    const path = "machines/toolbox/machine.yaml";
    const machine = readFileSync(join(w.registry, path), "utf8");
    const changed = await commitFile(w.registry, path, machine + "\n---\n" + machine.replace("version: 1", "version: 2").replace("09b42648636a8405", "19b42648636a8405"));
    const machineDraft = await w.changesets.propose(document(w.base, { changes: [{ op: "upsert", kind: "Machine", name: "toolbox" }] }), changed, { by: "fake-agent@1" });
    expect((await stage.advance(machineDraft.id)).state).toBe("verified");
    await w.changesets.addReview(machineDraft.id, { runtime: "script@1", verdict: "approve", note: "", by: "script" });
    expect((await stage.advance(machineDraft.id)).state).toBe("shadow");
    expect((await stage.advance(machineDraft.id)).state).toBe("canary");
    recordSample(w.context, { id: "failed-canary", epoch: machineDraft.commit, state: "FAILED" });
    recordSample(w.context, { id: "passed-canary", epoch: machineDraft.commit, state: "SUCCEEDED" });
    expect(await stage.advance(machineDraft.id)).toMatchObject({
      state: "rolled-back", report: { code: "SLO_ROLLBACK", detail: { metric: "success_rate", value: 0.5, bound: ">= 0.9" } },
    });
    expect(w.epochs.pointers()).toEqual({ active: w.base, candidate: null, previous: null });

    await git(w.registry, "switch", "-C", "candidate-work", w.base);
    const upper = readFileSync(join(w.registry, "stations/upper.yaml"), "utf8");
    const source = await commitFile(w.registry, "stations/upper-v2.yaml", upper.replace("version: 1", "version: 2").replace("60s", "30s"));
    const upperDraft = await w.changesets.propose(document(w.base, {
      rollout: { shadow: { budget_usd: 1 }, observe: "1h", slo: { success_rate: ">= 0.9" } },
    }), source, { by: "human:a" });
    expect((await stage.advance(upperDraft.id)).state).toBe("verified");
    expect((await stage.advance(upperDraft.id)).state).toBe("shadow");
    expect((await stage.advance(upperDraft.id)).state).toBe("active");
    recordSample(w.context, { id: "failed-live", epoch: upperDraft.commit, state: "FAILED" });
    (w.context.clock as FakeClock).advance(7200);
    expect(await stage.advance(upperDraft.id)).toMatchObject({ state: "rolled-back", report: { code: "SLO_ROLLBACK" } });
    expect(w.epochs.pointers()).toEqual({ active: w.base, candidate: null, previous: upperDraft.commit });
    expect(await git(w.registry, "rev-parse", "refs/factory/active")).toBe(w.base);
    expect(await git(w.registry, "rev-parse", "refs/heads/main")).toBe(w.base);
  } finally { w.close(); }
});

test("aggregates terminal rollout run outcomes, current cost, and distinct rejected jobs", () => {
  const root = mkdtempSync(join(tmpdir(), "mao-rollout-metrics-"));
  const context = Context.open(root);
  try {
    const since = "2026-11-10T00:00:00.000Z";
    const epoch = "e".repeat(40);
    expect(runMetrics(context, epoch, since)).toEqual({ runs: 0, jobs: 0, success_rate: null, gate_reject_rate: null, cost_per_run_usd: null });
    recordSample(context, { id: "ok", epoch, state: "SUCCEEDED", cost: 1, at: since });
    recordSample(context, { id: "failed", epoch, state: "FAILED", cost: 2, at: since });
    recordSample(context, { id: "running", epoch, state: "RUNNING", cost: 9, at: since });
    recordSample(context, { id: "old", epoch, state: "SUCCEEDED", cost: 9, at: "2026-11-09T00:00:00.000Z" });
    const link = context.db.query("INSERT INTO run_node_jobs (run_id,node_id,job_id,created_at) VALUES (?, 'node', ?, ?)");
    for (const [run, job] of [["ok", "j1"], ["ok", "j2"], ["failed", "j3"], ["failed", "j4"], ["old", "j5"]]) link.run(run, job, since);
    context.db.query("INSERT INTO job_events (id,job_id,kind,data,at) VALUES ('reject-j1','j1','job.state',?,?)").run('{"to":"REJECTED"}', since);
    context.db.query("INSERT INTO events (id,job_id,kind,data,at) VALUES ('legacy-reject-j1','j1','job.state',?,?)").run('{"to":"REJECTED"}', since);
    expect(runMetrics(context, epoch, since)).toEqual({ runs: 2, jobs: 4, success_rate: 0.5, gate_reject_rate: 0.25, cost_per_run_usd: 1.5 });
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

test("pauses higher-risk releases after repeated rollbacks and resumes after cooldown", { timeout: 120_000 }, async () => {
  const w = await world();
  try {
    const stage = new ChangeSetStages(w.changesets, {
      contracts: () => contracts("pass"), shadow: async () => ({ ok: true, runs: 0, regressions: [], spent_usd: 0 }),
    });
    const stationPath = "stations/upper-v2.yaml";
    const upper = readFileSync(join(w.registry, "stations/upper.yaml"), "utf8").replace("version: 1", "version: 2");
    const clock = w.context.clock as FakeClock;
    const proposeStation = async (timeout: string) => {
      await git(w.registry, "switch", "-C", "candidate-work", w.base);
      const source = await commitFile(w.registry, stationPath, upper.replace("60s", timeout));
      return w.changesets.propose(document(w.base, {
        rollout: { shadow: { budget_usd: 1 }, observe: "1h", slo: { success_rate: ">= 0.9" } },
      }), source, { by: "human:a" });
    };
    for (const timeout of ["30s", "45s"]) {
      const proposal = await proposeStation(timeout);
      expect(await stage.tick()).toMatchObject([{ state: "verified", risk_tier: "T1" }]);
      expect(await stage.tick()).toMatchObject([{ state: "shadow" }]);
      expect(await stage.tick()).toMatchObject([{ state: "active" }]);
      recordSample(w.context, { id: `failed-${proposal.id}`, epoch: proposal.commit, state: "FAILED" });
      clock.advance(7200);
      expect(await stage.tick()).toMatchObject([{ state: "rolled-back", report: { code: "SLO_ROLLBACK" } }]);
    }
    await git(w.registry, "update-ref", "-d", "refs/factory/active");
    expect(await stage.tick()).toEqual([]);
    expect(await git(w.registry, "rev-parse", "refs/factory/active")).toBe(w.base);
    const safeMode = new GovernanceSafeMode(w.context);
    const status = safeMode.current();
    expect(status).toEqual({ on: true, level: 1, since: utcIso(clock.now()), until: utcIso(new Date(clock.now().getTime() + 3_600_000)) });
    const output: unknown[] = [];
    const log = console.log;
    try {
      console.log = (value) => output.push(JSON.parse(String(value)));
      expect(await runFactoryCli(["governance", "safe-mode", "--root", w.root, "--json"])).toBe(0);
    } finally { console.log = log; }
    expect(output).toEqual([status]);

    await git(w.registry, "switch", "-C", "candidate-work", w.base);
    const machinePath = "machines/toolbox/machine.yaml";
    const machine = readFileSync(join(w.registry, machinePath), "utf8");
    const source = await commitFile(w.registry, machinePath, machine + "\n---\n" + machine.replace("version: 1", "version: 2").replace("09b42648636a8405", "19b42648636a8405"));
    const high = await w.changesets.propose(document(w.base, { changes: [{ op: "upsert", kind: "Machine", name: "toolbox" }] }), source, { by: "fake-agent@1" });
    expect(await stage.tick()).toMatchObject([{ id: high.id, state: "verified", risk_tier: "T2" }]);
    await w.changesets.addReview(high.id, { runtime: "script@1", verdict: "approve", note: "", by: "script" });
    expect(await stage.advance(high.id)).toMatchObject({ state: "verified", report: { reason: "safe-mode" } });
    expect(await stage.tick()).toEqual([]);

    const low = await proposeStation("50s");
    expect(await stage.tick()).toMatchObject([{ id: low.id, state: "verified" }]);
    expect(await stage.tick()).toMatchObject([{ id: low.id, state: "shadow" }]);
    expect(await stage.tick()).toMatchObject([{ id: low.id, state: "active" }]);
    expect(w.epochs.pointers()).toMatchObject({ active: low.commit, candidate: null });
    await w.epochs.rollback(w.base, "human:a");

    clock.advance(86400 + 3600 + 1);
    expect(await stage.advance(low.id)).toMatchObject({ state: "active", report: { stage: "observe", ok: true } });
    expect(await stage.tick()).toMatchObject([{ id: high.id, state: "shadow" }]);
    expect(safeMode.current()).toEqual({ ...status, on: false });

    const { db } = w.context;
    db.exec("CREATE TABLE audit_findings (id TEXT PRIMARY KEY, state TEXT NOT NULL)");
    db.query("INSERT INTO audit_findings VALUES ('audit-1', 'wake')").run();
    expect(await stage.tick()).toMatchObject([{ id: high.id, state: "shadow", report: { reason: "safe-mode" } }]);
    db.query("UPDATE audit_findings SET state='closed'").run();
    clock.advance(7200);
    expect(await stage.tick()).toMatchObject([{ id: high.id, state: "canary" }]);
    db.query("UPDATE audit_findings SET state='wake'").run();
    recordSample(w.context, { id: "canary-success-1", epoch: high.commit, state: "SUCCEEDED" });
    recordSample(w.context, { id: "canary-success-2", epoch: high.commit, state: "SUCCEEDED" });
    expect(await stage.tick()).toMatchObject([{ id: high.id, state: "canary", report: { reason: "safe-mode" } }]);
    recordSample(w.context, { id: "canary-failure", epoch: high.commit, state: "FAILED" });
    expect(await stage.tick()).toMatchObject([{ id: high.id, state: "rolled-back", report: { code: "SLO_ROLLBACK" } }]);
    expect(w.epochs.pointers()).toMatchObject({ active: w.base, candidate: null });
  } finally { w.close(); }
});

test("retains safe mode for an open audit finding and doubles the next cooldown", async () => {
  const w = await world();
  try {
    const { db } = w.context;
    const clock = w.context.clock as FakeClock;
    const safeMode = new GovernanceSafeMode(w.context);
    db.exec("CREATE TABLE audit_findings (id TEXT PRIMARY KEY, state TEXT NOT NULL)");
    db.query("INSERT INTO audit_findings VALUES ('audit-1', 'wake')").run();
    const first = safeMode.evaluate();
    expect(first).toMatchObject({ on: true, level: 1 });
    clock.advance(3600);
    const extended = safeMode.evaluate();
    expect(extended).toEqual({ ...first, until: utcIso(new Date(clock.now().getTime() + 3_600_000)) });
    db.query("UPDATE audit_findings SET state='closed'").run();
    expect(safeMode.evaluate()).toEqual(extended);
    clock.advance(3600);
    expect(safeMode.evaluate()).toEqual({ ...extended, on: false });
    db.query("UPDATE audit_findings SET state='wake'").run();
    expect(safeMode.evaluate()).toEqual({ on: true, level: 2, since: utcIso(clock.now()), until: utcIso(new Date(clock.now().getTime() + 7_200_000)) });
    const events = db.query<{ data: string }>("SELECT data FROM events WHERE kind='governance.safe_mode' ORDER BY seq").all().map(({ data }) => JSON.parse(data));
    expect(events.map(({ on, level, reason }) => [on, level, reason])).toEqual([
      [true, 1, "audit-wake"], [true, 1, "audit-wake"], [false, 1, null], [true, 2, "audit-wake"],
    ]);
  } finally { w.close(); }
});
