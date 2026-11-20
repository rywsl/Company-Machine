import { describe, expect, test } from "bun:test";
import { FactoryError } from "../src/legacy/errors";
import { compileRunPlan, RunPlan } from "../src/legacy/runplan";
import { loadSnapshot, Snapshot } from "../src/legacy/snapshot";

const EXAMPLE = "explainer-multilang@5";
const exampleKey = "Line:" + EXAMPLE;

function change(snapshot: Snapshot, update: (docs: Snapshot["docs"]) => void): Snapshot {
  const docs = structuredClone(snapshot.docs);
  update(docs);
  return new Snapshot(snapshot.root, snapshot.epoch, docs, snapshot.result);
}

function checkError(work: () => unknown, code: string, detail: Record<string, unknown> = {}): void {
  try {
    work();
  } catch (error) {
    expect(error).toBeInstanceOf(FactoryError);
    expect(error).toMatchObject({ code, detail });
    return;
  }
  throw new Error("Expected " + code);
}

describe("Bun static RunPlan", () => {
  test("orders the example by dependency layer and records parameters and budgets", async () => {
    const plan = compileRunPlan(await loadSnapshot("spec/examples/registry"), EXAMPLE);
    expect(plan.line).toBe(exampleKey);
    expect(plan.nodes.map(({ id }) => id)).toEqual(["explainer", "dub_en", "to_github", "vertical", "to_s3", "to_shorts"]);
    expect(plan.nodes.every((node) => node.kind === "station" && !node.implicit)).toBe(true);
    expect(plan.node("dub_en").inputs).toContainEqual({ port: "video", source: "node", node: "explainer", outPort: "video" });
    expect(plan.node("to_s3").templated).toEqual(["prefix"]);
    expect(plan.node("to_s3").params.prefix).toBe("explainers/{{ run.id }}/");
    expect(plan.budgetCapUsd).toBe(12);
    expect(plan.budgetItems).toEqual([["dub_en", 3], ["explainer", 5], ["vertical", 0]]);
    expect(plan.toJSON().nodes[0].inputs).toEqual([{ port: "brief", source: "input", node: "", out_port: "brief" }]);
    checkError(() => plan.node("missing"), "NODE_NOT_FOUND", { node: "missing" });
  });

  test("produces stable JSON, digest and persisted-plan round trip", async () => {
    const base = await loadSnapshot("tests/fixtures/registry-m2");
    const first = compileRunPlan(base, "note-pipeline@1");
    const reordered = change(base, (docs) => {
      const spec = docs["Line:note-pipeline@1"].spec;
      spec.steps = Object.fromEntries(Object.entries(spec.steps).reverse());
      const upper = spec.steps.upper;
      upper.in = Object.fromEntries(Object.entries(upper.in).reverse());
    });
    const second = compileRunPlan(reordered, "Line:note-pipeline@1");
    expect(first.digest()).toBe(second.digest());
    expect(first.toJSON()).toEqual(second.toJSON());
    expect(first.toJSON().input_requires).toEqual({ task: ["has_steps"] });
    const restored = RunPlan.fromJSON(JSON.parse(JSON.stringify(first)));
    expect(restored.toJSON()).toEqual(first.toJSON());
    expect(restored.digest()).toBe(first.digest());
    expect(restored.node("upper").inputs[0].outPort).toBe("out");
  });

  test("injects a converter while retaining wire resolution and its budget item", async () => {
    const base = await loadSnapshot("spec/examples/registry");
    const snapshot = change(base, (docs) => {
      docs["Station:explainer@3"].spec.outputs.subtitle.type = "subtitle.vtt@1";
      docs["Station:subs-convert@1"].spec.limits = { budget_usd: 0.25 };
      docs[exampleKey].spec.keep = ["explainer.subtitle"];
      docs[exampleKey].spec.interface.outputs = { original: { from: "explainer.subtitle" } };
    });
    const plan = compileRunPlan(snapshot, EXAMPLE);
    const id = "dub_en.in.subtitle~subs-convert@1";
    const converter = plan.node(id);
    expect(converter).toMatchObject({
      id, kind: "converter", station: "Station:subs-convert@1", implicit: true, budgetUsd: 0.25,
      deps: ["explainer"],
      resolution: {
        line: exampleKey, step: "dub_en", port: "subtitle", from: "subtitle.vtt@1", to: "subtitle.srt@1",
        status: "convert", chain: ["subs-convert@1"], type: "subtitle.srt@1", lossless: true, missing: [],
      },
    });
    expect(plan.node("dub_en").inputs).toContainEqual({ port: "subtitle", source: "node", node: id, outPort: "out" });
    expect(plan.budgetItems).toContainEqual([id, 0.25]);
    expect(plan.outputs.original).toEqual(["explainer", "subtitle"]);
    expect(plan.keep).toEqual([["explainer", "subtitle"]]);
  });

  test("ranks lossless multi-hop paths and honors explicit and none bindings", async () => {
    const base = await loadSnapshot("spec/examples/registry");
    const snapshot = change(base, (docs) => {
      docs["Station:explainer@3"].spec.outputs.subtitle.type = "subtitle.vtt@1";
      const original = docs["Station:subs-convert@1"];
      const bridge = structuredClone(original);
      bridge.metadata.name = "subs-bridge";
      bridge.spec.converts.to = "subtitle.vtt@1";
      bridge.spec.outputs.out.type = "subtitle.vtt@1";
      docs["Station:subs-bridge@1"] = bridge;
      const precise = structuredClone(original);
      precise.metadata.name = "subs-precise";
      precise.spec.converts.preserves = ["has_mark"];
      precise.spec.inputs.in.requires = ["has_mark"];
      docs["Station:subs-precise@1"] = precise;
      bridge.spec.outputs.out.guarantees = ["has_mark"];
      original.spec.converts.lossless = false;
    });

    const auto = compileRunPlan(snapshot, EXAMPLE);
    const prefix = "dub_en.in.subtitle~";
    const upstream = auto.node(prefix + "subs-bridge@1");
    const downstream = auto.node(prefix + "subs-precise@1");
    expect(downstream.deps).toEqual([upstream.id]);
    expect(downstream.resolution).toMatchObject({ chain: ["subs-bridge@1", "subs-precise@1"], lossless: true });
    expect(auto.nodes.indexOf(upstream)).toBeLessThan(auto.nodes.indexOf(downstream));

    const explicit = change(snapshot, (docs) => {
      docs["Station:subs-convert@1"].metadata.lifecycle = "deprecated";
      docs[exampleKey].spec.steps.dub_en.in.subtitle = { from: "explainer.subtitle", via: ["subs-convert@1"] };
    });
    expect(compileRunPlan(explicit, EXAMPLE).node(prefix + "subs-convert@1").resolution)
      .toMatchObject({ chain: ["subs-convert@1"], lossless: false });

    const none = change(snapshot, (docs) => {
      docs[exampleKey].spec.steps.dub_en.in.subtitle = { from: "explainer.subtitle", via: "none" };
    });
    checkError(() => compileRunPlan(none, EXAMPLE), "RUN_PLAN_INVALID", { status: "no-converter", step: "dub_en", port: "subtitle" });
  });

  test("rejects missing inputs, incompatible contracts, cycles and unsupported constructs", async () => {
    const base = await loadSnapshot("tests/fixtures/registry-m2");
    const line = "note-pipeline@1";
    const key = "Line:" + line;
    const missing = change(base, (docs) => { docs[key].spec.steps.upper.in = {}; });
    checkError(() => compileRunPlan(missing, line), "RUN_PLAN_INVALID", { step: "upper", port: "note" });
    const contract = change(base, (docs) => {
      docs[key].spec.interface.inputs.task.requires = [];
    });
    checkError(() => compileRunPlan(contract, line), "RUN_PLAN_INVALID", { step: "write", port: "task", status: "contract" });
    const cycle = change(base, (docs) => { docs[key].spec.steps.upper.in.note = "upper.note"; });
    checkError(() => compileRunPlan(cycle, line), "RUN_PLAN_INVALID");
    for (const construct of ["map", "switch", "until", "subline"]) {
      const dynamic = change(base, (docs) => { docs[key].spec.steps.upper[construct] = {}; });
      checkError(() => compileRunPlan(dynamic, line), "LINE_UNSUPPORTED", { step: "upper", construct });
    }
  });

  test("rejects duplicate chain nodes and converters missing required parameter defaults", async () => {
    const base = await loadSnapshot("tests/fixtures/registry-m2");
    const station = "Station:note-to-text@1";
    const line = "note-pipeline@1";
    const key = "Line:" + line;
    const missing = change(base, (docs) => { docs[station].spec.params = { mode: { type: "string" } }; });
    checkError(() => compileRunPlan(missing, line), "RUN_PLAN_INVALID", { station, param: "mode" });
    const duplicate = change(base, (docs) => {
      docs[key].spec.steps.upper.in.note = { from: "write.note", via: ["note-to-text@1", "note-to-text@1"] };
    });
    checkError(() => compileRunPlan(duplicate, line), "RUN_PLAN_INVALID", { step: "upper", port: "note", chain: ["note-to-text@1", "note-to-text@1"] });
  });
});
