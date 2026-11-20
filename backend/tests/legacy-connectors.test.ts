import { expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { cpSync, existsSync, mkdirSync, rmSync, statSync } from "node:fs";
import { join } from "node:path";
import { Context, CounterIds, FakeClock, LocalProvider, Runner, Snapshot, loadSnapshot } from "../src/legacy";
import { readReceiptPayload, ReceiptStore, renderReceiptPayload, sinkKey, verifyReceipt } from "../src/legacy/connectors";
import { startEgressForwarder } from "../src/legacy/egress-forwarder";
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

function sinkEvents(context: Context, jobId: string): string[] {
  return events(context, jobId).filter((kind) => kind.startsWith("sink.") || kind.startsWith("receipt."));
}

async function sealNote(context: Context): Promise<string> {
  const path = join(context.root, "input.txt");
  await Bun.write(path, "hello\n");
  const bean = await context.warehouse.add({ type: textType, files: { "note.txt": path } });
  context.warehouse.sealMany([[bean.id, { chars: 5, lines: 1, has_lower: true }]], null, `sha256:${"a".repeat(64)}`);
  return bean.id;
}

async function sinkFixture(context: Context): Promise<string> {
  const fixture = join(context.root, "sink-fixture.ts");
  await Bun.write(fixture, `
import { createHash } from "node:crypto";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";

const [mode, source, dir] = Bun.argv.slice(2);
const ctl = process.env.FACTORY_CTL!;
const factoryctl = process.env.FACTORYCTL!;
if (mode === "timeout") {
  await Bun.sleep(30_000);
  process.exit(0);
}
if (mode === "refused") {
  const result = Bun.spawnSync([Bun.argv[0], factoryctl, "fail", "remote refused"]);
  process.exit(result.exitCode);
}
await mkdir(dir, { recursive: true });
const target = join(dir, "note.txt");
const bytes = await Bun.file(source).bytes();
await Bun.write(target, bytes);
if (mode === "broken") {
  await Bun.write(join(ctl, "receipt.json"), JSON.stringify({ locator: { path: target } }));
}
if (mode === "factoryctl") {
  const job = await Bun.file(join(ctl, "job.json")).json();
  const digest = "sha256:" + createHash("sha256").update(bytes).digest("hex");
  const result = Bun.spawnSync([Bun.argv[0], factoryctl, "receipt", "--target", dir, "--locator", "path=" + target,
    "--item", job.inputs.note.id + "=" + target + "@" + digest]);
  process.exit(result.exitCode);
}
`);
  return fixture;
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
  expect(store.publish(claim.receipt.id, { target: "foo", locator: {} }).status).toBe("published");
  expect(store.settle(claim.receipt.id, { ok: true, detail: "matched" }).status).toBe("verified");
  expect(store.claim(next)).toMatchObject({ kind: "duplicate", receipt: { id: claim.receipt.id } });
  expect(store.forBean("bean-id").map(({ id }) => id)).toEqual([claim.receipt.id]);
  store.settle(claim.receipt.id, { ok: false, detail: "missing" });
  expect(() => store.claim(next)).toThrow(/未通过核验/);
  store.void(claim.receipt.id, { by: "operator", reason: "redo" });
  expect(store.claim(next)).toMatchObject({ kind: "new", receipt: { station: next.station } });
  expect(store.list({ station: "publish-local@2", status: "claimed" })).toHaveLength(1);
  expect(events(context, "job-1")).toEqual(["sink.claimed", "sink.published", "sink.verified", "sink.verify_failed", "receipt.voided"]);
  const unresolved = store.claim({ ...base, key: "unresolved", jobId: "job-4" }).receipt;
  expect(store.void(unresolved.id, { by: "operator", reason: "confirmed unpublished" }).status).toBe("void");
  expect(store.claim({ ...base, key: "unresolved", jobId: "job-5" })).toMatchObject({ kind: "new", receipt: { status: "claimed" } });
}));

