import { expect, test } from "bun:test";
import { cpSync, existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createBackup, restoreBackup } from "../src/backup";
import { main } from "../src/cli";
import { runEpochCli } from "../src/epoch-cli";
import { Context } from "../src/legacy/context";
import { FakeClock } from "../src/legacy/foundation";
import { EpochStore } from "../src/legacy/registry-epochs";

const gitTest = { timeout: 30_000 };

async function git(root: string, ...args: string[]): Promise<string> {
  const bun = Bun as unknown as { spawn(argv: string[], options: object): { stdout: ReadableStream; stderr: ReadableStream; exited: Promise<number> } };
  const process = bun.spawn(["git", "-C", root, "-c", "user.name=Acceptance", "-c", "user.email=acceptance@example.invalid", "-c", "commit.gpgsign=false", ...args], {
    stdout: "pipe", stderr: "pipe",
  });
  const [status, output, error] = await Promise.all([process.exited, new Response(process.stdout).text(), new Response(process.stderr).text()]);
  if (status !== 0) throw new Error(`${args[0]} failed: ${error}`);
  return output.trim();
}

async function world() {
  const root = mkdtempSync(join(tmpdir(), "mao-git-epoch-"));
  const context = Context.open(root, { clock: new FakeClock() });
  try {
    const registry = join(root, "registry");
    cpSync("tests/fixtures/registry-m2", registry, { recursive: true });
    await git(registry, "init", "--initial-branch=main", "--template=");
    await git(registry, "add", ".");
    await git(registry, "commit", "-m", "Initial Registry");
    const base = await git(registry, "rev-parse", "HEAD");
    writeFileSync(join(root, "factory.json"), JSON.stringify({ governance: { registry: "registry" } }));
    return { root, context, registry, base, epochs: await EpochStore.open(context), close() {
      context.close();
      rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 80 });
    } };
  } catch (error) {
    context.close();
    rmSync(root, { recursive: true, force: true });
    throw error;
  }
}

async function commitChange(registry: string, path: string, text: string): Promise<string> {
  writeFileSync(join(registry, path), text);
  await git(registry, "add", "--", path);
  await git(registry, "commit", "-m", `Change ${path}`);
  return git(registry, "rev-parse", "HEAD");
}

test("initializes the active Git epoch in SQLite and refs/factory exactly once", gitTest, async () => {
  const w = await world();
  try {
    expect(w.base).toMatch(/^[0-9a-f]{40}$/);
    expect(await w.epochs.init(w.base.slice(0, 12), "human:acceptance")).toBe(w.base);
    expect(w.epochs.pointers()).toEqual({ active: w.base, candidate: null, previous: null });
    expect(w.epochs.current()).toBe(w.base);
    expect(await git(w.registry, "rev-parse", "refs/factory/active")).toBe(w.base);
    expect((await w.epochs.materialize(w.base)).epoch).toBe(w.base);
    expect(w.context.db.query('SELECT "commit" FROM epoch_pointers WHERE name = ?').get("active")).toEqual({ commit: w.base });
    await expect(w.epochs.init(w.base, "human:acceptance")).rejects.toMatchObject({ code: "EPOCH_STATE_INVALID" });
  } finally { w.close(); }
});

test("freezes a committed Registry and its nonempty resolutions once", gitTest, async () => {
  const w = await world();
  try {
    const station = "stations/upper.yaml";
    writeFileSync(join(w.registry, station), "uncommitted: [");
    const built = await w.epochs.build(w.base);
    expect(built.tree).toBe(await git(w.registry, "rev-parse", `${w.base}^{tree}`));
    const epochRoot = join(w.root, "epochs", w.base);
    const frozen = join(epochRoot, "resolutions.json");
    expect(readFileSync(join(epochRoot, "registry", station), "utf8")).toContain("timeout: 60s");
    const loaded = await w.epochs.materialize(w.base);
    expect(loaded.epoch).toBe(w.base);
    const expected = loaded.result.resolutions.map((item) => ({ ...item, origin: "precomputed" }));
    expect(expected.length).toBeGreaterThan(0);
    expect(JSON.parse(readFileSync(frozen, "utf8"))).toEqual(expected);
    writeFileSync(frozen, "[]");
    const reopened = await EpochStore.open(w.context);
    expect((await reopened.build(w.base)).commit).toBe(w.base);
    expect(readFileSync(frozen, "utf8")).toBe("[]");
    expect((await reopened.materialize(w.base)).spec("Station:upper@1").limits.timeout).toBe("60s");
    expect(readFileSync(join(w.registry, station), "utf8")).toBe("uncommitted: [");
    expect(w.context.db.query("SELECT kind FROM events ORDER BY seq").all()).toEqual([{ kind: "epoch.built" }]);
  } finally { w.close(); }
});

