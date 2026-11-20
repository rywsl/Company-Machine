import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { harvest, main } from "../src/legacy/factoryctl";

async function work(body: (paths: { ctl: string; workspace: string; out: string; in: string; root: string }) => Promise<void>): Promise<void> {
  const root = resolve("tmp", "harvest-" + randomUUID());
  const paths = { root, ctl: join(root, "ctl"), workspace: join(root, "workspace"), out: join(root, "out"), in: join(root, "in") };
  for (const directory of Object.values(paths)) mkdirSync(directory, { recursive: true });
  try {
    await body(paths);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

function put(path: string, text: string): void {
  writeFileSync(path, text, "utf8");
}

async function cliHarvest(paths: { ctl: string; workspace: string; out: string; in: string }, outputs: Record<string, unknown>) {
  const variables = { FACTORY_CTL: paths.ctl, FACTORY_WORKSPACE: paths.workspace, FACTORY_OUT: paths.out, FACTORY_IN: paths.in };
  const saved = Object.keys(variables).map((name) => [name, process.env[name]] as const);
  try {
    Object.assign(process.env, variables);
    put(join(paths.ctl, "harvest.json"), JSON.stringify({ outputs }));
    expect(await main(["harvest"])).toBe(0);
    return JSON.parse(readFileSync(join(paths.ctl, "harvest-result.json"), "utf8"));
  } finally {
    for (const [name, value] of saved) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
}

describe("Bun factoryctl harvest", () => {
  test("harvests virtual absolute paths and preserves an output already in place", async () => {
    await work(async (paths) => {
      const drafts = join(paths.workspace, "drafts");
      mkdirSync(drafts);
      put(join(drafts, "a.txt"), "a\nb\n");
      mkdirSync(join(paths.out, "existing"));
      put(join(paths.out, "existing", "note.txt"), "untouched");
      const result = await harvest({ ...paths, request: { outputs: {
        note: { harvest: "/workspace/drafts/*.txt", place_as: "note.txt" },
        existing: { harvest: "/factory/out/existing/note.txt", place_as: "note.txt" },
      } } });
      expect(result).toEqual({ ok: true, ports: {
        existing: { ok: true, files: ["note.txt"], matches: ["/factory/out/existing/note.txt"] },
        note: { ok: true, files: ["note.txt"], matches: ["drafts/a.txt"] },
      } });
      expect(readFileSync(join(paths.out, "note", "note.txt"), "utf8")).toBe("a\nb\n");
      expect(readFileSync(join(paths.out, "existing", "note.txt"), "utf8")).toBe("untouched");
      expect(JSON.parse(readFileSync(join(paths.ctl, "harvest-result.json"), "utf8"))).toEqual(result);
      expect(await cliHarvest(paths, { note: { harvest: "drafts/*.txt", place_as: "note.txt", list: false, optional: false } }))
        .toEqual({ ok: true, ports: { note: { ok: true, files: ["note.txt"], matches: ["drafts/a.txt"] } } });
      expect(readFileSync(join(paths.out, "note", "note.txt"), "utf8")).toBe("a\nb\n");
    });
  });

  test("reports missing and ambiguous outputs, sorts list matches and honors submissions", async () => {
    await work(async (paths) => {
      const drafts = join(paths.workspace, "drafts");
      mkdirSync(drafts);
      put(join(drafts, "z.txt"), "last");
      put(join(drafts, "a.txt"), "first");
      mkdirSync(join(paths.ctl, "submissions"));
      put(join(paths.ctl, "submissions", "submitted.json"), "{}");
      const result = await cliHarvest(paths, {
        listed: { harvest: "drafts/*.txt", list: true, place_as: "note.txt" },
        submitted: { harvest: "missing" },
        ambiguous: { harvest: "drafts/*.txt", place_as: "note.txt" },
        required: { harvest: "drafts/*.pdf" },
        optional: { harvest: "drafts/*.pdf", optional: true },
      });
      expect(result).toEqual({ ok: false, ports: {
        ambiguous: { ok: false, code: "HARVEST_AMBIGUOUS", files: [], matches: ["drafts/a.txt", "drafts/z.txt"] },
        listed: { ok: true, files: ["0/note.txt", "1/note.txt"], matches: ["drafts/a.txt", "drafts/z.txt"] },
        optional: { ok: true, files: [], matches: [] },
        required: { ok: false, code: "HARVEST_EMPTY", files: [], matches: [] },
      } });
      expect(readFileSync(join(paths.out, "listed", "0", "note.txt"), "utf8")).toBe("first");
      expect(readFileSync(join(paths.out, "listed", "1", "note.txt"), "utf8")).toBe("last");
      expect(existsSync(join(paths.out, "ambiguous"))).toBe(false);
    });
  });

  test("keeps virtual source and destination paths inside a job", async () => {
    await work(async (paths) => {
      put(join(paths.workspace, "safe.txt"), "input");
      put(join(paths.root, "private.txt"), "protected");
      const other = join(paths.root, "other");
      mkdirSync(other);
      symlinkSync(other, join(paths.workspace, "linked"), process.platform === "win32" ? "junction" : "dir");
      symlinkSync(other, join(paths.out, "redirected"), process.platform === "win32" ? "junction" : "dir");
      put(join(other, "note.txt"), "protected");
      const result = await harvest({ ...paths, request: { outputs: {
        root: { harvest: "/somewhere/file.txt" },
        escaped: { harvest: "../private.txt" },
        recurse: { harvest: "**/*.txt" },
        pathpart: { harvest: "safe.txt", place_as: "../../private.txt" },
        linked: { harvest: "linked/*.txt" },
        redirected: { harvest: "safe.txt", place_as: "note.txt" },
        "../invalid": { harvest: "safe.txt" },
      } } });
      expect(result.ok).toBe(false);
      expect(Object.values(result.ports).map(({ code }) => code)).toEqual(Array(7).fill("HARVEST_IO"));
      expect(readFileSync(join(paths.root, "private.txt"), "utf8")).toBe("protected");
      expect(readFileSync(join(other, "note.txt"), "utf8")).toBe("protected");
    });
  });

  test("returns the factoryctl exit codes for missing, rejected and unwritable results", async () => {
    await work(async ({ ctl, workspace, out }) => {
      const keys = ["FACTORY_CTL", "FACTORY_WORKSPACE", "FACTORY_OUT"];
      const saved = keys.map((name) => process.env[name]);
      try {
        keys.forEach((name, index) => { process.env[name] = [ctl, workspace, out][index]; });
        expect(await main(["harvest"])).toBe(2);
        put(join(ctl, "harvest.json"), '{"outputs":{"note":{"harvest":"missing.txt"}}}');
        expect(await main(["harvest"])).toBe(0);
        const report = join(ctl, "harvest-result.json");
        expect(JSON.parse(readFileSync(report, "utf8"))).toMatchObject({ ok: false, ports: { note: { code: "HARVEST_EMPTY" } } });
        rmSync(report);
        mkdirSync(report);
        expect(await main(["harvest"])).toBe(1);
      } finally {
        keys.forEach((name, index) => {
          const value = saved[index];
          if (value === undefined) delete process.env[name];
          else process.env[name] = value;
        });
      }
    });
  });
});