test("Sink renders recipe fallback from inputs and parameters and rejects an invalid receipt file", async () => setup(async (context) => {
  const snapshot = await loadSnapshot(registry);
  const station = "publish-templated@1";
  const bean = context.warehouse.get(await sealNote(context));
  const machine = snapshot.spec("Machine:toolbox@1");
  const recipe = machine.recipes["publish-templated"];
  const params = { dir: "archive" };
  expect(sinkKey(snapshot, station, { note: bean }, params)).toBe(`${bean.id}:${params.dir}`);
  const payload = renderReceiptPayload(recipe.receipt, { note: bean }, params);
  expect(payload).toEqual({ target: "archive", locator: { path: "archive/note.txt" } });
  const store = new ReceiptStore(context);
  const { receipt } = store.claim({ station: "Station:publish-templated@1", key: "template", inputs: {}, jobId: "job-1", verifyMethod: "local-file-check" });
  let missingTarget: unknown;
  try { store.publish(receipt.id, { locator: {} }); } catch (error) { missingTarget = error; }
  expect(missingTarget).toMatchObject({ code: "RECEIPT_INVALID", detail: { job: "job-1", problems: ["target"] } });
  expect(store.get(receipt.id).status).toBe("claimed");
  expect(store.publish(receipt.id, payload)).toMatchObject({ status: "published", target: "archive", locator: { path: "archive/note.txt" } });
  const ctl = join(context.root, "ctl");
  expect(await readReceiptPayload(ctl)).toBeUndefined();
  await Bun.write(join(ctl, "receipt.json"), JSON.stringify({ target: "archive", locator: { path: 45 } }));
  await expect(readReceiptPayload(ctl, "job-1")).rejects.toMatchObject({ code: "RECEIPT_INVALID", detail: { job: "job-1", problems: ["locator"] } });
  for (const value of ["{", "[]"]) {
    await Bun.write(join(ctl, "receipt.json"), value);
    await expect(readReceiptPayload(ctl, "job-1")).rejects.toMatchObject({ code: "RECEIPT_INVALID", detail: { job: "job-1", problems: ["json"] } });
  }
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
  expect(context.warehouse.get(first.outputs.note[0]).sealed).toBe(true);
  const event = context.db.query<{ data: string }>("SELECT data FROM job_events WHERE job_id=? AND kind='source.ingested'").get(first.id);
  expect(JSON.parse(event!.data)).toMatchObject({ external: true, station: `Station:${station}`, params: { from_dir: from }, outputs: first.outputs });
  const sealed = context.db.query<{ data: string }>("SELECT data FROM job_events WHERE job_id=? AND kind='bean.sealed'").get(first.id);
  expect(JSON.parse(sealed!.data)).toMatchObject({ external: true, bean: first.outputs.note[0] });
  expect((await produce()).outputs).toEqual(first.outputs);
  let invalidInput: unknown;
  try { runner.submit(station, { note: first.outputs.note[0] }, {}); } catch (error) { invalidInput = error; }
  expect(invalidInput).toMatchObject({ code: "INTAKE_REJECTED" });
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
  expect(job).toMatchObject({ state: "SEALED", attempt: 1, outputs: {} });
  const path = join(dir, "note.txt");
  expect(await Bun.file(path).text()).toBe("hello\n");
  const publishedAt = statSync(path).mtimeMs;
  const receipt = store.forJob(job.id)!;
  expect(receipt).toMatchObject({
    status: "verified", idempotency_key: `${bean}:${dir}`, target: dir,
    locator: { path }, verify_method: "local-file-check",
  });
  expect(receipt.verified_at).toBeTruthy();
  expect(sinkEvents(context, job.id)).toEqual(["sink.claimed", "sink.published", "sink.verified"]);
  const same = await runner.run(submit(2).id);
  expect(same).toMatchObject({ state: "SEALED", outputs: {} });
  expect(existsSync(join(context.root, "jobs", same.id, "1"))).toBe(false);
  expect(sinkEvents(context, same.id)).toEqual(["sink.duplicate"]);
  expect(statSync(path).mtimeMs).toBe(publishedAt);
  expect(store.forBean(bean)).toHaveLength(1);
  const cli = join(import.meta.dir, "../src/cli.ts");
  const downstream = await runCommand([Bun.argv[0], cli, "--root", context.root, "bean", "lineage", bean, "--down"]);
  expect(downstream.exitCode).toBe(0);
  expect(JSON.parse(new TextDecoder().decode(downstream.stdout))).toMatchObject({ id: bean, direction: "down", receipts: [receipt.id] });

  rmSync(join(dir, "note.txt"));
  expect(store.settle(receipt.id, await verifyReceipt(context, snapshot, provider, receipt)).status).toBe("failed");
  expect(events(context, job.id)).toContain("sink.verify_failed");
  expect(await runner.run(submit(1).id)).toMatchObject({ state: "DEAD", error: { code: "RECEIPT_UNVERIFIED_EXISTS" } });
  store.void(receipt.id, { by: "human", reason: "target deleted" });
  expect((await runner.run(submit(2).id)).state).toBe("SEALED");
  expect(store.forBean(bean).map(({ status }) => status).sort()).toEqual(["verified", "void"]);
}));