test("rejects a malformed committed Registry without publishing an epoch or stage", gitTest, async () => {
  const w = await world();
  try {
    await w.epochs.init(w.base, "human:acceptance");
    const bad = await commitChange(w.registry, "stations/upper.yaml", "invalid: [");
    await expect(w.epochs.build(bad)).rejects.toMatchObject({ code: "REGISTRY_INVALID" });
    expect(w.context.db.query('SELECT "commit" FROM epochs WHERE "commit" = ?').get(bad)).toBeNull();
    expect(existsSync(join(w.root, "epochs", bad))).toBe(false);
    expect(readdirSync(join(w.root, "epochs"))).toEqual([w.base]);
    expect(w.epochs.current()).toBe(w.base);
  } finally { w.close(); }
});

test("retains activation pointers and unfinished Run, Job and ChangeSet dependencies", gitTest, async () => {
  const w = await world();
  try {
    await w.epochs.init(w.base, "human:acceptance");
    const second = await commitChange(w.registry, "README.txt", "second");
    const third = await commitChange(w.registry, "README.txt", "third");
    await w.epochs.build(second);
    await w.epochs.build(third);
    w.context.db.exec(`
      CREATE TABLE runs (epoch TEXT, state TEXT);
      CREATE TABLE jobs (epoch TEXT, state TEXT);
      CREATE TABLE changesets (base_epoch TEXT, "commit" TEXT, state TEXT);
    `);
    const database = w.context.db;
    database.query("INSERT INTO runs VALUES (?, 'RUNNING'), (?, 'SUCCEEDED')").run(third, second);
    expect(await w.epochs.retain(w.base)).toBe(true);
    expect(await w.epochs.retain(third)).toBe(true);
    expect(await w.epochs.retain(second)).toBe(false);
    expect(Object.fromEntries(w.epochs.list().map((epoch) => [epoch.commit, epoch.retain]))).toEqual({ [w.base]: true, [second]: false, [third]: true });
    expect(existsSync(join(w.root, "epochs", second, "registry"))).toBe(true);
    database.query("INSERT INTO jobs VALUES (?, 'AWAITING_APPROVAL')").run(second);
    expect(await w.epochs.retain(second)).toBe(true);
    database.exec("UPDATE jobs SET state = 'SEALED'");
    database.query("INSERT INTO changesets VALUES (?, ?, 'verified')").run(second, third);
    expect(await w.epochs.retain(second)).toBe(true);
    database.exec("UPDATE changesets SET state = 'rejected'");
    expect(await w.epochs.retain(second)).toBe(false);
  } finally { w.close(); }
});

