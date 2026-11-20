import { expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { Context, CounterIds, FakeClock, LocalProvider, Runner, loadSnapshot } from "../src/legacy";
import { readReceiptPayload, ReceiptStore, renderReceiptPayload, sinkKey, verifyReceipt } from "../src/legacy/connectors";
import { runCommand } from "../runtime/podman";

const registry = "tests/fixtures/registry-m2";
const textType = "note.text@1.0";
const localCommands = Object.fromEntries(["count-text", "pull-notes", "publish-copy", "publish-flaky", "local-file-check"].map((tool) => [
  tool, [Bun.argv[0], join(import.meta.dir, `../../tests/fakes/tools/${tool.replaceAll("-", "_")}.ts`)],
]));

async function setup<T>(run: (context: Context) => Promise<T>): Promise<T> {
  const context = Context.open(join("tmp", `connectors-${randomUUID()}`), { clock: new FakeClock(), ids: new CounterIds() });
  try {
    return await run(context);
  } finally {
    // Bun leaves statements beyond the SQLite query cache to GC, which can hold the database open on Windows.
    (Bun as unknown as { gc(synchronous: boolean): void }).gc(true);
    context.close();
    rmSync(context.root, { recursive: true, force: true, maxRetries: 3 });
  }
}

function events(context: Context, job: string): string[] {
  return context.db.query<{ kind: string }>("SELECT kind FROM job_events WHERE job_id=? ORDER BY rowid").all(job).map(({ kind }) => kind);
}

async function sealNote(context: Context): Promise<string> {
  const path = join(context.root, "input.txt");
  await Bun.write(path, "hello\n");
  const bean = await context.warehouse.add({ type: textType, files: { "note.txt": path } });
  context.warehouse.sealMany([[bean.id, { chars: 5, lines: 1, has_lower: true }]], null, `sha256:${"a".repeat(64)}`);
  return bean.id;
}

test("Sink receipt claims reject an unresolved older version and release after voiding", async () => setup(async (context) => {
  const store = new ReceiptStore(context);
  const base = { station: "Station:publish-local@1", key: "shared", inputs: { note: ["bean-id"] }, verifyMethod: "local-file-check", jobId: "job-1" };
  const claim = store.claim(base);
  expect(claim.kind).toBe("new");
  const next = { ...base, station: "Station:publish-local@2", jobId: "job-2" };
  expect(() => store.claim(next)).toThrow(/幂等键已被占用/);
  expect(() => store.publish(claim.receipt.id, { target: "foo", locator: {}, items: [null] })).toThrow(/回执载荷不合法/);
  expect(store.forJob(base.jobId)?.status).toBe("claimed");
  store.publish(claim.receipt.id, { target: "foo", locator: {} });
  store.settle(claim.receipt.id, { ok: true, detail: "matched" });
  expect(store.claim(next)).toMatchObject({ kind: "duplicate", receipt: { id: claim.receipt.id } });
  expect(store.forBean("bean-id").map(({ id }) => id)).toEqual([claim.receipt.id]);
  store.settle(claim.receipt.id, { ok: false, detail: "missing" });
  expect(() => store.claim(next)).toThrow(/未通过核验/);
  store.void(claim.receipt.id, { by: "operator", reason: "redo" });
  expect(store.claim(next)).toMatchObject({ kind: "new", receipt: { station: next.station } });
  expect(store.list({ station: "publish-local@2", status: "claimed" })).toHaveLength(1);
  expect(events(context, "job-1")).toEqual(["sink.claimed", "sink.published", "sink.verified", "sink.verify_failed", "receipt.voided"]);
}));

test("Sink renders recipe fallback from inputs and parameters and rejects an invalid receipt file", async () => setup(async (context) => {
  const snapshot = await loadSnapshot(registry);
  const station = "publish-templated@1";
  const bean = context.warehouse.get(await sealNote(context));
  const machine = snapshot.spec("Machine:toolbox@1");
  const recipe = machine.recipes["publish-templated"];
  const params = { dir: "archive" };
  expect(sinkKey(snapshot, station, { note: bean }, params)).toBe(`${bean.id}:${params.dir}`);
  expect(renderReceiptPayload(recipe.receipt, { note: bean }, params)).toEqual({ target: "archive", locator: { path: "archive/note.txt" } });
  const ctl = join(context.root, "ctl");
  expect(await readReceiptPayload(ctl)).toBeUndefined();
  await Bun.write(join(ctl, "receipt.json"), JSON.stringify({ target: "archive", locator: { path: 45 } }));
  await expect(readReceiptPayload(ctl, "job-1")).rejects.toMatchObject({ code: "RECEIPT_INVALID", detail: { job: "job-1", problems: ["locator"] } });
}));

test("Source seals local input through Gate and records provenance for repeated content", { timeout: 90_000 }, async () => setup(async (context) => {
  const snapshot = await loadSnapshot(registry);
  const runner = new Runner(context, snapshot, new LocalProvider(localCommands));
  const station = "pull-notes@1";
  const from = join(context.root, "incoming");
  mkdirSync(from);
  const file = join(from, "note.txt");
  await Bun.write(file, "hello source\n");
  const produce = () => runner.run(runner.submit(station, {}, { from_dir: from }).id);
  const first = await produce();
  expect(first.state).toBe("SEALED");
  expect(first.outputs.note).toHaveLength(1);
  const event = context.db.query<{ data: string }>("SELECT data FROM job_events WHERE job_id=? AND kind='source.ingested'").get(first.id);
  expect(JSON.parse(event!.data)).toMatchObject({ external: true, station: `Station:${station}`, params: { from_dir: from }, outputs: first.outputs });
  expect((await produce()).outputs).toEqual(first.outputs);
  expect(() => runner.submit(station, { note: first.outputs.note[0] }, { from_dir: from })).toThrow();
  await Bun.write(file, "");
  const rejected = await produce();
  expect(rejected).toMatchObject({ state: "DEAD", outputs: {} });
  expect(events(context, rejected.id)).not.toContain("source.ingested");
  expect(context.warehouse.list({ sealedOnly: true })).toHaveLength(1);
}));

test("Sink verifies once, suppresses duplicate jobs and can be reaudited or voided", { timeout: 90_000 }, async () => setup(async (context) => {
  const snapshot = await loadSnapshot(registry);
  const provider = new LocalProvider(localCommands);
  const runner = new Runner(context, snapshot, provider);
  const store = new ReceiptStore(context);
  const bean = await sealNote(context);
  const dir = join(context.root, "publication");
  const submit = (version: number) => runner.submit(`publish-local@${version}`, { note: bean }, { dir });

  const job = await runner.run(submit(1).id);
  expect(job).toMatchObject({ state: "SEALED", outputs: {} });
  expect(await Bun.file(join(dir, "note.txt")).text()).toBe("hello\n");
  const receipt = store.forJob(job.id)!;
  expect(receipt).toMatchObject({ status: "verified", idempotency_key: `${bean}:${dir}`, target: dir });
  const same = await runner.run(submit(2).id);
  expect(same.state).toBe("SEALED");
  expect(existsSync(join(context.root, "jobs", same.id, "1"))).toBe(false);
  expect(events(context, same.id)).toContain("sink.duplicate");
  expect(store.forBean(bean)).toHaveLength(1);

  rmSync(join(dir, "note.txt"));
  expect(store.settle(receipt.id, await verifyReceipt(context, snapshot, provider, receipt)).status).toBe("failed");
  expect(await runner.run(submit(1).id)).toMatchObject({ state: "DEAD", error: { code: "RECEIPT_UNVERIFIED_EXISTS" } });
  store.void(receipt.id, { by: "human", reason: "target deleted" });
  expect((await runner.run(submit(2).id)).state).toBe("SEALED");
  expect(store.forBean(bean).map(({ status }) => status).sort()).toEqual(["verified", "void"]);
}));

test("Sink preserves the claim and prevents automatic retries after an uncertain result", { timeout: 90_000 }, async () => setup(async (context) => {
  const snapshot = await loadSnapshot(registry);
  const runner = new Runner(context, snapshot, new LocalProvider(localCommands));
  const note = await sealNote(context);
  const submit = () => runner.submit("publish-flaky@1", { note }, {});
  const failed = await runner.run(submit().id);
  expect(failed).toMatchObject({ state: "DEAD", attempt: 1, error: { code: "SINK_OUTCOME_UNKNOWN" } });
  expect(new ReceiptStore(context).forJob(failed.id)?.status).toBe("claimed");
  expect(await runner.run(submit().id)).toMatchObject({ state: "DEAD", error: { code: "IDEMPOTENCY_CONFLICT" } });
}));

test("factoryctl receipt writes a validated payload that preserves locator URLs and digests", { timeout: 30_000 }, async () => setup(async (context) => {
  const ctl = join(context.root, "ctl");
  const script = join(import.meta.dir, "../src/legacy/factoryctl.ts");
  const digest = "sha256:" + "a".repeat(64);
  const run = async (args: string[]) => (await runCommand([Bun.argv[0], script, "receipt", ...args], { env: { FACTORY_CTL: ctl } })).exitCode;
  expect(await run(["--locator", "path=missing"])).toBe(2);
  expect(await run(["--target", "archive", "--locator", "url=https://example.test/x?a=b", "--item", `bean=https://example.test/x@v1@${digest}`])).toBe(0);
  expect(await readReceiptPayload(ctl)).toMatchObject({
    target: "archive", locator: { url: "https://example.test/x?a=b" },
    items: [{ bean: "bean", locator: "https://example.test/x@v1", digest }],
  });
}));