test("reruns a Line successfully without executing the already verified Sink again", { timeout: 90_000 }, async () => setup(async (context) => {
  const fixtures = join(context.root, "registry");
  cpSync(registry, fixtures, { recursive: true });
  const dir = join(context.root, "published");
  await Bun.write(join(fixtures, "lines", "note-publish.yaml"), JSON.stringify({
    apiVersion: "mao/v1", kind: "Line", metadata: { name: "note-publish", version: 1 },
    spec: { interface: { inputs: { note: { type: "note.text@1" } } }, budget: { cap_usd: 0 },
      steps: { publish: { station: "publish-local@1", in: { note: "$inputs.note" }, params: { dir } } } },
  }));
  const commands = join(context.root, "commands.json");
  await Bun.write(commands, JSON.stringify(localCommands));
  const cli = join(import.meta.dir, "../src/cli.ts");
  const note = await sealNote(context);
  const run = async (...args: string[]) => {
    const result = await runCommand([Bun.argv[0], cli, "--root", context.root, "--registry", fixtures,
      "--provider", "local", "--local-commands", commands, "--json", ...args]);
    if (result.exitCode !== 0) throw new Error(new TextDecoder().decode(result.stderr));
    return JSON.parse(new TextDecoder().decode(result.stdout));
  };
  const line = ["line", "run", "note-publish@1", "--in", `note=${note}`];
  const first = await run(...line);
  const second = await run(...line);
  expect([first.state, second.state]).toEqual(["SUCCEEDED", "SUCCEEDED"]);
  const [published] = await run("line", "nodes", first.id);
  const [duplicate] = await run("line", "nodes", second.id);
  expect(duplicate).toMatchObject({ nodeId: "publish", state: "DONE" });
  const verified = await run("job", "events", published.jobId, "--kind", "sink.");
  const duplicates = await run("job", "events", duplicate.jobId, "--kind", "sink.");
  expect(verified.map(({ kind }: { kind: string }) => kind)).toEqual(["sink.claimed", "sink.published", "sink.verified"]);
  expect(duplicates.map(({ kind }: { kind: string }) => kind)).toEqual(["sink.duplicate"]);
  expect(new ReceiptStore(context).list()).toMatchObject([{ job_id: published.jobId, status: "verified" }]);
}));

test("publishes a templated receipt when the Sink leaves no receipt file", { timeout: 30_000 }, async () => setup(async (context) => {
  const snapshot = await loadSnapshot(registry);
  const provider = new LocalProvider({ ...localCommands, "publish-copy": [Bun.argv[0], await sinkFixture(context), "template"] });
  const runner = new Runner(context, snapshot, provider);
  const note = await sealNote(context);
  const dir = join(context.root, "template");
  const job = await runner.run(runner.submit("publish-templated@1", { note }, { dir }).id);
  expect(job).toMatchObject({ state: "SEALED", attempt: 1, outputs: {} });
  expect(existsSync(join(context.root, "jobs", job.id, "1", "ctl", "receipt.json"))).toBe(false);
  expect(runner.receipts.forJob(job.id)).toMatchObject({ status: "verified", target: dir, locator: { path: dir + "/note.txt" }, items: [] });
}));

