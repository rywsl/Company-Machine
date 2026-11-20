import { describe, expect, test } from "bun:test";
import { main } from "../src/cli";

async function capture(run: () => Promise<number>): Promise<{ code: number; output: string[]; errors: string[] }> {
  const output: string[] = [];
  const errors: string[] = [];
  const log = console.log;
  const error = console.error;
  console.log = (...values: unknown[]) => output.push(values.join(" "));
  console.error = (...values: unknown[]) => errors.push(values.join(" "));
  try {
    return { code: await run(), output, errors };
  } finally {
    console.log = log;
    console.error = error;
  }
}

describe("Bun factory CLI", () => {
  test("provides scoped help for active business commands", async () => {
    const project = await capture(() => main(["project", "help"]));
    const business = await capture(() => main(["business", "help"]));
    const foreman = await capture(() => main(["foreman", "help"]));
    const runtime = await capture(() => main(["runtime", "help"]));
    expect(project.code).toBe(0);
    expect(project.output.join("\n")).toContain("intake");
    expect(business.output.join("\n")).toContain("conversion");
    expect(foreman.output.join("\n")).toContain("inbox");
    expect(runtime.output.join("\n")).toContain("ensure-ready");
  });

  test("returns stable usage errors instead of exiting during import", async () => {
    const unknown = await capture(() => main(["missing"]));
    const missingSource = await capture(() => main(["project", "intake"]));
    expect(unknown.code).toBe(2);
    expect(unknown.errors[0]).toContain("unknown factory command");
    expect(missingSource.code).toBe(2);
    expect(missingSource.errors[0]).toContain("one source is required");
  });

  test("runs registry checks through the Bun CLI", async () => {
    const result = await capture(() => main(["registry", "check", "tests/fixtures/registry-m1", "--json"]));
    expect(result.code).toBe(0);
    expect(JSON.parse(result.output.join("\n"))).toMatchObject({ ok: true });
  });
});
