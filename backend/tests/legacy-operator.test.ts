import { describe, expect, test } from "bun:test";
import { parseTranscript, buildInvocation, renderBrief, loadSnapshot } from "../src/legacy";

describe("Bun M1 operator boundary", () => {
  test("renders a brief and script invocation from the Registry snapshot", async () => {
    const snapshot = await loadSnapshot("tests/fixtures/registry-m1");
    const brief = renderBrief(snapshot, "upper@1", { attempt: 1, budgetUsd: 1.5, params: {} });
    expect(brief.factoryContract).toContain("工厂契约");
    const invocation = buildInvocation(snapshot, "upper@1", brief, { params: {} });
    expect(invocation.argv[0]).toBe("upper-text");
    expect(invocation.transcript).toBeUndefined();
  });

  test("parses stream-json transcripts without trusting malformed lines", () => {
    const summary = parseTranscript("tests/fixtures/transcripts/claude-stream.jsonl", "stream-json");
    expect(summary.events).toBeGreaterThan(0);
    expect(summary.malformed).toBe(0);
    expect(summary.costUsd).toBeGreaterThanOrEqual(0);
  });
});