test("retains the claim after missing and malformed Sink receipts", { timeout: 30_000 }, async () => setup(async (context) => {
  const snapshot = await loadSnapshot(registry);
  const fixture = await sinkFixture(context);
  const note = await sealNote(context);
  for (const { mode, code, problems, verifyDetail } of [
    { mode: "missing", code: "RECEIPT_MISSING", problems: undefined, verifyDetail: null },
    { mode: "broken", code: "RECEIPT_INVALID", problems: ["target"], verifyDetail: "receipt.json 格式错误：target" },
  ]) {
    const runner = new Runner(context, snapshot, new LocalProvider({ ...localCommands, "publish-copy": [Bun.argv[0], fixture, mode] }));
    const dir = join(context.root, mode);
    const submit = () => runner.submit("publish-local@1", { note }, { dir });
    const job = await runner.run(submit().id);
    expect(job).toMatchObject({ state: "DEAD", attempt: 1, error: { code, detail: { job: job.id, ...(problems && { problems }) } } });
    expect(runner.receipts.forJob(job.id)).toMatchObject({ status: "claimed", verify_detail: verifyDetail });
    expect(sinkEvents(context, job.id)).toEqual(["sink.claimed"]);
    expect(await runner.run(submit().id)).toMatchObject({ state: "DEAD", error: { code: "IDEMPOTENCY_CONFLICT" } });
  }
}));

test("records verification refusal while preserving published evidence on facility failure", { timeout: 30_000 }, async () => setup(async (context) => {
  const snapshot = await loadSnapshot(registry);
  const note = await sealNote(context);
  for (const { exitCode, receiptStatus, jobError, verifyDetail } of [
    { exitCode: 1, receiptStatus: "failed", jobError: "RECEIPT_UNVERIFIED", verifyDetail: "not visible" },
    { exitCode: 2, receiptStatus: "published", jobError: "RECEIPT_VERIFY_ERROR", verifyDetail: null },
  ]) {
    const provider = new LocalProvider({
      ...localCommands, "local-file-check": [Bun.argv[0], "-e", `console.log('{"detail":"not visible"}'); process.exit(${exitCode});`],
    });
    const runner = new Runner(context, snapshot, provider);
    const dir = join(context.root, "publish-" + exitCode);
    const job = await runner.run(runner.submit("publish-local@1", { note }, { dir }).id);
    const receipt = runner.receipts.forJob(job.id)!;
    expect(receipt).toMatchObject({ status: receiptStatus, verify_detail: verifyDetail });
    expect(job).toMatchObject({ state: "DEAD", attempt: 1, error: { code: jobError, detail: { receipt: receipt.id,
      ...(exitCode === 1 ? { detail: verifyDetail } : { cause: { exit_code: 2, timed_out: false } }),
    } } });
    expect(sinkEvents(context, job.id)).toEqual(["sink.claimed", "sink.published", ...(exitCode === 1 ? ["sink.verify_failed"] : [])]);
    expect(await Bun.file(join(dir, "note.txt")).text()).toBe("hello\n");
  }
}));

test("keeps a Sink claim unverified on timeout or an explicit factoryctl refusal", { timeout: 30_000 }, async () => setup(async (context) => {
  const snapshot = await loadSnapshot(registry);
  const fixture = await sinkFixture(context);
  const note = await sealNote(context);
  const station = snapshot.docs["Station:publish-local@1"].spec;
  station.limits.retries.failed = 2;
  for (const { mode, cause, timeoutMs } of [
    { mode: "timeout", cause: { timeout_s: 0.25 }, timeoutMs: 250 },
    { mode: "refused", cause: { reason: "remote refused" }, timeoutMs: undefined },
  ]) {
    const provider = new LocalProvider({ ...localCommands, "publish-copy": [Bun.argv[0], fixture, mode] });
    const runner = new Runner(context, snapshot, provider);
    const dir = join(context.root, mode);
    const job = await runner.run(runner.submit("publish-local@1", { note }, { dir }, undefined, { timeoutMs }).id);
    expect(job).toMatchObject({ state: "DEAD", attempt: 1, error: { code: "SINK_OUTCOME_UNKNOWN", detail: { job: job.id, cause } } });
    expect(runner.receipts.forJob(job.id)?.status).toBe("claimed");
    expect(sinkEvents(context, job.id)).toEqual(["sink.claimed"]);
  }
}));