test("rolls back to a built commit by changing epoch pointers and Git refs", gitTest, async () => {
  const w = await world();
  try {
    await w.epochs.init(w.base, "human:acceptance");
    await expect(w.epochs.rollback(undefined, "human:acceptance")).rejects.toMatchObject({ code: "EPOCH_UNSET" });
    await git(w.registry, "switch", "-c", "candidate-work");
    const second = await commitChange(w.registry, "README.txt", "second");
    await expect(w.epochs.rollback(second, "human:acceptance")).rejects.toMatchObject({ code: "EPOCH_NOT_FOUND" });
    await expect(w.epochs.rollback(w.base, "human:acceptance")).rejects.toMatchObject({ code: "EPOCH_STATE_INVALID" });
    await w.epochs.build(second);
    const change = { by: "human:acceptance", reason: "fixture" };
    await w.epochs.setPointer("previous", w.base, change);
    await w.epochs.setPointer("active", second, change);
    await w.epochs.setPointer("candidate", second, change);
    expect(w.epochs.current()).toBe(second);
    expect(await w.epochs.rollback(undefined, change.by)).toBe(w.base);
    expect(w.epochs.pointers()).toEqual({ active: w.base, previous: second, candidate: null });
    expect(await git(w.registry, "rev-parse", "refs/factory/active", "refs/factory/previous")).toBe(`${w.base}\n${second}`);
    expect(await git(w.registry, "for-each-ref", "--format=%(refname)", "refs/factory/candidate")).toBe("");
    expect(await git(w.registry, "rev-parse", "refs/heads/main")).toBe(w.base);
    expect(await git(w.registry, "cat-file", "-t", second)).toBe("commit");
    expect(w.context.db.query("SELECT kind FROM events WHERE kind = 'epoch.pointer'").all().length).toBeGreaterThanOrEqual(3);
  } finally { w.close(); }
});

test("rejects mixed and unsigned constitution adoptions", gitTest, async () => {
  const w = await world();
  try {
    const actor = "human:acceptance";
    await w.epochs.init(w.base, actor);
    const station = "stations/upper.yaml";
    const constitution = "constitution/invariants.mrl";
    const text = readFileSync(join(w.registry, constitution), "utf8") + "\n// adopted\n";
    await git(w.registry, "switch", "-c", "mixed-work");
    writeFileSync(join(w.registry, constitution), text);
    const limits = readFileSync(join(w.registry, station), "utf8").replace("60s", "30s");
    writeFileSync(join(w.registry, station), limits);
    await git(w.registry, "add", "--", constitution, station);
    await git(w.registry, "commit", "-m", "Mixed change");
    await expect(w.epochs.adopt("HEAD", actor)).rejects.toMatchObject({ code: "EPOCH_ADOPT_INVALID", detail: { reason: "paths", paths: [constitution, station] } });
    await git(w.registry, "switch", "main");
    const unsigned = await commitChange(w.registry, constitution, text);
    await expect(w.epochs.adopt(unsigned, actor)).rejects.toMatchObject({ code: "EPOCH_ADOPT_INVALID", detail: { reason: "unsigned" } });
    expect(w.epochs.current()).toBe(w.base);
  } finally { w.close(); }
});

test("reports an unconfigured Registry before creating any epochs", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-no-git-epoch-"));
  const context = Context.open(root);
  try {
    await expect(EpochStore.open(context)).rejects.toMatchObject({ code: "REGISTRY_NOT_CONFIGURED" });
    expect(context.db.query("SELECT name FROM sqlite_master WHERE name = 'epochs'").get()).toBeNull();
  } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
});

test("materializes the recorded Git epoch from a restored bundle and SQLite checkpoint", { timeout: 120_000 }, async () => {
  const w = await world();
  try {
    const epoch = await w.epochs.build(w.base);
    await w.epochs.init(w.base, "human:acceptance");
    const newer = await commitChange(w.registry, "stations/upper.yaml", readFileSync(join(w.registry, "stations", "upper.yaml"), "utf8").replace("60s", "30s"));
    const destination = join(w.root, "backup");
    const restoredRoot = join(w.root, "restored");
    await createBackup({ root: w.root }, destination);
    const restored = await restoreBackup(destination, restoredRoot);
    const context = Context.open(restoredRoot);
    try {
      const epochs = await EpochStore.open(context);
      const caches = join(restoredRoot, "epochs", w.base);
      expect(existsSync(caches)).toBe(false);
      expect(epochs.current()).toBe(w.base);
      expect(await git(restored.registry!, "rev-parse", "HEAD")).toBe(newer);
      const snapshot = await epochs.materialize(w.base);
      expect(snapshot.epoch).toBe(w.base);
      expect(snapshot.spec("Station:upper@1").limits.timeout).toBe("60s");
      expect(await epochs.show()).toEqual({ ...epoch, retain: true, pointers: ["active"] });
      expect(context.db.query("SELECT kind FROM events WHERE kind = 'epoch.built'").all()).toEqual([{ kind: "epoch.built" }]);
      const frozen = join(caches, "resolutions.json");
      const runtime = join(caches, "resolution-runtime.json");
      expect(JSON.parse(readFileSync(frozen, "utf8"))).toEqual(snapshot.result.resolutions.map((item) => ({ ...item, origin: "precomputed" })));
      writeFileSync(frozen, "[]");
      writeFileSync(runtime, "{}");
      rmSync(join(caches, "registry"), { recursive: true, force: true });
      expect((await epochs.materialize(w.base)).epoch).toBe(w.base);
      expect(readFileSync(frozen, "utf8")).toBe("[]");
      expect(readFileSync(runtime, "utf8")).toBe("{}");
    } finally { context.close(); }
  } finally { w.close(); }
});

