import { expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { existsSync, rmSync } from "node:fs";
import { join } from "node:path";
import { Context } from "../src/legacy/context";
import { CounterIds, FakeClock } from "../src/legacy/foundation";
import { Runner } from "../src/legacy/runner";
import { LocalProvider } from "../src/legacy/sandbox";
import { loadSnapshot } from "../src/legacy/snapshot";

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
