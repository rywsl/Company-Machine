import { expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { Context } from "../src/legacy/context";
import { request } from "../src/legacy/factoryctl";
import { CounterIds, FakeClock } from "../src/legacy/foundation";
import { Runner } from "../src/legacy/runner";
import { LocalProvider } from "../src/legacy/sandbox";
import { loadSnapshot } from "../src/legacy/snapshot";
import type { Sandbox, SandboxSpec } from "../runtime/podman";

const fixtureCommands = Object.fromEntries(["write-proposal", "count-text", "upper-text", "publish-copy", "local-file-check"].map((name) =>
  [name, [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools", name.replaceAll("-", "_") + ".ts")]]));

async function setup() {
  const ids = new CounterIds();
  const clock = new FakeClock();
  const root = join("tmp", "legacy-proposal-" + randomUUID());
  const snapshot = await loadSnapshot("tests/fixtures/registry-m2");
  let context = Context.open(root, { clock, ids });
  const input = join(context.root, "note.txt");
  await Bun.write(input, "hello\n");
  const bean = await context.warehouse.add({ type: "note.text@1.0", files: { "note.txt": input } });
  context.warehouse.sealMany([[bean.id, { chars: 5, lines: 1, has_lower: true }]], null, "sha256:" + "a".repeat(64));
  return {
    get context() { return context; }, snapshot, bean, clock,
    reopen() { context.close(); context = Context.open(root, { clock, ids }); },
    close() {
      (Bun as unknown as { gc(synchronous: boolean): void }).gc(true);
      context.close();
      rmSync(root, { recursive: true, force: true, maxRetries: 4, retryDelay: 75 });
    },
  };
}

test("Runner approves a scripted proposal and seals in the same attempt", { timeout: 60_000 }, async () => {
  const world = await setup();
  try {
    const runner = new Runner(world.context, world.snapshot, new LocalProvider(fixtureCommands));
    const job = runner.submit("quote@1", { note: world.bean.id }, {});
    expect(job.phase).toBe("propose");
    const sealed = await runner.run(job.id);
    expect(sealed).toMatchObject({ state: "SEALED", phase: "execute", attempt: 1 });
    expect(runner.approvals.proposal(job.id, 1)).toMatchObject({ phase: "propose", status: "approved", decidedBy: "auto" });
    expect(sealed.outputs.note).toHaveLength(1);
    const root = join(world.context.root, "jobs", job.id, "1");
    expect(existsSync(join(root, "propose", "logs", "stdout"))).toBe(true);
    expect(existsSync(join(root, "logs", "stdout"))).toBe(true);
  } finally { world.close(); }
});

test("Runner preserves pending Sink proposals without claiming or executing until a durable approval", { timeout: 90_000 }, async () => {
  const world = await setup();
  try {
    const spec = world.snapshot.docs["Station:publish-staged@1"].spec;
    spec.approval.propose = "foreman";
    const target = join(world.context.root, "published");
    let runner = new Runner(world.context, world.snapshot, new LocalProvider(fixtureCommands));
    const job = runner.submit("publish-staged@1", { note: world.bean.id }, { dir: target });
    const pending = await runner.run(job.id);
    expect(pending).toMatchObject({ state: "AWAITING_APPROVAL", phase: "propose", attempt: 1 });
    expect(runner.receipts.forJob(job.id)).toBeUndefined();
    expect(existsSync(join(target, "note.txt"))).toBe(false);
    const proposal = runner.approvals.proposal(job.id, 1);
    if (!proposal) throw new Error("Missing proposal");
    world.reopen();
    runner = new Runner(world.context, world.snapshot, new LocalProvider(fixtureCommands));
    expect(await runner.run(job.id)).toMatchObject({ state: "AWAITING_APPROVAL", phase: "propose", attempt: 1 });
    runner.approvals.applyDecision(proposal.id, "approve", { by: "operator" });
    expect(await runner.run(job.id)).toMatchObject({ state: "SEALED", phase: "execute", attempt: 1 });
    expect(await Bun.file(join(target, "note.txt")).text()).toBe("hello\n");
    expect(runner.receipts.forJob(job.id)?.status).toBe("verified");
    expect(runner.approvals.list({ jobId: job.id, attempt: 1 })).toHaveLength(1);
  } finally { world.close(); }
});

test("Runner ends a denied Sink proposal without producing or claiming a receipt", { timeout: 60_000 }, async () => {
  const world = await setup();
  try {
    world.snapshot.docs["Station:publish-staged@1"].spec.approval.propose = "foreman";
    const target = join(world.context.root, "denied");
    const runner = new Runner(world.context, world.snapshot, new LocalProvider(fixtureCommands));
    const job = runner.submit("publish-staged@1", { note: world.bean.id }, { dir: target });
    await runner.run(job.id);
    const proposal = runner.approvals.proposal(job.id, 1);
    if (!proposal) throw new Error("Missing proposal");
    runner.approvals.applyDecision(proposal.id, "deny", { by: "operator" });
    expect(await runner.run(job.id)).toMatchObject({
      state: "DEAD", phase: "propose", attempt: 1, error: { code: "APPROVAL_DENIED" },
    });
    expect(runner.receipts.forJob(job.id)).toBeUndefined();
    expect(existsSync(target)).toBe(false);
  } finally { world.close(); }
});

test("delivers in-run review denial through the mailbox and returns the Job to RUNNING", { timeout: 30_000 }, async () => {
  const world = await setup();
  try {
    const spec = world.snapshot.docs["Station:upper@1"].spec;
    const payload = { kind: "expense", summary: "Fixture", cost_usd: 3 };
    let runner: Runner;
    let jobId: string;
    let expectedCode: string;
    class MailboxProvider extends LocalProvider {
      async open(sandbox: SandboxSpec): Promise<Sandbox> {
        if (sandbox.mounts.some(({ target }) => target === "/bean")) return super.open(sandbox);
        const ctl = sandbox.mounts.find(({ target }) => target === "/factory/ctl")!.source;
        const output = sandbox.mounts.find(({ target }) => target === "/factory/out")!.source;
        return {
          name: sandbox.name,
          async exec() {
            const asking = request("approval", payload, { ctl, timeoutMs: 10_000, pollIntervalMs: 10 });
            const decide = async () => {
              if (expectedCode !== "APPROVAL_REVIEW_SPLIT") return;
              for (let attempt = 0; attempt < 500 && runner.jobs.get(jobId).state !== "AWAITING_APPROVAL"; attempt++) {
                await new Promise((done) => setTimeout(done, 10));
              }
              expect(runner.jobs.get(jobId).state).toBe("AWAITING_APPROVAL");
              const [review] = runner.approvals.list({ jobId, status: "pending" });
              runner.approvals.applyDecision(review.id, "approve", { by: "runtime:first@1" });
              expect(runner.jobs.get(jobId).state).toBe("AWAITING_APPROVAL");
              runner.approvals.applyDecision(review.id, "deny", { by: "runtime:second@1" });
            };
            const [response] = await Promise.all([asking, decide()]);
            expect(response).toMatchObject({ ok: false, result: { decision: "deny" }, error: { code: expectedCode } });
            expect(runner.jobs.get(jobId).state).toBe("RUNNING");
            mkdirSync(join(output, "note"), { recursive: true });
            await Bun.write(join(output, "note", "note.txt"), "HELLO");
            return { exitCode: 0, timedOut: false, durationMs: 1 };
          },
          async close() {},
        };
      }
    }
    runner = new Runner(world.context, world.snapshot, new MailboxProvider(fixtureCommands));
    const run = async (code: string, parked: number) => {
      expectedCode = code;
      jobId = runner.submit("upper@1", { note: world.bean.id }, {}).id;
      expect(await runner.run(jobId)).toMatchObject({ state: "SEALED", attempt: 1 });
      expect(runner.jobs.countTransitions(jobId, "AWAITING_APPROVAL")).toBe(parked);
      expect(runner.approvals.list({ jobId, status: "pending" })).toEqual([]);
    };
    await run("APPROVAL_RULE_MISSING", 0);
    spec.approval = { in_run: "review" };
    await run("REVIEW_QUORUM_UNSET", 0);
    world.snapshot.result.registry.constitution[0].source += '\nlimit("approval_review_quorum", 2.0).\n';
    await run("APPROVAL_REVIEW_SPLIT", 1);
  } finally { world.close(); }
});

test("resumes an expired proposal as an approval timeout and leaves no-deadline proposals waiting", { timeout: 30_000 }, async () => {
  const world = await setup();
  try {
    const spec = world.snapshot.docs["Station:publish-staged@1"].spec;
    spec.approval = { propose: "foreman", timeout: "1s" };
    const runner = new Runner(world.context, world.snapshot, new LocalProvider(fixtureCommands));
    const submit = () => runner.submit("publish-staged@1", { note: world.bean.id }, { dir: join(world.context.root, "unpublished") });
    const expired = submit();
    expect(await runner.run(expired.id)).toMatchObject({ state: "AWAITING_APPROVAL", attempt: 1 });
    const proposal = runner.approvals.proposal(expired.id, 1)!;
    expect(Date.parse(proposal.deadline!) - Date.parse(proposal.createdAt)).toBe(1_000);
    world.clock.advance(1);
    expect(runner.approvals.expireDue()).toEqual([proposal.id]);
    expect(await runner.run(expired.id)).toMatchObject({ state: "DEAD", attempt: 1, error: { code: "APPROVAL_TIMEOUT" } });
    expect(runner.jobs.countTransitions(expired.id, "FAILED")).toBe(1);
    expect(runner.receipts.forJob(expired.id)).toBeUndefined();

    delete spec.approval.timeout;
    const waiting = submit();
    expect(await runner.run(waiting.id)).toMatchObject({ state: "AWAITING_APPROVAL" });
    expect(runner.approvals.proposal(waiting.id, 1)?.deadline).toBeNull();
    world.clock.advance(7_200);
    expect(runner.approvals.expireDue()).toEqual([]);
    expect(await runner.run(waiting.id)).toMatchObject({ state: "AWAITING_APPROVAL", attempt: 1 });
    expect(runner.receipts.forJob(waiting.id)).toBeUndefined();
  } finally { world.close(); }
});
