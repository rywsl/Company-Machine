import { expect, test } from "bun:test";
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runChangeSetCli } from "../src/changeset-cli";
import { diffChangeFacts, scoreChangeRisk } from "../src/legacy/change-facts";
import { ChangeSetStore } from "../src/legacy/changesets";
import { ChangeSetStages } from "../src/legacy/change-stages";
import type { VerificationContracts } from "../src/legacy/change-verification";
import { ContractRunner } from "../src/legacy/contract";
import { Context } from "../src/legacy/context";
import { CounterIds, FakeClock } from "../src/legacy/foundation";
import { EpochStore } from "../src/legacy/registry-epochs";
import { Runner } from "../src/legacy/runner";
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
    expect(await risk.advance(claimed.id)).toEqual(pending);
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
    expect(await stage.advance(draft.id)).toEqual(shadow);
    expect(w.epochs.pointers()).toMatchObject({ active: w.base, candidate: draft.commit });
    expect(await git(w.registry, "rev-parse", "refs/factory/active")).toBe(w.base);
  } finally { w.close(); }
});
