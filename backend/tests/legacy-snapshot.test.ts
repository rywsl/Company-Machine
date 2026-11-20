import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { createHash } from "node:crypto";
import { cpSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Context, LineRunner, LocalProvider, RegistrySnapshotStore, Runner, canonicalJson, compileRunPlan, digestBytes, loadSnapshot } from "../src/legacy";
import type { EvalEnvironment } from "../mrl/evaluator";

const registryFixture = "tests/fixtures/registry-m2";

function writeTypeFixtures(registry: string): void {
  const validator = (name: string) => ({
    name, image: `fixture/validator@sha256:${"a".repeat(64)}`,
    run: ["count-text", "/bean/payload/note.txt"], emits: ["chars", "lines"],
    fixtures: { accept: ["fixtures/note/ok.txt"], reject: ["fixtures/note/empty.txt"] },
  });
  const writeType = (name: string, version: string, spec: Record<string, unknown>) => {
    writeFileSync(join(registry, "types", `${name}-${version}.yaml`), JSON.stringify({
      apiVersion: "mao/v1", kind: "Type", metadata: { name, version }, spec,
    }));
  };
  writeType("memo", "1.0", { facts: { chars: "int", lines: "int" }, properties: { non_empty: "bean.facts.chars > 0" } });
  const child = { extends: "memo@1", payload: [{ path: "note.txt", media_type: "text/plain", required: true }], properties: { short: "bean.facts.chars <= 280" } };
  writeType("memo.text", "1.0", { ...child, validators: [validator("old-counter")] });
  writeType("memo.text", "1.1", { ...child, validators: [validator("count-text")], always: ["non_empty"] });
  writeType("memo.indexed", "1.0", { extends: "memo.text@1", payload: child.payload, validators: [validator("index-counter")] });
  writeType("memo.empty", "1.0", {});
}

async function writeCompiledSiteFixtures(root: string): Promise<void> {
  const edit = async (file: string, update: (document: any) => void, output = file) => {
    const document = Bun.YAML.parse(await Bun.file(join(root, file)).text());
    update(document);
    await Bun.write(join(root, output), JSON.stringify(document));
  };
  const suffix = { type: "string", default: "!" };
  const times = { type: "int", default: 2 };
  await edit("machines/toolbox/machine.yaml", ({ spec }) => {
    spec.recipes.upper.params = { times };
    spec.recipes.split = {
      inputs: { note: { type: "note.text@1" } },
      outputs: { parts: { type: "list<note.text@1>" } },
      params: { suffix },
      run: ["script", "{{ params.suffix }}"],
    };
  });
  await edit("stations/upper.yaml", (document) => {
    document.metadata.name = "split";
    document.spec.recipe = "split";
    document.spec.outputs = { parts: { type: "list<note.text@1>", accept: ["out.size() == 2"] } };
    document.spec.params = { suffix };
  }, "stations/split.yaml");
  await edit("stations/upper.yaml", ({ spec }) => {
    spec.params = { times };
    spec.outputs.note.accept = ["out.facts.chars == inputs.note.facts.chars + params.times"];
  });
  await edit("runtimes/fake-agent.yaml", ({ spec }) => {
    spec.invoke.resume = ["--resume", "{{ session.id }}"];
    spec.env.MODEL = "{{ operator.model }}";
  });
  await edit("types/raw.yaml", ({ spec }) => {
    spec.properties.untyped_meta = "bean.meta.unknown.rank > 0";
  });
}

function referenceEpoch(root: string): string {
  const digests = new Map<string, string>();
  const visit = (prefix: string) => {
    for (const entry of readdirSync(join(root, prefix), { withFileTypes: true })) {
      const path = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) visit(path);
      if (entry.isFile()) digests.set(path, `sha256:${createHash("sha256").update(readFileSync(join(root, path))).digest("hex")}`);
    }
  };
  visit("");
  const sorted = Object.fromEntries([...digests.keys()].sort().map((path) => [path, digests.get(path)]));
  return `e-${createHash("sha256").update(JSON.stringify(sorted)).digest("hex").slice(0, 16)}`;
}

