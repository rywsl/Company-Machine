import { expect, test } from "bun:test";
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runChangeSetCli } from "../src/changeset-cli";
import { diffChangeFacts, scoreChangeRisk } from "../src/legacy/change-facts";
import { ChangeSetStore } from "../src/legacy/changesets";
import { Context } from "../src/legacy/context";
import { CounterIds, FakeClock } from "../src/legacy/foundation";
import { EpochStore } from "../src/legacy/registry-epochs";

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
