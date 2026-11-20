import { expect, test } from "bun:test";
import { cpSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { RegistryDiagnostic } from "../mrl/factory/check";
import { main } from "../src/cli";
import { runMrlConformance } from "../../tools/mrl-conformance";

type Report = { ok: boolean; counts: Record<RegistryDiagnostic["severity"], number>; diagnostics: RegistryDiagnostic[] };
type Document = { metadata: { name: string }; spec: Record<string, any> };

async function withRegistry(run: (registry: string) => Promise<void>): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), "mao-admit-static-"));
  try {
    const registry = join(root, "registry");
    cpSync("spec/examples/registry", registry, { recursive: true });
    await run(registry);
  } finally {
    rmSync(root, { recursive: true, force: true, maxRetries: 3 });
  }
}

async function updateDocument(registry: string, file: string, change: (document: Document) => void, destination = file) {
  const document = Bun.YAML.parse(await Bun.file(join(registry, file)).text()) as Document;
  change(document);
  await Bun.write(join(registry, destination), Bun.YAML.stringify(document));
}

async function check(registry: string, ...options: string[]): Promise<{ code: number; report: Report }> {
  const output: string[] = [];
  const log = console.log;
  try {
    console.log = (...values: unknown[]) => output.push(values.join(" "));
    const code = await main(["registry", "check", registry, "--json", ...options]);
    if (output.length !== 1) throw new Error(`Expected one Registry JSON report, got exit ${code} and ${output.length} lines`);
    return { code, report: JSON.parse(output[0]) as Report };
  } finally {
    console.log = log;
  }
}

const dub = "stations/dub.yaml";
const dubMachine = "machines/openmontage/machine.yaml";
const matchingFacts = "inputs.subtitle.facts.last_cue_end_s <= inputs.video.facts.duration_s + 1.0";
const problems = (report: Report) => report.diagnostics.filter(({ severity }) => severity !== "info");

test("Registry admits typed cross-port expressions in strict mode without warnings", async () => {
  await withRegistry(async (registry) => {
    await updateDocument(registry, dub, ({ spec }) => { spec.admit = [matchingFacts, "params.target_language != ''"]; });
    const { code, report } = await check(registry, "--strict");
    expect(code).toBe(0);
    expect(report.ok).toBe(true);
    expect(report.counts).toEqual({ error: 0, warning: 0, info: report.diagnostics.length });
    expect(problems(report)).toEqual([]);
  });
});

test("Registry locates unknown admission facts, unknown ports, and non-boolean expressions", async () => {
  await withRegistry(async (registry) => {
    await updateDocument(registry, dub, ({ spec }) => {
      spec.admit = [
        "inputs.subtitle.facts.last_cue <= inputs.video.facts.duration_s",
        "inputs.audio.facts.duration_s > 0.0",
        "inputs.video.facts.duration_s",
      ];
    });
    const { code, report } = await check(registry);
    expect(code).toBe(1);
    expect(report.ok).toBe(false);
    const diagnostics = problems(report).sort((a, b) => a.subject.localeCompare(b.subject));
    expect(diagnostics).toMatchObject([0, 1, 2].map((index) => ({
      code: "E-EXPR", severity: "error", subject: `Station:dub@2/admit[${index}]`, file: dub, path: `spec.admit[${index}]`,
    })));
    expect(diagnostics[0].message).toContain("MRL-NO-FIELD");
    expect(diagnostics[1].message).toContain("MRL-NO-FIELD");
    expect(diagnostics[2].message).toContain("MRL-TYPE");
  });
});

test("Registry type-checks admission parameter names and list element facts", async () => {
  await withRegistry(async (registry) => {
    await updateDocument(registry, dub, ({ spec }) => { spec.admit = ["params.target_langauge != ''"]; });
    await updateDocument(registry, dub, ({ metadata, spec }) => {
      metadata.name = "admit-many";
      spec.recipe = "admit-many";
      spec.inputs.subtitle = { type: "list<subtitle.srt@1>" };
      spec.admit = ["inputs.subtitle.all(item, item.facts.last_cue_end > 0.0)"];
    }, "stations/admit-many.yaml");
    await updateDocument(registry, dubMachine, ({ spec }) => {
      const dubRecipe = spec.recipes["localization-dub"];
      spec.recipes["admit-many"] = {
        ...dubRecipe, inputs: { ...dubRecipe.inputs, subtitle: { type: "list<subtitle.srt@1>" } },
      };
    });
    const { code, report } = await check(registry);
    expect(code).toBe(1);
    const diagnostics = problems(report).sort((a, b) => a.subject.localeCompare(b.subject));
    expect(diagnostics).toMatchObject([
      { code: "E-EXPR", subject: "Station:admit-many@2/admit[0]" },
      { code: "E-EXPR", subject: "Station:dub@2/admit[0]" },
    ]);
    for (const diagnostic of diagnostics) expect(diagnostic.message).toContain("MRL-NO-FIELD");
  });
});

