import { expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { existsSync, rmSync, symlinkSync } from "node:fs";
import { join, resolve } from "node:path";
import { ContractRunner } from "../src/legacy/contract";
import { Context, CounterIds, FakeClock, LocalProvider, Runner, Snapshot, loadSnapshot } from "../src/legacy";
import { runCommand, type SandboxSpec } from "../runtime/podman";

const commands = Object.fromEntries(["count-text", "upper-text", "write-proposal", "publish-copy", "local-file-check", "pull-notes", "exit-three"].map((name) => [
  name, [Bun.argv[0], join(import.meta.dir, `../../tests/fakes/tools/${name.replaceAll("-", "_")}.ts`)],
]));

async function withContract(run: (contract: ContractRunner) => Promise<void>, provider = new LocalProvider(commands)): Promise<void> {
  const context = Context.open(join("tmp", `contract-${randomUUID()}`), { clock: new FakeClock(), ids: new CounterIds() });
  try {
    const snapshot = await loadSnapshot("tests/fixtures/registry-m2");
    await run(new ContractRunner(new Runner(context, snapshot, provider)));
  } finally {
    context.close();
    rmSync(context.root, { recursive: true, force: true, maxRetries: 3 });
  }
}

test("runs validator accept/reject samples and audits removed negative fixtures", { timeout: 60_000 }, () => withContract(async (contract) => {
  const first = await contract.runValidator("note.text@1", "count-text", { timeoutS: 30 });
  expect(first).toMatchObject({ kind: "validator", verdict: "pass", detail: {
    accept: [{ path: "fixtures/note/ok.txt", ok: true }], reject: [{ path: "fixtures/note/empty.txt", ok: true }],
  } });
  const snapshot = contract.runner.snapshot;
  const old = new Snapshot(snapshot.root, snapshot.epoch, structuredClone(snapshot.docs), snapshot.result);
  const fixtures = snapshot.docs["Type:note.text@1.0"].spec.validators[0].fixtures;
  fixtures.reject = ["fixtures/note/ok.txt"];
  expect(contract.checkFixtureSet(old, snapshot, "note.text@1", "count-text")).toMatchObject({ verdict: "fail", detail: {
    code: "CONTRACT_FIXTURE_REMOVED", removed: ["fixtures/note/empty.txt"],
  } });
  const rejected = await contract.runValidator("note.text@1", "count-text", { timeoutS: 30 });
  expect(rejected).toMatchObject({ kind: "validator", verdict: "fail", detail: { code: "CONTRACT_FIXTURE_ACCEPTED" } });
  fixtures.reject = ["fixtures/note/empty.txt"];
  fixtures.accept.push("fixtures/note/fail.txt");
  expect(contract.checkFixtureSet(old, snapshot, "note.text@1", "count-text")).toMatchObject({ verdict: "pass", detail: { removed: [] } });
  expect(contract.get(first.id)).toEqual(first);
  expect(contract.list({ kind: "validator" }).map(({ id }) => id)).toEqual([first.id, rejected.id]);
}));

test("rejects direct and linked fixture paths outside the Registry before launch", () => withContract(async (contract) => {
  const { context, snapshot, provider } = contract.runner;
  const check = snapshot.docs["Type:note.text@1.0"].spec.validators[0];
  const directory = join(context.root, "fixtures");
  await Bun.write(join(directory, "local.txt"), "local");
  const isolated = new Snapshot(directory, snapshot.epoch, snapshot.docs, snapshot.result);
  const isolatedContract = new ContractRunner(new Runner(context, isolated, provider));
  check.fixtures = { accept: ["../outside.txt"] };
  await expect(isolatedContract.runValidator("note.text@1", "count-text", { timeoutS: 30 })).rejects.toMatchObject({ code: "CONTRACT_FIXTURE_PATH" });
  const nested = join(context.root, "outside");
  await Bun.write(join(nested, "outside.txt"), "hidden fixture");
  symlinkSync(nested, join(directory, "link"), process.platform === "win32" ? "junction" : "dir");
  check.fixtures = { accept: ["link/outside.txt"] };
  await expect(isolatedContract.runValidator("note.text@1", "count-text", { timeoutS: 30 })).rejects.toMatchObject({ code: "CONTRACT_FIXTURE_PATH" });
  expect(contract.list()).toEqual([]);
}));

test("executes GoldenSet jobs with caller timeouts and applies only declared Baseline thresholds", { timeout: 90_000 }, async () => {
  const timeouts: number[] = [];
  class RecordingProvider extends LocalProvider {
    override async open(spec: SandboxSpec) {
      const sandbox = await super.open(spec);
      const execute = sandbox.exec.bind(sandbox);
      sandbox.exec = (argv, options) => { timeouts.push(options.timeoutMs); return execute(argv, options); };
      return sandbox;
    }
  }
  await withContract(async (contract) => {
    const golden = "upper-golden@1";
    const baseline = "upper-baseline@1";
    const result = await contract.runGolden(golden, baseline, { timeoutS: 30 });
    expect(result).toMatchObject({ verdict: "pass", detail: {
      cases: [{ name: "ok", sealed: true }], observed: { cost_usd: 0, gate_reject_rate: 0 }, gates: [{ metric: "cost_usd", key: "max", ok: true }],
    } });
    const job = contract.runner.jobs.get((result.detail.cases as Array<{ job: string }>)[0].job);
    expect(job.timeoutMs).toBe(30_000);
    expect(timeouts.filter((value) => value === 30_000).length).toBeGreaterThanOrEqual(3);
    const body = contract.runner.snapshot.docs[`Baseline:${baseline}`].spec;
    body.metrics = { cost_usd: { max: -1 } };
    contract.runner.snapshot.docs[`GoldenSet:${golden}`].spec.cases[0].name = "token=example-credential";
    const exceeded = await contract.canary("upper@1", { timeoutS: 30 });
    expect(exceeded).toMatchObject({ verdict: "fail", detail: { code: "CONTRACT_BASELINE_EXCEEDED", cases: [{ name: "token=[REDACTED]" }] } });
    expect(JSON.stringify(contract.get(exceeded.id))).not.toContain("example-credential");
    body.metrics = { cost_usd: { p50: -1, mean: -1 } };
    expect(await contract.runGolden(golden, baseline, { timeoutS: 30 })).toMatchObject({ verdict: "pass", detail: { gates: [] } });
    body.metrics = { judge_score: { min: 1 } };
    expect(await contract.runGolden(golden, baseline, { timeoutS: 30 })).toMatchObject({ verdict: "fail", detail: { code: "CONTRACT_METRIC_MISSING", cases: [] } });
    delete contract.runner.snapshot.docs[`GoldenSet:${golden}`].spec.budget_usd;
    await expect(contract.runGolden(golden)).rejects.toMatchObject({ code: "CONTRACT_BUDGET_REQUIRED" });
  }, new RecordingProvider(commands));
});

test("stops scheduling GoldenSet jobs once their observed cost exceeds the written budget", { timeout: 60_000 }, () => withContract(async (contract) => {
  const { context, snapshot } = contract.runner;
  const file = join(context.root, "task.json");
  await Bun.write(file, JSON.stringify({ cost_usd: 0.1, default: [{ do: "write", path: "drafts/report.txt", text: "a\nb\n" }] }));
  const bean = await context.warehouse.add({ type: "agent.task@1.0", files: { "task.json": file } });
  context.warehouse.sealMany([[bean.id, { steps: 1 }]], null, `sha256:${"a".repeat(64)}`);
  snapshot.docs["GoldenSet:budget-cases@1"] = { spec: { station: "writer@1", budget_usd: 0.05, cases: [
    { name: "measured", inputs: { task: bean.id } }, { name: "not-scheduled", inputs: { task: bean.id } },
  ] } };
  const result = await contract.runGolden("budget-cases@1", undefined, { timeoutS: 30 });
  expect(result).toMatchObject({ verdict: "fail", detail: { code: "CONTRACT_BUDGET_EXCEEDED", cap: 0.05, spent: 0.1, cases: [{ name: "measured", sealed: true }] } });
  expect(context.db.query("SELECT COUNT(*) AS jobs FROM jobs").get()).toEqual({ jobs: 1 });
}, new LocalProvider({ ...commands, "/opt/operator/bin/fake-agent": [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/fake_agent.ts")] })));

test("checks converter equivalence and compares shared facts against the prior version", { timeout: 90_000 }, () => withContract(async (contract) => {
  const original = "note-to-text@1";
  await expect(contract.runEquivalence(original)).rejects.toMatchObject({ code: "CONTRACT_TIMEOUT_REQUIRED" });
  expect(await contract.runDiff(original)).toMatchObject({ verdict: "pass", detail: { reason: "no_previous" } });
  const options = { timeoutS: 30 };
  const before = await contract.runEquivalence(original, options);
  expect(before).toMatchObject({ verdict: "pass", detail: {
    accept: [{ path: "fixtures/note/ok.txt", sealed: true, failed_expr: [] }], reject: [{ path: "fixtures/note/empty.txt", sealed: false }],
  } });
  const { docs } = contract.runner.snapshot;
  docs[`Station:${original}`].spec.equivalence = ["false"];
  expect(await contract.runEquivalence(original, options)).toMatchObject({ verdict: "fail", detail: { code: "CONTRACT_EQUIVALENCE_FAILED" } });
  const updated = structuredClone(docs[`Station:${original}`]);
  updated.metadata.version = 2;
  updated.spec.recipe = "note-to-text-v2";
  docs["Station:note-to-text@2"] = updated;
  const recipes = docs["Machine:toolbox@1"].spec.recipes;
  recipes["note-to-text-v2"] = { ...recipes["note-to-text"], run: ["exit-three"] };
  expect(await contract.runDiff("note-to-text@2", options)).toMatchObject({ verdict: "fail", detail: { code: "CONTRACT_DIFF_REGRESSION" } });
  recipes["note-to-text-v2"].run = ["pull-notes", "/factory/in/in", "/factory/out/out/note.txt"];
  expect(await contract.runDiff("note-to-text@2", options)).toMatchObject({ verdict: "fail", detail: { code: "CONTRACT_DIFF_FACTS", fact: "has_lower" } });
}));

test("runs an automatically approved Sink proposal without publishing or creating a receipt", { timeout: 60_000 }, () => withContract(async (contract) => {
  const { context, jobs } = contract.runner;
  const input = join(context.root, "fixture.txt");
  await Bun.write(input, "hello\n");
  const bean = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": input } });
  context.warehouse.sealMany([[bean.id, { chars: 5, lines: 1, has_lower: true }]], null, `sha256:${"a".repeat(64)}`);
  const target = join(context.root, "published");
  const options = { inputs: { note: bean.id }, params: { dir: target }, timeoutS: 30 };
  await expect(contract.runSinkDryRun("publish-local@1", options)).rejects.toMatchObject({ code: "CONTRACT_SINK_NO_PROPOSE" });
  const result = await contract.runSinkDryRun("publish-staged@1", options);
  expect(result).toMatchObject({ kind: "sink_dry_run", verdict: "pass", detail: { approval: { status: "approved" }, receipt: null, executed_run: false } });
  expect(jobs.get(String(result.detail.job))).toMatchObject({ state: "SEALED", phase: "propose", proposalOnly: true });
  expect(context.db.query("SELECT id FROM receipts").all()).toEqual([]);
  expect(existsSync(target)).toBe(false);
}));

test("keeps an approved propose-only Sink isolated after the factory is reopened", { timeout: 60_000 }, () => withContract(async (contract) => {
  const runner = contract.runner;
  const { context, snapshot } = runner;
  snapshot.docs["Station:publish-staged@1"].spec.approval.propose.auto_if = "false";
  const input = join(context.root, "fixture.txt");
  await Bun.write(input, "hello\n");
  const bean = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": input } });
  context.warehouse.sealMany([[bean.id, { chars: 5, lines: 1, has_lower: true }]], null, `sha256:${"a".repeat(64)}`);
  const target = resolve(context.root, "published");
  const result = await contract.runSinkDryRun("publish-staged@1", { inputs: { note: bean.id }, params: { dir: target } });
  expect(result).toMatchObject({ verdict: "fail", detail: { approval: { status: "pending" }, receipt: null, executed_run: false } });
  const jobId = String(result.detail.job);
  const reopened = Context.open(context.root);
  try {
    const resumed = new Runner(reopened, snapshot, new LocalProvider(commands));
    const approval = resumed.approvals.proposal(jobId, 1)!;
    resumed.approvals.applyDecision(approval.id, "approve", { by: "test" });
    expect(() => resumed.jobs.transition(jobId, "PROVISIONING", { phase: "execute" })).toThrow("只可执行方案阶段");
    expect(await resumed.run(jobId)).toMatchObject({ state: "SEALED", phase: "propose", proposalOnly: true, outputs: {} });
    expect(await resumed.run(jobId)).toMatchObject({ state: "SEALED", phase: "propose" });
    expect(resumed.receipts.forJob(jobId)).toBeUndefined();
    expect(reopened.db.query("SELECT id FROM receipts").all()).toEqual([]);
    expect(existsSync(target)).toBe(false);
  } finally { reopened.close(); }
}));

test("runs a GoldenSet through factory contract and reads its saved report", { timeout: 60_000 }, () => withContract(async (contract) => {
  const { context } = contract.runner;
  const local = join(context.root, "commands.json");
  await Bun.write(local, JSON.stringify(commands));
  const cli = join(import.meta.dir, "../src/cli.ts");
  const registry = resolve("tests/fixtures/registry-m2");
  const argv = [Bun.argv[0], cli, "--root", context.root, "--registry", registry, "--provider", "local", "--local-commands", local, "contract"];
  const output = async (...args: string[]) => {
    const result = await runCommand([...argv, ...args]);
    const decoder = new TextDecoder();
    if (result.exitCode !== 0) throw new Error(`factory contract ${args[0]} failed: ${decoder.decode(result.stderr)}${decoder.decode(result.stdout)}`);
    return JSON.parse(decoder.decode(result.stdout));
  };
  const run = await output("golden", "upper-golden@1", "--baseline", "upper-baseline@1", "--timeout", "30");
  expect(run).toMatchObject({ kind: "golden", verdict: "pass", detail: { cases: [{ sealed: true }] } });
  expect(await output("show", run.id)).toEqual(run);
  expect(await output("ls", "--kind", "golden")).toEqual([run]);
}));