describe("Frozen Bun Registry snapshots", () => {
  test("looks up typed predicates and templates by subject before and after reopening", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-compiled-sites-"));
    const db = new Database(":memory:");
    try {
      cpSync(registryFixture, root, { recursive: true });
      await writeCompiledSiteFixtures(root);
      const snapshots = new RegistrySnapshotStore(db);
      const initial = await snapshots.capture(root);
      expect(initial.result.diagnostics.map(({ code }) => code)).toContain("W-PROPERTY-META");
      expect(initial.result.expressionSites.length).toBeGreaterThan(10);
      expect(new Set(initial.result.expressionSites.map(({ subject }) => subject)).size).toBe(initial.result.expressionSites.length);
      const upper = await Bun.file(join(root, "stations", "upper.yaml")).text();
      expect(upper).toContain("params.times");
      await Bun.write(join(root, "stations", "upper.yaml"), upper.replace("params.times", "1"));
      const reopened = await snapshots.materialize(initial.epoch);
      expect(reopened.result.expressionSites).toEqual(initial.result.expressionSites);
      const purposes = [
        ["Type:note.text@1.0/properties.non_empty", "bean.facts.chars > 0"],
        ["Station:upper@1/outputs.note.accept[0]", "out.facts.chars == inputs.note.facts.chars + params.times"],
        ["Station:split@1/outputs.parts.accept[0]", "out.size() == 2"],
        ["Station:quote@1/approval.in_run", "request.cost_usd <= 0.5"],
        ["Machine:toolbox@1/recipes.split.run[1]", "{{ params.suffix }}"],
        ["OperatorRuntime:fake-agent@1/invoke.argv[2]", "{{ brief.task }}"],
        ["OperatorRuntime:fake-agent@1/invoke.argv[8]", "{{ limits.max_turns }}"],
        ["OperatorRuntime:fake-agent@1/env.MODEL", "{{ operator.model }}"],
      ];
      const bean = (chars: number) => ({ id: "bean", type: "note.text@1.0", facts: { chars }, meta: {}, payload: [] });
      for (const snapshot of [initial, reopened]) {
        for (const { subject, source, kind } of snapshot.result.expressionSites) {
          expect(snapshot.compiled(subject)).toMatchObject({ source, kind });
          expect(snapshot.compiled(subject)).toBe(snapshot.compiled(subject));
        }
        for (const [subject, source] of purposes) expect(snapshot.compiled(subject).source).toBe(source);
        const evaluate = (subject: string, env: EvalEnvironment) => {
          const site = snapshot.compiled(subject);
          if (site.kind !== "expression") throw new Error(`${subject} must be a predicate`);
          return site.evaluate(env);
        };
        const render = (subject: string, env: EvalEnvironment) => {
          const site = snapshot.compiled(subject);
          if (site.kind !== "template") throw new Error(`${subject} must be a template`);
          return site.render(env);
        };
        expect(evaluate("Type:note.text@1.0/properties.non_empty", { bean: bean(3) })).toBe(true);
        expect(evaluate("Type:note.text@1.0/properties.non_empty", { bean: bean(0) })).toBe(false);
        expect(evaluate("Type:note.raw@1.0/properties.untyped_meta", { bean: { ...bean(2), meta: { unknown: { rank: 1 } } } })).toBe(true);
        const inputs = { note: bean(3) };
        const params = { times: 2 };
        expect(evaluate("Station:upper@1/outputs.note.accept[0]", { out: bean(5), inputs, params })).toBe(true);
        expect(evaluate("Station:upper@1/outputs.note.accept[0]", { out: bean(1), inputs, params })).toBe(false);
        expect(evaluate("Station:split@1/outputs.parts.accept[0]", { out: [bean(1), bean(1)] })).toBe(true);
        expect(evaluate("Station:split@1/outputs.parts.accept[0]", { out: [bean(1)] })).toBe(false);
        expect(evaluate("Station:quote@1/approval.in_run", { request: { cost_usd: 0.5 }, inputs, params })).toBe(true);
        expect(evaluate("Station:quote@1/approval.in_run", { request: { cost_usd: 0.6 }, inputs, params })).toBe(false);
        expect(render("Machine:toolbox@1/recipes.split.run[1]", { inputs, params: { suffix: "!" } })).toBe("!");
        const runtime = { brief: { task: "hello" }, limits: { max_turns: 4 }, operator: { model: "fake" }, session: { id: "external-session" } };
        expect(render("OperatorRuntime:fake-agent@1/invoke.argv[2]", runtime)).toBe("hello");
        expect(render("OperatorRuntime:fake-agent@1/invoke.argv[8]", runtime)).toBe("4");
        expect(render("OperatorRuntime:fake-agent@1/env.MODEL", runtime)).toBe("fake");
        expect(render("OperatorRuntime:fake-agent@1/invoke.resume[1]", runtime)).toBe("external-session");
        const absent = "Station:upper@1/nope";
        let failure: unknown;
        try { snapshot.compiled(absent); } catch (error) { failure = error; }
        expect(failure).toMatchObject({ code: "SUBJECT_UNKNOWN", detail: { subject: absent } });
      }
    } finally { db.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("indexes the kernel type closure and resolves current non-retired minors", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-snapshot-types-"));
    const db = new Database(":memory:");
    try {
      cpSync(registryFixture, root, { recursive: true });
      writeTypeFixtures(root);
      const snapshots = new RegistrySnapshotStore(db);
      const snapshot = await snapshots.capture(root);
      expect(snapshot.result.ok).toBe(true);
      expect(snapshot.result.diagnostics.filter(({ severity }) => severity !== "info")).toEqual([]);
      expect(snapshot.root).toBe(root);
      expect(snapshot.key("Station", "upper@1")).toBe("Station:upper@1");
      expect(snapshot.key("Station", "Station:upper@1")).toBe("Station:upper@1");
      expect(snapshot.key("Type", "memo.text@1.1")).toBe("Type:memo.text@1.1");
      snapshot.doc("Station:upper@1").spec.recipe = "changed";
      snapshot.spec("Station:upper@1").recipe = "changed";
      expect(snapshot.doc("Station:upper@1").spec.recipe).toBe("upper");
      expect(snapshot.spec("Station:upper@1").recipe).toBe("upper");
      expect(snapshot.spec("Type:memo.text@1.1").extends).toBe("memo@1");
      expect(snapshot.typeKey("memo.text@1")).toBe("Type:memo.text@1.1");
      expect(snapshot.typeKey("memo@1")).toBe("Type:memo@1.0");
      expect(snapshot.typeKey("memo.empty@1")).toBe("Type:memo.empty@1.0");
      const rejected: unknown[] = [];
      try { snapshot.typeKey("memo.text@1.1"); } catch (error) { rejected.push(error); }
      try { snapshot.factsDecl("missing@1"); } catch (error) { rejected.push(error); }
      try { snapshot.doc("Station:missing@1"); } catch (error) { rejected.push(error); }
      expect(rejected).toMatchObject([
        { code: "TYPE_NOT_FOUND", detail: { tid: "memo.text@1.1" } },
        { code: "TYPE_NOT_FOUND", detail: { tid: "missing@1" } },
        { code: "SPEC_NOT_FOUND", detail: { key: "Station:missing@1" } },
      ]);
      for (const [type, parent] of [["memo.text@1", "memo@1"], ["memo.indexed@1", "memo@1"], ["memo.text@1", "memo.text@1"], ["memo@1", "memo@1"]]) {
        expect(snapshot.isSubtype(type, parent)).toBe(true);
      }
      for (const type of ["memo.empty@1", "missing@1", "memo.text@1.1", "Type:memo@1.0", "memo@2", "any"]) {
        expect(snapshot.isSubtype(type, "memo@1")).toBe(false);
        expect(snapshot.isSubtype(type, type)).toBe(type === "memo.empty@1");
      }
      expect(snapshot.isSubtype("memo@1", "memo.text@1")).toBe(false);
      expect(snapshot.factsDecl("memo.indexed@1")).toEqual({ chars: "int", lines: "int" });
      expect(snapshot.factsDecl("memo@1")).toEqual({ chars: "int", lines: "int" });
      expect(snapshot.factsDecl("memo.empty@1")).toEqual({});
      expect([...snapshot.always("memo.indexed@1")]).toEqual(["non_empty"]);
      expect([...snapshot.always("memo@1")]).toEqual([]);
      expect(snapshot.properties("memo.indexed@1")).toEqual({ non_empty: "bean.facts.chars > 0", short: "bean.facts.chars <= 280" });
      expect(snapshot.validators("memo.text@1")).toMatchObject([{ name: "count-text", run: ["count-text", "/bean/payload/note.txt"] }]);
      expect(snapshot.validators("memo.indexed@1").map(({ name }) => name)).toEqual(["index-counter"]);
      expect(snapshot.validators("memo@1")).toEqual([]);
      const validators = snapshot.validators("memo.text@1");
      validators[0].run[0] = "changed";
      expect(snapshot.validators("memo.text@1")[0].run[0]).toBe("count-text");
      snapshot.factsDecl("memo.text@1").chars = "bool";
      snapshot.properties("memo.text@1").short = "false";
      snapshot.always("memo.text@1").clear();
      expect(snapshot.factsDecl("memo.text@1").chars).toBe("int");
      expect(snapshot.properties("memo.text@1").short).toBe("bean.facts.chars <= 280");
      expect([...snapshot.always("memo.text@1")]).toEqual(["non_empty"]);

      const latest = join(root, "types", "memo.text-1.1.yaml");
      const retired = JSON.parse(readFileSync(latest, "utf8"));
      retired.metadata.lifecycle = "retired";
      writeFileSync(latest, JSON.stringify(retired));
      const current = await snapshots.capture(root);
      expect(current.typeKey("memo.text@1")).toBe("Type:memo.text@1.0");
      expect([...current.always("memo.text@1")]).toEqual([]);
      expect(current.validators("memo.text@1").map(({ name }) => name)).toEqual(["old-counter"]);

      const row = db.query<{ tree_digest: string; payload: string }>("SELECT tree_digest, payload FROM registry_snapshots WHERE epoch=?").get(snapshot.epoch)!;
      const legacy = JSON.parse(row.payload);
      delete legacy.result.typeRelations;
      delete legacy.result.expressionSites;
      const payload = canonicalJson(legacy);
      const digest = digestBytes(new TextEncoder().encode(`${row.tree_digest}\n${payload}`));
      db.query("UPDATE registry_snapshots SET payload=?, payload_digest=? WHERE epoch=?").run(payload, digest, snapshot.epoch);
      const reopened = await snapshots.materialize(snapshot.epoch);
      expect(reopened.typeKey("memo.text@1")).toBe("Type:memo.text@1.1");
      expect(reopened.result.typeRelations).toEqual(snapshot.result.typeRelations);
      expect(reopened.result.expressionSites).toEqual(snapshot.result.expressionSites);
      const inherited = reopened.compiled("Type:memo@1.0/properties.non_empty");
      if (inherited.kind !== "expression") throw new Error("Expected an inherited property expression");
      expect(inherited.evaluate({ bean: { facts: { chars: 1 } } })).toBe(true);
      expect(reopened.properties("memo.indexed@1")).toEqual(snapshot.properties("memo.indexed@1"));
    } finally { db.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("matches an independent tree hash across directories and includes non-Spec files", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-epoch-tree-"));
    const first = join(root, "first");
    const second = join(root, "second");
    try {
      cpSync(registryFixture, first, { recursive: true });
      cpSync(registryFixture, second, { recursive: true });
      const a = await loadSnapshot(first);
      expect(a.epoch).toMatch(/^e-[0-9a-f]{16}$/);
      expect(a.epoch).toBe(referenceEpoch(first));
      expect((await loadSnapshot(second)).epoch).toBe(a.epoch);
      writeFileSync(join(second, "added.txt"), "contents outside the Registry schema\r\n");
      const changed = await loadSnapshot(second);
      expect(changed.epoch).not.toBe(a.epoch);
      expect(changed.epoch).toBe(referenceEpoch(second));
    } finally { rmSync(root, { recursive: true, force: true }); }
  });

  test("reopens the original documents, constitution and resolutions after the source Registry changes", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-epoch-reopen-"));
    const registry = join(root, "registry");
    const dbPath = join(root, "factory.db");
    cpSync(registryFixture, registry, { recursive: true });
    let db = new Database(dbPath);
    try {
      const initial = await new RegistrySnapshotStore(db).capture(registry);
      const originalConstitution = initial.result.registry.constitution;
      const originalResolutions = initial.result.resolutions;
      const upper = join(registry, "stations", "upper.yaml");
      const rules = join(registry, "constitution", "invariants.mrl");
      expect(initial.spec("Station:upper@1").limits.timeout).toBe("60s");
      expect(originalConstitution.length).toBeGreaterThan(0);
      expect(originalResolutions.length).toBeGreaterThan(0);
      const stored = db.query<{ payload: string }>("SELECT payload FROM registry_snapshots WHERE epoch = ?").get(initial.epoch)!;
      expect(stored.payload).not.toContain("mao-epoch-reopen-");

      db.close();
      db = new Database(dbPath);
      const snapshots = new RegistrySnapshotStore(db);
      writeFileSync(upper, readFileSync(upper, "utf8").replace("60s", "30s"));
      writeFileSync(rules, readFileSync(rules, "utf8") + "\n// Updated Registry\n");
      const current = await snapshots.capture(registry);
      expect(current.epoch).not.toBe(initial.epoch);
      expect(current.spec("Station:upper@1").limits.timeout).toBe("30s");
      const reopened = await snapshots.materialize(initial.epoch, registry);
      expect(reopened.spec("Station:upper@1").limits.timeout).toBe("60s");
      expect(reopened.result.registry.constitution).toEqual(originalConstitution);
      expect(reopened.result.resolutions).toEqual(originalResolutions);
      const corrupted = db.query("UPDATE registry_snapshots SET payload = payload || ' ' WHERE epoch = ?");
      corrupted.run(initial.epoch);
      await expect(snapshots.materialize(initial.epoch, registry)).rejects.toMatchObject({ code: "EPOCH_CORRUPT" });
    } finally { db.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("recovers an older unfrozen epoch only from matching bytes and rejects invalid capture", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-epoch-fallback-"));
    const registry = join(root, "registry");
    const db = new Database(":memory:");
    try {
      cpSync(registryFixture, registry, { recursive: true });
      const old = await loadSnapshot(registry);
      const snapshots = new RegistrySnapshotStore(db);
      await expect(snapshots.materialize(old.epoch)).rejects.toMatchObject({ code: "EPOCH_NOT_FOUND" });
      const extra = join(registry, "local.txt");
      writeFileSync(extra, "changed");
      await expect(snapshots.materialize(old.epoch, registry)).rejects.toMatchObject({ code: "EPOCH_MISMATCH" });
      expect(db.query("SELECT epoch FROM registry_snapshots").all()).toEqual([]);
      rmSync(extra);
      expect((await snapshots.materialize(old.epoch, registry)).docs).toEqual(old.docs);
      const upper = join(registry, "stations", "upper.yaml");
      const original = readFileSync(upper, "utf8");
      writeFileSync(upper, "invalid: [");
      await expect(snapshots.capture(registry)).rejects.toMatchObject({ code: "REGISTRY_INVALID" });
      writeFileSync(upper, original);
      rmSync(join(registry, "constitution"), { recursive: true });
      await expect(snapshots.capture(registry)).rejects.toMatchObject({
        code: "REGISTRY_INVALID", detail: { diags: [{ code: "E-CONSTITUTION", severity: "error" }] },
      });
      expect(db.query("SELECT epoch FROM registry_snapshots").all()).toEqual([{ epoch: old.epoch }]);
    } finally { db.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("rejects a Run tick from a LineRunner bound to a different epoch", async () => {
    const root = mkdtempSync(join(tmpdir(), "mao-epoch-run-"));
    const context = Context.open(root);
    try {
      const snapshots = new RegistrySnapshotStore(context.db);
      const bound = await snapshots.capture(registryFixture);
      const other = await loadSnapshot("spec/examples/registry");
      expect(other.epoch).not.toBe(bound.epoch);
      const provider = new LocalProvider();
      const lines = new LineRunner(new Runner(context, bound, provider));
      const plan = compileRunPlan(bound, "note-pipeline@1");
      const run = lines.runs.create(plan, { inputs: { task: "sealed-task" } });
      const wrong = new LineRunner(new Runner(context, other, provider));
      let failure: unknown;
      try { wrong.tick(run.id); } catch (error) { failure = error; }
      expect(failure).toMatchObject({ code: "EPOCH_MISMATCH" });
      expect(lines.runs.get(run.id).state).toBe("PENDING");
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });
});