test("runs the human epoch CLI with canonical results and explicit actor attribution", gitTest, async () => {
  const w = await world();
  const log = console.log;
  const outputs: unknown[] = [];
  try {
    console.log = (value) => outputs.push(JSON.parse(String(value)));
    expect(await runEpochCli(w.context, "init", ["--by", "human:cli", w.base])).toBe(0);
    expect(await runEpochCli(w.context, "show", [w.base.slice(0, 12)])).toBe(0);
    expect(await runEpochCli(w.context, "ls", [])).toBe(0);
    expect(outputs[0]).toEqual({ commit: w.base });
    expect(outputs[1]).toMatchObject({ commit: w.base, retain: true, pointers: ["active"] });
    expect(outputs[2]).toEqual(w.epochs.list());
    const event = w.context.db.query<{ data: string }>("SELECT data FROM events WHERE kind = 'epoch.pointer'").get();
    expect(JSON.parse(event!.data)).toMatchObject({ by: "human:cli", reason: "init" });
    await expect(runEpochCli(w.context, "show", ["--by", "human:cli"])).rejects.toMatchObject({ code: "CLI_USAGE" });
    await expect(runEpochCli(w.context, "rollback", [])).rejects.toMatchObject({ code: "EPOCH_UNSET" });
    await expect(runEpochCli(w.context, "adopt", [])).rejects.toMatchObject({ code: "CLI_USAGE" });
  } finally { console.log = log; w.close(); }
});

test("runs a Line against the active Registry Git commit and can plan against an explicit epoch", gitTest, async () => {
  const w = await world();
  const log = console.log;
  const outputs: any[] = [];
  try {
    console.log = (value) => outputs.push(JSON.parse(String(value)));
    const routes = ["--root", w.root, "--provider", "local", "--json"];
    expect(await main([...routes, "epoch", "init", w.base])).toBe(0);
    expect(outputs.pop()).toEqual({ commit: w.base });
    const machine = "machines/toolbox/machine.yaml";
    const body = readFileSync(join(w.registry, machine), "utf8");
    const newer = await commitChange(w.registry, machine, body.replace("upper-text", "missing-worktree-tool"));
    await w.epochs.build(newer);
    expect(await main([...routes, "line", "plan", "note-trigger@1", "--epoch", newer])).toBe(0);
    expect(outputs.pop()).toMatchObject({ epoch: newer });
    const note = join(w.root, "note.txt");
    const tools = join(w.root, "tools.json");
    writeFileSync(note, "keep the active tool\n");
    writeFileSync(tools, JSON.stringify(Object.fromEntries(["upper-text", "count-text"].map((name) =>
      [name, [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools", name.replaceAll("-", "_") + ".ts")]],
    ))));
    const provider = [...routes, "--local-commands", tools];
    expect(await main([...provider, "bean", "import", "--type", "note.text@1", "--file", `note.txt=${note}`])).toBe(0);
    const { id: bean } = outputs.pop();
    expect(await main([...provider, "line", "run", "note-trigger@1", "--in", `note=${bean}`])).toBe(0);
    expect(outputs.pop()).toMatchObject({ state: "SUCCEEDED", epoch: w.base });
  } finally { console.log = log; w.close(); }
});