test("Registry reports a metadata admission warning and strict mode rejects it", async () => {
  await withRegistry(async (registry) => {
    await updateDocument(registry, dub, ({ spec }) => {
      spec.admit = ["has(inputs.video.meta.language) && inputs.video.meta.language == params.target_language", matchingFacts];
    });
    const { code, report } = await check(registry);
    expect(code).toBe(0);
    expect(report.counts).toMatchObject({ error: 0, warning: 1 });
    expect(problems(report)).toMatchObject([{
      code: "W-ADMIT-META", severity: "warning", subject: "Station:dub@2/admit[0]", file: dub, path: "spec.admit[0]",
    }]);
    const strict = await check(registry, "--strict");
    expect(strict.code).toBe(1);
    expect(strict.report).toEqual(report);
  });
});

test("Registry warns on optional admission inputs only when the expression lacks a guard", async () => {
  await withRegistry(async (registry) => {
    await updateDocument(registry, dubMachine, ({ spec }) => { spec.recipes["localization-dub"].inputs.subtitle.optional = true; });
    await updateDocument(registry, dub, ({ spec }) => {
      spec.inputs.subtitle.optional = true;
      spec.admit = [matchingFacts, "!has(inputs.subtitle) || " + matchingFacts];
    });
    const { code, report } = await check(registry);
    expect(code).toBe(0);
    const diagnostics = problems(report);
    expect(diagnostics).toMatchObject([{
      code: "W-EXPR", severity: "warning", subject: "Station:dub@2/admit[0]", path: "spec.admit[0]",
    }]);
    expect(diagnostics[0].message).toContain("MRL-MAYBE-ABSENT");
  });
});

test("Registry locates the admission error on a Source with no input ports", async () => {
  await withRegistry(async (registry) => {
    const file = "stations/intake-brief.yaml";
    await updateDocument(registry, file, ({ spec }) => { spec.admit = ["true"]; });
    const { code, report } = await check(registry);
    expect(code).toBe(1);
    expect(problems(report)).toMatchObject([{
      code: "E-PORT", severity: "error", subject: "Station:intake-brief@1/admit", file, path: "spec.admit",
      message: "没有输入端口的工位不能写 admit",
    }]);
  });
});

test("Registry rejects empty, non-string, and non-array admission values at their schema paths", async () => {
  await withRegistry(async (registry) => {
    for (const [value, path] of [[[], "spec.admit"], [[1], "spec.admit[0]"], [matchingFacts, "spec.admit"]]) {
      await updateDocument(registry, dub, ({ spec }) => { spec.admit = value; });
      const { code, report } = await check(registry);
      expect(code).toBe(1);
      const schema = report.diagnostics.filter((item) => item.code === "E-SCHEMA" && item.file === dub);
      expect(schema).toMatchObject([{ path }]);
    }
  });
});

test("Bun runs every installed admission conformance case from the archived suite", async () => {
  const root = resolve(".");
  const installed = join(root, "spec/mrl/conformance/factory/admit.yaml");
  const archived = join(root, "removed/python/tests/acceptance/m14/admit.conformance.yaml");
  const read = async (file: string) => (await Bun.file(file).text()).replaceAll("\r\n", "\n");
  const [suite, original] = await Promise.all([read(installed), read(archived)]);
  expect(suite).toBe(original);
  const result = runMrlConformance(root, new Set(), [installed]);
  expect(result.suites).toBe(1);
  expect(result.counts.factory).toBeGreaterThanOrEqual(6);
  expect(result.passed).toEqual(result.counts);
  expect(result.failures).toEqual([]);
});
