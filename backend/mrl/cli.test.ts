import { describe, expect, test } from "bun:test";
import { main } from "./cli";

async function capture(run: () => Promise<number>): Promise<{ code: number; lines: string[] }> {
  const lines: string[] = [];
  const original = console.log;
  console.log = (...values: unknown[]) => lines.push(values.join(" "));
  try {
    return { code: await run(), lines };
  } finally {
    console.log = original;
  }
}

describe("Bun MRL CLI", () => {
  test("evaluates expressions with JSON output", async () => {
    const result = await capture(() => main(["eval", "name", "--env", '{"name":"MAO"}', "--json"]));
    expect(result.code).toBe(0);
    expect(JSON.parse(result.lines.join("\n"))).toMatchObject({ ok: true, value: "MAO" });
  });

  test("renders templates and reports usage errors", async () => {
    const rendered = await capture(() => main(["template", "hello {{ name }}", "--env", '{"name":"MAO"}']));
    expect(rendered.code).toBe(0);
    expect(rendered.lines).toEqual(["hello MAO"]);
    const unknown = await capture(() => main(["missing"]));
    expect(unknown.code).toBe(2);
  });
});