test("rejects service Source execution and incomplete Source Run context before launching", async () => setup(async (context) => {
  const snapshot = await loadSnapshot(registry);
  let launches = 0;
  class RecordingProvider extends LocalProvider {
    override async open(spec: Parameters<LocalProvider["open"]>[0]) {
      launches += 1;
      return super.open(spec);
    }
  }
  const runner = new Runner(context, snapshot, new RecordingProvider(localCommands));
  let policyError: unknown;
  try { runner.submit("pull-notes@1", {}, { from_dir: context.root }, { id: "x" }); } catch (error) { policyError = error; }
  expect(policyError).toMatchObject({ code: "POLICY_VIOLATION", detail: { station: "Station:pull-notes@1", role: "source", reason: "run" } });
  expect(context.db.query("SELECT id FROM jobs").all()).toEqual([]);

  const service = runner.submit("service-source@1", {}, {});
  await expect(runner.run(service.id)).rejects.toMatchObject({
    code: "FORM_UNSUPPORTED", detail: { station: "Station:service-source@1", machine: "Machine:servicebox@1", form: "service" },
  });
  expect(runner.jobs.get(service.id)).toMatchObject({ state: "PENDING", attempt: service.attempt });
  expect(existsSync(join(context.root, "jobs", service.id))).toBe(false);
  expect(launches).toBe(0);
}));

test("uses independent egress listeners for Sink publication and receipt verification", { timeout: 30_000 }, async () => setup(async (context) => {
  const base = await loadSnapshot(registry);
  const station = "Station:publish-local@1";
  const docs = structuredClone(base.docs);
  const policy = base.key("Policy", base.spec(station).policy);
  docs[policy].spec.egress = { publish: ["archive-profile"] };
  mkdirSync(join(context.root, "egress"));
  await Bun.write(join(context.root, "egress", "profiles.json"), JSON.stringify({ "archive-profile": ["archive.example"] }));
  const snapshot = new Snapshot(base.root, base.epoch, docs, base.result);
  const identities = new Set<string>();
  let closed = 0;
  const provider = new LocalProvider(localCommands, async (plan) => {
    expect(plan).toMatchObject({ station, policy, allow: [{ host: "archive.example", tier: "publish" }] });
    const listener = await startEgressForwarder({ plan, resolve: async () => { throw new Error("unexpected fixture DNS"); } });
    identities.add(listener.authorization);
    return { ...listener, async close() { await listener.close(); closed++; } };
  });
  const runner = new Runner(context, snapshot, provider);
  const note = await sealNote(context);
  const job = await runner.run(runner.submit(station, { note }, { dir: join(context.root, "publication") }).id);
  expect(job.state).toBe("SEALED");
  expect(new ReceiptStore(context).forJob(job.id)?.status).toBe("verified");
  expect(identities.size).toBe(2);
  expect(closed).toBe(2);
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
  expect(await run(["--target", "archive", "--locator", "no-equals"])).toBe(2);
  expect(await readReceiptPayload(ctl)).toBeUndefined();
  expect(await run(["--target", "archive", "--locator", "url=https://example.test/x?a=b", "--item", `bean=https://example.test/x@v1@${digest}`])).toBe(0);
  expect(await readReceiptPayload(ctl)).toMatchObject({
    target: "archive", locator: { url: "https://example.test/x?a=b" },
    items: [{ bean: "bean", locator: "https://example.test/x@v1", digest }],
  });
}));

test("publishes and verifies a Sink receipt written by installed factoryctl", { timeout: 30_000 }, async () => setup(async (context) => {
  const snapshot = await loadSnapshot(registry);
  const fixture = await sinkFixture(context);
  const runner = new Runner(context, snapshot, new LocalProvider({ ...localCommands, "publish-copy": [Bun.argv[0], fixture, "factoryctl"] }));
  const note = await sealNote(context);
  const dir = join(context.root, "factoryctl-publication");
  const path = join(dir, "note.txt");
  const job = await runner.run(runner.submit("publish-local@1", { note }, { dir }).id);
  expect(job).toMatchObject({ state: "SEALED", attempt: 1 });
  expect(runner.receipts.forJob(job.id)).toMatchObject({
    status: "verified", target: dir, locator: { path },
    items: [{ bean: note, locator: path, digest: context.warehouse.get(note).payload[0].digest }],
  });
}));
