import { expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { main } from "../src/cli";
import { Context } from "../src/legacy/context";
import { ConversionStore } from "../src/legacy/conversion-requests";
import { FactoryError } from "../src/legacy/errors";
import { loadSnapshot } from "../src/legacy/snapshot";

const rootOf = () => mkdtempSync(join(tmpdir(), "mao-conversion-"));

test("merges conversion sightings, samples and blocked Runs into one durable request", () => {
  const root = rootOf();
  let context = Context.open(root);
  try {
    const requests = new ConversionStore(context);
    const contract = { from: "note.raw@1", to: "note.text@1", guarantees: ["a", "b"], requires: ["non_empty"] };
    const original = requests.openOrMerge(contract, { foundAt: { line: "first" }, sample: "sample-1", runId: "run-1" });
    requests.mark(original.id, "in_progress", "cs-1");
    expect(requests.openOrMerge({ ...contract, guarantees: ["b", "a"] }, { foundAt: { line: "second" }, sample: "sample-1", runId: "run-2" })).toMatchObject({
      id: original.id, status: "in_progress", changeset_id: "cs-1",
      found_at: [{ line: "first" }, { line: "second" }], samples: ["sample-1"], blocked_runs: ["run-1", "run-2"],
    });
    context.close();
    context = Context.open(root);
    expect(new ConversionStore(context).list("in_progress")).toHaveLength(1);
    expect(new ConversionStore(context).get(original.id).guarantees).toEqual(["a", "b"]);
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

test("resolves only contracts satisfied by the activated snapshot and refuses self-resolution", async () => {
  const root = rootOf();
  const context = Context.open(root);
  try {
    const requests = new ConversionStore(context);
    const contract = { from: "note.raw@1", to: "note.text@1", guarantees: ["non_empty"], requires: ["non_empty"] };
    const first = requests.openOrMerge(contract, { foundAt: { line: "supported" } });
    const second = requests.openOrMerge({ ...contract, requires: ["missing_property"] }, { foundAt: { line: "requires-more" } });
    try {
      requests.mark(first.id, "resolved");
      throw new Error("conversion was marked resolved without rollout");
    } catch (error) {
      if (!(error instanceof FactoryError)) throw error;
      expect(error.code).toBe("CONVERSION_STATE_INVALID");
    }
    const snapshot = await loadSnapshot("tests/fixtures/registry-m2");
    expect(requests.resolveMatching("cs-1", snapshot)).toEqual([first.id]);
    expect(requests.get(first.id)).toMatchObject({ status: "resolved", changeset_id: "cs-1" });
    expect(requests.list("open").map(({ id }) => id)).toEqual([second.id]);
    expect(requests.resolveMatching("cs-2", snapshot)).toEqual([]);
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

test("lists conversion requests through the Foreman CLI as portable records", async () => {
  const root = rootOf();
  const context = Context.open(root);
  const log = console.log;
  const output: string[] = [];
  try {
    const requests = new ConversionStore(context);
    const contract = { from: "note.raw@1", to: "note.text@1", guarantees: ["non_empty"], requires: ["non_empty"] };
    const open = requests.openOrMerge(contract, { foundAt: { line: "notes" } });
    const hidden = requests.openOrMerge({ ...contract, requires: [] }, { foundAt: { line: "draft" } });
    requests.mark(hidden.id, "in_progress", "cs-1");
    console.log = (value: string) => { output.push(value); };
    expect(await main(["foreman", "conversion", "ls", "--root", root, "--status", "open", "--json"])).toBe(0);
    expect(JSON.parse(output[0])).toEqual([{
      apiVersion: "mao/v1", kind: "ConversionRequest", metadata: { name: open.id },
      spec: { from: { type: "note.raw@1", guarantees: ["non_empty"] }, to: { type: "note.text@1", requires: ["non_empty"] }, found_at: [{ line: "notes" }], samples: [], blocked_runs: [], status: "open" },
    }]);
  } finally { console.log = log; context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});
