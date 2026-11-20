import { describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import { randomUUID } from "node:crypto";
import { join, resolve } from "node:path";
import { Context, CounterIds, FakeClock, Gate, LocalProvider, canonicalBytes, digestBytes, loadSnapshot } from "../src/legacy";
import type { SandboxSpec } from "../runtime/podman";

const { cpSync, mkdirSync, rmSync } = fs;
const provider = new LocalProvider({
  "count-text": [Bun.argv[0], resolve("tests/fakes/tools/count_text.ts")],
  "check-task": [Bun.argv[0], resolve("tests/fakes/tools/check_task.ts")],
});

describe("Bun M1 Gate boundary", () => {
  test("admits an external card without validators and explains missing required metadata", async () => {
    const root = join("tmp", `legacy-gate-card-${randomUUID()}`);
    const context = Context.open(root);
    const opened: SandboxSpec[] = [];
    class RecordingProvider extends LocalProvider {
      async open(spec: SandboxSpec) { opened.push(spec); return super.open(spec); }
    }
    try {
      const registry = join(root, "registry");
      cpSync("tests/fixtures/registry-m1", registry, { recursive: true });
      await Bun.write(join(registry, "types", "card.yaml"), JSON.stringify({
        apiVersion: "mao/v1",
        kind: "Type",
        metadata: { name: "card", version: "1.0" },
        spec: {
          payload: [{ path: "card.json", media_type: "application/json", required: true }],
          meta_schema: { type: "object", required: ["title"], properties: { title: { type: "string" } } },
        },
      }));
      const snapshot = await loadSnapshot(registry);
      const gate = new Gate(snapshot, context.warehouse, join(root, "work"), new RecordingProvider());
      const input = join(root, "card.json");
      await Bun.write(input, "{}");
      const files = { "card.json": input };
      const accepted = await gate.admitExternal("card@1", files, { title: "ok" });
      expect(accepted.report.data.verdict).toBe("pass");
      expect(accepted.bean).toMatchObject({ sealed: false });
      expect(context.warehouse.get(accepted.bean!.id)).toMatchObject({ sealed: false, type: "card@1.0" });
      expect((await gate.admitExternal("card@1.0", files, { title: "ok" })).bean?.id).toBe(accepted.bean!.id);
      expect(opened).toEqual([]);
      const rejected = await gate.admitExternal("card@1", files, {});
      expect(rejected.report.data.verdict).toBe("reject");
      expect(rejected.bean).toBeNull();
      expect(rejected.report.data.checks.some(({ stage, name, ok }) => stage === "consistency" && name === "meta_schema" && !ok)).toBe(true);
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });
  test("checks missing, unsealed, wrong type, properties and ordered input cardinality without opening a sandbox", async () => {
    const context = Context.open(join("tmp", `legacy-gate-inputs-${randomUUID()}`));
    const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
    const opened: SandboxSpec[] = [];
    class RecordingProvider extends LocalProvider {
      async open(spec: SandboxSpec) { opened.push(spec); return super.open(spec); }
    }
    const gate = new Gate(snapshot, context.warehouse, join(context.root, "work"), new RecordingProvider());
    try {
      const store = context.warehouse;
      const source = join(context.root, "input.txt");
      const addNote = async (body: string) => {
        await Bun.write(source, body);
        return store.add({ type: "note.text@1.0", files: { "note.txt": source } });
      };
      const first = await addNote("hi\n");
      const second = await addNote("yo\n");
      const empty = await addNote("   \n");
      const wrong = await store.add({ type: "agent.task@1.0", files: { "task.json": source } });
      expect(first.sealed).toBe(false);
      const checkRejects = (value: string | string[], reason: string, beanId?: string) => {
        try {
          gate.checkInputs("upper@1", { note: value });
          throw new Error("Expected INTAKE_REJECTED");
        } catch (error) {
          expect(error).toMatchObject({ code: "INTAKE_REJECTED", detail: { problems: [{ reason, ...(beanId ? { bean_id: beanId } : {}) }] } });
        }
      };
      checkRejects(first.id, "unsealed", first.id);
      checkRejects("sha256:" + "ff".repeat(32), "not_found");
      const report = "sha256:" + "ab".repeat(32);
      store.sealMany([[first.id, { chars: 2 }], [second.id, { chars: 2 }], [empty.id, { chars: 0 }], [wrong.id, {}]], "job-1", report);
      checkRejects(wrong.id, "type");
      checkRejects(empty.id, "requires");
      checkRejects([first.id], "cardinality");
      expect(gate.checkInputs("upper@1", { note: first.id }).note).toMatchObject({ id: first.id, sealed: true });
      snapshot.docs["Station:upper@1"].spec.inputs.note.type = "list<note.text@1>";
      const listed = gate.checkInputs("upper@1", { note: [first.id, second.id] }).note;
      if (!Array.isArray(listed)) throw new Error("Expected list input");
      expect(listed.map(({ id }) => id)).toEqual([first.id, second.id]);
      expect(opened).toEqual([]);
    } finally { context.close(); rmSync(context.root, { recursive: true, force: true }); }
  });
  test("validates external material and checks required properties on sealed inputs", { timeout: 60_000 }, async () => {
    const root = join("tmp", "legacy-gate-" + randomUUID());
    const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
    const context = Context.open(root, { clock: new FakeClock(), ids: new CounterIds() });
    try {
      const source = join(root, "note.txt");
      await Bun.write(source, "hello");
      const gate = new Gate(snapshot, context.warehouse, join(root, "work"), provider);
      const admitted = await gate.admitExternal("note.text@1", { "note.txt": source }, { title: "hello" });
      expect(admitted.report.data.verdict).toBe("pass");
      const bean = admitted.bean;
      if (!bean) throw new Error(admitted.report.data.feedback);
      const admittedFacts = admitted.report.data.facts[bean.id];
      expect(admittedFacts).toEqual({ chars: 5, has_lower: true, lines: 1 });
      await context.warehouse.sealMany([[bean.id, admittedFacts]], null, admitted.report.digest());
      expect(gate.checkInputs("upper@1", { note: bean.id }).note).toMatchObject({ id: bean.id, facts: admittedFacts });
      const station = snapshot.docs["Station:upper@1"].spec;
      station.inputs.note.requires = ["shouting"];
      expect(() => gate.checkInputs("upper@1", { note: bean.id })).toThrow("来料检验未通过");

      await Bun.write(source, " ");
      const rejected = await gate.admitExternal("note.text@1", { "note.txt": source }, {});
      expect(rejected.bean).toBeNull();
      expect(rejected.report.data.verdict).toBe("reject");
      expect(rejected.report.data.feedback).toContain("文本为空");
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("checks output structure, measured facts, guarantees, acceptance and metadata", { timeout: 60_000 }, async () => {
    const root = join("tmp", "legacy-gate-inspect-" + randomUUID());
    const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
    const context = Context.open(root, { clock: new FakeClock(), ids: new CounterIds() });
    try {
      const gate = new Gate(snapshot, context.warehouse, join(root, "work"), provider);
      const outDir = join(root, "out");
      const options = { jobId: "job-1", attempt: 1, outDir };
      const missing = await gate.inspect("upper@1", options);
      expect(missing.data.verdict).toBe("reject");
      const note = join(outDir, "note", "note.txt");
      mkdirSync(join(outDir, "note"), { recursive: true });
      await Bun.write(note, "hello");
      const lowercase = await gate.inspect("upper@1", options);
      expect(lowercase.data.verdict).toBe("reject");
      expect(lowercase.data.checks).toContainEqual({
        port: "note", index: 0, stage: "property", name: "shouting", ok: false, detail: "shouting",
      });
      await Bun.write(note, "HELLO");
      const tooShort = await gate.inspect("writer@1", options);
      expect(tooShort.data.verdict).toBe("reject");
      expect(tooShort.data.feedback).toContain("accept[0]");
      await Bun.write(note, "HELLO\nWORLD");
      const accepted = await gate.inspect("writer@1", { ...options, jobId: "job-11", attempt: 4 });
      expect(accepted.data.verdict).toBe("pass");
      expect(accepted.data.facts[accepted.data.outputs.note[0]]).toEqual({ chars: 10, lines: 2, has_lower: false });
      const body = accepted.toJSON();
      expect(body).toMatchObject({ job_id: "job-11", station: "Station:writer@1", attempt: 4, verdict: accepted.data.verdict,
        outputs: accepted.data.outputs, facts: accepted.data.facts, checks: accepted.data.checks, feedback: "",
      });
      expect(["structure", "validator", "property", "accept", "consistency"]).toContain(body.checks[0].stage);
      expect(accepted.digest()).toBe(digestBytes(canonicalBytes(body)));
      expect(accepted.digest()).toBe(accepted.digest());
      const meta = join(outDir, "note", "meta.json");
      await Bun.write(meta, JSON.stringify({ title: 42 }));
      const invalidMeta = await gate.inspect("writer@1", options);
      expect(invalidMeta.data.verdict).toBe("reject");
      expect(invalidMeta.data.feedback).toContain("meta_schema title must be string");
      await Bun.write(meta, JSON.stringify({ title: "Valid title", lines: 5 }));
      const contradictory = await gate.inspect("writer@1", options);
      expect(contradictory.data.verdict).toBe("reject");
      expect(contradictory.data.feedback).toContain("meta_schema lines");
      await Bun.write(join(outDir, "note", "other.txt"), "EXTRA");
      const extra = await gate.inspect("upper@1", options);
      expect(extra.data.verdict).toBe("reject");
      expect(extra.data.outputs).toEqual({});
      expect(extra.data.feedback).toContain("other.txt");
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });

  test("rejects linked output directories and list ports with a gap", async () => {
    const root = join("tmp", "legacy-gate-structure-" + randomUUID());
    const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
    const context = Context.open(root, { clock: new FakeClock(), ids: new CounterIds() });
    try {
      const outDir = join(root, "out");
      const sourceDir = join(root, "unrelated");
      mkdirSync(outDir, { recursive: true });
      mkdirSync(sourceDir, { recursive: true });
      await Bun.write(join(sourceDir, "note.txt"), "UNRELATED");
      const { symlinkSync } = fs as typeof fs & { symlinkSync: (source: string, target: string, type: string) => void };
      symlinkSync(resolve(sourceDir), join(outDir, "note"), process.platform === "win32" ? "junction" : "dir");
      const gate = new Gate(snapshot, context.warehouse, join(root, "work"), provider);
      const options = { jobId: "job-1", attempt: 1, outDir };
      const linked = await gate.inspect("upper@1", options);
      expect(linked.data.verdict).toBe("reject");
      expect(linked.data.outputs).toEqual({});

      const station = snapshot.docs["Station:upper@1"].spec;
      station.outputs.note.type = "list<note.text@1>";
      const listDir = join(root, "list-out");
      mkdirSync(join(listDir, "note", "1"), { recursive: true });
      const gap = await gate.inspect("upper@1", { ...options, outDir: listDir });
      expect(gap.data.verdict).toBe("reject");
      expect(gap.data.feedback).toContain("从 0 连续");
    } finally { context.close(); rmSync(root, { recursive: true, force: true }); }
  });
});
