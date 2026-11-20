import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { cpSync, existsSync, mkdirSync, rmSync } from "node:fs";
import { join, resolve } from "node:path";
import { runCommand } from "../runtime/podman";
import { main } from "../src/cli";
import { SqliteStore } from "../src/db";
import { Context } from "../src/legacy/context";
import { tailEvents } from "../src/legacy/events";

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

test("tails read-only factory events in sequence with inclusive time, literal prefix, limit and redaction", async () => {
  const root = resolve("tmp", "event-evidence-" + randomUUID());
  try {
    const run = (args: string[]) => capture(() => main(["--root", root, "--json", "events", ...args]));
    expect(await run([])).toMatchObject({ code: 0, output: ["[]"] });
    expect(existsSync(root)).toBe(false);
    const context = Context.open(root);
    try {
      context.db.exec("CREATE TABLE events (seq INTEGER PRIMARY KEY, id TEXT, at TEXT, kind TEXT, job_id TEXT, data TEXT)");
      const event = context.db.query("INSERT INTO events (id,at,kind,job_id,data) VALUES (?,?,?,?,?)");
      const records = [
        ["ev-t1", "2027-01-01T00:00:00.000Z", "job.state", { to: "RUNNING" }],
        ["ev-t2", "2027-01-01T00:00:01.000Z", "foreman.wake", { note: "token=fixture-key leaked", nested: [{ authorization: "Bearer fixture" }] }],
        ["ev-t3", "2027-01-01T00:00:02.000Z", "foreman.session", { state: "ok" }],
        ["ev-t4", "2027-01-01T00:00:03.000Z", "serve.tick", { tick: 1 }],
      ] as const;
      for (const [id, at, kind, data] of records) event.run(id, at, kind, null, JSON.stringify(data));
    } finally { context.close(); }
    const picked = await run(["--since", "2027-01-01T08:00:01+08:00", "--kind", "foreman."]);
    expect(picked.code).toBe(0);
    const rows = JSON.parse(picked.output[0]);
    expect(rows.map(({ id }: { id: string }) => id)).toEqual(["ev-t2", "ev-t3"]);
    expect(rows[0].data).toEqual({ note: "token=[REDACTED] leaked", nested: [{ authorization: "[REDACTED]" }] });
    expect(tailEvents(root, { since: "2027-01-01T00:00:00.000Z", limit: 2 }).map(({ id }) => id)).toEqual(["ev-t3", "ev-t4"]);
    expect(tailEvents(root, { since: "2027-01-01T00:00:01.000Z", kind: "foreman.", limit: 1 }).map(({ id }) => id)).toEqual(["ev-t3"]);
    expect(tailEvents(root, { kind: "foreman_%" })).toEqual([]);
    for (const query of [{ since: "yesterday" }, { since: "2027-02-30" }, { limit: 0 }, { limit: 10_001 }]) {
      let rejected: unknown;
      try { tailEvents(root, query); } catch (error) { rejected = error; }
      expect(rejected).toMatchObject({ code: "EVENTS_QUERY_INVALID" });
    }
    for (const args of [["--since", "yesterday"], ["--limit", "10001"], ["--limit", "1", "--limit", "2"]]) {
      expect(await run(args)).toMatchObject({ code: 2, output: [] });
    }
    expect(await capture(() => main(["--root", root, "line", "events", "absent"]))).toMatchObject({ code: 0, output: ["[]"] });
  } finally { rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

test("reads job and run evidence through isolated scopes without a Registry", async () => {
  const root = resolve("tmp", "scoped-evidence-" + randomUUID());
  const context = Context.open(root);
  try {
    context.db.exec(`
      CREATE TABLE events (seq INTEGER PRIMARY KEY, id TEXT, at TEXT, kind TEXT, job_id TEXT, data TEXT, run_id TEXT);
      CREATE TABLE job_events (id TEXT, at TEXT, kind TEXT, job_id TEXT, data TEXT);
    `);
    const runEvent = context.db.query("INSERT INTO events (id,at,kind,job_id,data,run_id) VALUES (?,?,?,?,?,?)");
    const jobEvent = context.db.query("INSERT INTO job_events (id,at,kind,job_id,data) VALUES (?,?,?,?,?)");
    const at = "2027-01-01T00:00:01.000Z";
    runEvent.run("e1", at, "run.state", null, "{}", "r1");
    runEvent.run("e2", at, "run.state", null, "{}", "r2");
    jobEvent.run("e3", at, "job.state", "j1", "{}");
    jobEvent.run("e4", at, "job.state", "j2", "{}");
    const command = async (...args: string[]) => {
      const result = await capture(() => main(["--root", root, "--registry", join(root, "missing-registry"), ...args]));
      expect(result.code).toBe(0);
      return JSON.parse(result.output[0]).map(({ id }: { id: string }) => id);
    };
    expect(await command("events")).toEqual(["e1", "e2"]);
    expect(await command("line", "events", "r1")).toEqual(["e1"]);
    expect(await command("job", "events", "j2")).toEqual(["e4"]);
  } finally { context.close(); rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
});

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

  test("checks Registry references without creating factory storage", { timeout: 20_000 }, async () => {
    const root = resolve("tmp", "registry-check-" + randomUUID());
    const registry = join(root, "registry");
    const factory = join(root, "factory");
    const database = join(factory, "factory.db");
    mkdirSync(root, { recursive: true });
    try {
      cpSync("tests/fixtures/registry-m1", registry, { recursive: true });
      const run = () => runCommand([Bun.argv[0], "run", "backend/src/cli.ts", "registry", "check", registry, "--json"], {
        env: { FACTORY_ROOT: factory, FACTORY_DB_PATH: database, BUSINESS_DB_PATH: database },
      });
      const valid = await run();
      expect(valid.exitCode).toBe(0);
      expect(JSON.parse(new TextDecoder().decode(valid.stdout))).toMatchObject({ root: registry, ok: true });

      const station = join(registry, "stations", "upper.yaml");
      const original = await Bun.file(station).text();
      await Bun.write(station, original.replace("machine: toolbox@1", "machine: missing-machine@1"));
      const invalid = await run();
      expect(invalid.exitCode).toBe(1);
      const report = JSON.parse(new TextDecoder().decode(invalid.stdout));
      expect(report).toMatchObject({ root: registry, ok: false });
      expect(report.diagnostics.some((item: { code: string; severity: string }) => item.code === "E-REF" && item.severity === "error")).toBe(true);
      expect(existsSync(factory)).toBe(false);
    } finally { rmSync(root, { recursive: true, force: true, maxRetries: 3 }); }
  });

  test("reports Registry usage errors and rejects a missing or non-directory path", async () => {
    const usage = "factory registry check PATH [--json]";
    expect(await capture(() => main(["registry", "--help"]))).toEqual({ code: 0, output: [usage], errors: [] });
    for (const args of [[], ["unknown"], ["check"], ["check", "--json"],
      ["check", ".", "--wrong"], ["check", ".", "--json", "extra"], ["check", ".", "--json", "--json"]]) {
      const result = await capture(() => main(["registry", ...args]));
      expect(result.code).toBe(2);
      expect(result.output).toEqual([]);
      expect(result.errors[1]).toBe(usage);
    }
    for (const path of [join("tmp", "missing-registry-" + randomUUID()), "package.json"]) {
      const result = await capture(() => main(["registry", "check", path, "--json"]));
      expect(result.code).toBe(1);
      expect(result.errors).toEqual([]);
      expect(JSON.parse(result.output[0])).toMatchObject({
        root: path,
        ok: false,
        diagnostics: [{ code: "E-LAYOUT", severity: "error" }],
      });
    }
  });

  test("registers a repository in the current business and reads a saved graph through the CLI", { timeout: 60_000 }, async () => {
    const root = resolve("tmp", "native-project-register-" + randomUUID());
    const dbPath = join(root, "factory.db");
    const store = new SqliteStore(dbPath, dbPath);
    const run = (...args: string[]) => runCommand([Bun.argv[0], "run", "backend/src/cli.ts", ...args], {
      env: { FACTORY_ROOT: root, FACTORY_DB_PATH: dbPath, BUSINESS_DB_PATH: dbPath },
    });
    const parse = (data: Uint8Array) => JSON.parse(new TextDecoder().decode(data));
    try {
      const system = store.createBusinessSystem({ name: "Existing system" });
      const invalid = await run("project", "register", "--repo", "https://example.test/repo.git?token=fixture");
      expect(invalid.exitCode).toBe(2);
      const missing = await run("project", "register", "--repo", "https://example.test/missing.git", "--system", "nonexistent");
      expect(missing.exitCode).toBe(1);
      expect(store.listProjects()).toHaveLength(0);

      const registered = await run("project", "register", "--repo", "https://example.test/team/video.git", "--system", system.id);
      expect(registered.exitCode).toBe(0);
      const { projectId, systemId } = parse(registered.stdout);
      expect(systemId).toBe(system.id);
      expect(store.getProject(projectId)).toMatchObject({ name: "video", businessSystemId: system.id });
      expect(store.listBusinessSystems()).toHaveLength(1);
      expect(store.listForemanInbox()).toHaveLength(0);

      const graph = { nodes: [{ id: projectId + ":operator" }, { id: projectId + ":machine" }], edges: [] };
      const graphFile = join(root, "update.json");
      await Bun.write(graphFile, JSON.stringify(graph));
      expect((await run("business", "graph", system.id, "--file", graphFile)).exitCode).toBe(0);
      const read = await run("business", "graph", system.id);
      expect(read.exitCode).toBe(0);
      expect(parse(read.stdout)).toEqual(graph);
      expect(store.listForemanInbox()).toHaveLength(0);
      expect((await run("project", "intake", "--repo", "https://example.test/new.git")).exitCode).toBe(0);
      expect(store.listForemanInbox()).toHaveLength(1);
    } finally {
      store.close();
      rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 75 });
    }
  });

  test("binds an Operator through the business CLI and preserves the binding across processes", { timeout: 30_000 }, async () => {
    const root = resolve("tmp", "operator-binding-" + randomUUID());
    mkdirSync(root, { recursive: true });
    const dbPath = join(root, "factory.db");
    const initial = new SqliteStore(dbPath, dbPath);
    const system = initial.createBusinessSystem({ name: "Binding CLI" });
    const project = initial.createIntake({ name: "Project", business_system_id: system.id, repo_url: "https://example.test/project" });
    const unitId = project.id + ":operator";
    initial.close();
    const run = async (args: string[]) => {
      const bun = Bun as unknown as { spawn(argv: string[], options: object): { stdout: ReadableStream; stderr: ReadableStream; exited: Promise<number> } };
      const child = bun.spawn([Bun.argv[0], "run", "backend/src/cli.ts", "business", ...args], {
        env: { ...process.env, FACTORY_ROOT: root, FACTORY_DB_PATH: dbPath, BUSINESS_DB_PATH: dbPath }, stdout: "pipe", stderr: "pipe",
      });
      const [code, body, errors] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()]);
      if (code !== 0) throw new Error(errors);
      return JSON.parse(body);
    };
    try {
      const changed = await run(["bind", system.id, unitId, "--harness", "claude-code", "--model", "custom/model"]);
      expect(changed).toMatchObject({ unit: { id: unitId, harnessRuntime: "claude-code", model: "custom/model" } });
      const units = await run(["units", system.id]) as Array<{ id: string }>;
      expect(units.find(({ id }) => id === unitId)).toMatchObject({ harnessRuntime: "claude-code", model: "custom/model" });
    } finally { rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 75 }); }
  });

  test("imports material, seals a scripted job and returns its lineage through Bun CLI", { timeout: 60_000 }, async () => {
    const root = join("tmp", `station-cli-${randomUUID()}`);
    mkdirSync(root, { recursive: true });
    try {
      const input = join(root, "note.txt");
      const commands = join(root, "commands.json");
      await Bun.write(input, "hello\n");
      await Bun.write(commands, JSON.stringify({
        "upper-text": [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools/upper_text.ts")],
        "count-text": [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools/count_text.ts")],
      }));
      const options = ["--root", root, "--registry", "tests/fixtures/registry-m1", "--provider", "local", "--local-commands", commands, "--json"];
      const imported = await capture(() => main([...options, "bean", "import", "--type", "note.text@1", "--file", `note.txt=${input}`]));
      expect(imported).toMatchObject({ code: 0, errors: [] });
      const { id: beanId } = JSON.parse(imported.output[0]);
      const executed = await capture(() => main([...options.slice(0, -1), "job", "run", "upper@1", "--input", `note=${beanId}`]));
      expect(executed).toMatchObject({ code: 0, errors: [] });
      expect(executed.output).toHaveLength(1);
      expect(executed.output[0]).toMatch(/^note\tsha256:[a-f0-9]{64}$/);
      const outputId = executed.output[0].split("\t")[1];
      expect(outputId).not.toBe(beanId);
      const lineage = await capture(() => main(["--root", root, "bean", "lineage", outputId, "--direction", "up"]));
      expect(lineage.code).toBe(0);
      const upstream = JSON.parse(lineage.output[0]);
      expect(upstream.beans).toEqual([beanId]);
      const shown = await capture(() => main(["job", "show", upstream.jobs[0].jobId, "--root", root]));
      expect(JSON.parse(shown.output[0])).toMatchObject({ state: "SEALED", attempt: 1, cost_usd: 0, outputs: { note: [outputId] } });
      const runAgain = (withCache: boolean) => capture(() => main([...options, "job", "run", "upper@1", "--input", `note=${beanId}`, ...(withCache ? ["--cache"] : [])]));
      const reused = await runAgain(true);
      expect(reused).toMatchObject({ code: 0, errors: [] });
      expect(JSON.parse(reused.output[0])).toMatchObject({ id: upstream.jobs[0].jobId, cache_hit: true });
      const newer = JSON.parse((await runAgain(false)).output[0]);
      expect(newer.id).not.toBe(upstream.jobs[0].jobId);
      const inspectCache = (args: string[]) => capture(() => main(["--root", root, "cache", ...args]));
      const entries = JSON.parse((await inspectCache(["ls", "--station", "upper@1"])).output[0]);
      expect(entries.map(({ jobId }: { jobId: string }) => jobId)).toEqual([upstream.jobs[0].jobId, newer.id]);
      const explained = JSON.parse((await inspectCache(["explain", newer.id])).output[0]);
      expect(explained.hits).toEqual(entries.map(({ jobId }: { jobId: string }) => jobId));
      const invalidated = await inspectCache(["invalidate", "--job", upstream.jobs[0].jobId, "--reason", "acceptance"]);
      expect(invalidated.code).toBe(0);
      expect(JSON.parse((await runAgain(true)).output[0])).toMatchObject({ id: newer.id, cache_hit: true });
    } finally {
      rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    }
  });

  test("executes a durable Line through Bun CLI and exposes its node outputs", { timeout: 60_000 }, async () => {
    const root = join("tmp", `line-cli-${randomUUID()}`);
    const registry = join(root, "registry");
    mkdirSync(root, { recursive: true });
    try {
      cpSync("tests/fixtures/registry-m1", registry, { recursive: true });
      mkdirSync(join(registry, "lines"), { recursive: true });
      const linePath = join(registry, "lines", "uppercase.yaml");
      await Bun.write(linePath, [
        "apiVersion: mao/v1", "kind: Line", "metadata: {name: uppercase, version: 1}", "spec:",
        "  interface:", "    inputs: {note: {type: note.text@1, requires: [non_empty]}}", "    outputs: {result: {type: note.text@1, from: upper.note}}",
        "  budget: {cap_usd: 0}", "  steps:", "    upper: {station: upper@1, in: {note: $inputs.note}}",
      ].join("\n"));
      const note = join(root, "note.txt");
      const commands = join(root, "commands.json");
      await Bun.write(note, "hello");
      await Bun.write(commands, JSON.stringify({
        "upper-text": [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools/upper_text.ts")],
        "count-text": [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools/count_text.ts")],
      }));
      const options = ["--root", root, "--registry", registry, "--provider", "local", "--local-commands", commands, "--json"];
      const plan = await capture(() => main([...options, "line", "plan", "uppercase@1"]));
      expect(plan).toMatchObject({ code: 0, errors: [] });
      expect(JSON.parse(plan.output[0]).outputs).toEqual({ result: ["upper", "note"] });
      const imported = await capture(() => main([...options, "bean", "import", "--type", "note.text@1", "--file", `note.txt=${note}`]));
      expect(imported).toMatchObject({ code: 0, errors: [] });
      const input = JSON.parse(imported.output[0]).id;
      const executed = await capture(() => main([...options, "line", "run", "uppercase@1", "--in", `note=${input}`]));
      expect(executed).toMatchObject({ code: 0, errors: [] });
      const run = JSON.parse(executed.output[0]);
      expect(run).toMatchObject({ state: "SUCCEEDED", spentUsd: 0, reservedUsd: 0 });
      const nodes = await capture(() => main(["--root", root, "line", "nodes", run.id]));
      expect(nodes.code).toBe(0);
      expect(JSON.parse(nodes.output[0])).toMatchObject([{ nodeId: "upper", state: "DONE", outputs: { note: run.outputs.result } }]);
      expect(run.outputs.result[0]).not.toBe(input);
      const reused = await capture(() => main([...options, "line", "run", "uppercase@1", "--in", `note=${input}`]));
      expect(reused).toMatchObject({ code: 0, errors: [] });
      const reusedRun = JSON.parse(reused.output[0]);
      const reusedNodes = await capture(() => main(["--root", root, "line", "nodes", reusedRun.id]));
      expect(reusedRun.outputs).toEqual(run.outputs);
      expect(JSON.parse(reusedNodes.output[0])).toMatchObject([{ nodeId: "upper", state: "DONE", cacheHit: true }]);

      await Bun.write(linePath, (await Bun.file(linePath).text()).replace("cap_usd: 0", "cap_usd: 1"));
      const rebuiltResponse = await capture(() => main([...options, "line", "rebuild", run.id]));
      expect(rebuiltResponse).toMatchObject({ code: 0, errors: [] });
      const rebuilt = JSON.parse(rebuiltResponse.output[0]);
      expect(rebuilt).toMatchObject({ state: "PENDING", rebuiltFrom: run.id, budgetCapUsd: 0, inputs: run.inputs });
      expect(rebuilt.epoch).not.toBe(run.epoch);
      const result = await capture(() => main([...options, "line", "exec", rebuilt.id]));
      expect(JSON.parse(result.output[0])).toMatchObject({ state: "SUCCEEDED", outputs: run.outputs, spentUsd: 0 });
    } finally {
      rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    }
  });

  test("approves and resumes a pending Job through the CLI after its Registry is removed", { timeout: 60_000 }, async () => {
    const root = join("tmp", `approval-cli-${randomUUID()}`);
    const registry = join(root, "registry");
    mkdirSync(root, { recursive: true });
    try {
      cpSync("tests/fixtures/registry-m2", registry, { recursive: true });
      const station = join(registry, "stations", "publish-staged.yaml");
      const spec = await Bun.file(station).text();
      await Bun.write(station, spec.replace(/propose: \{auto_if: .*\}/, "propose: foreman"));
      const note = join(root, "note.txt");
      const commands = join(root, "commands.json");
      const publication = resolve(root, "published");
      await Bun.write(note, "data to publish\n");
      await Bun.write(commands, JSON.stringify(Object.fromEntries(
        ["write-proposal", "count-text", "publish-copy", "local-file-check"].map((name) =>
          [name, [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools", name.replaceAll("-", "_") + ".ts")]]),
      )));
      const cli = async (...args: string[]) => {
        const result = await capture(() => main(["--root", root, "--registry", registry,
          "--provider", "local", "--local-commands", commands, "--json", ...args]));
        expect(result).toMatchObject({ code: 0, errors: [] });
        return JSON.parse(result.output[0]);
      };
      const bean = await cli("bean", "import", "--type", "note.text@1", "--file", `note.txt=${note}`);
      const job = await cli("job", "run", "publish-staged@1", "--input", `note=${bean.id}`, "--param", `dir=${JSON.stringify(publication)}`);
      expect(job.state).toBe("AWAITING_APPROVAL");
      const [approval] = await cli("approval", "ls", "--job", job.id);
      rmSync(registry, { recursive: true });
      expect(await cli("approval", "approve", approval.id)).toMatchObject({ status: "approved" });
      expect(await cli("job", "exec", job.id)).toMatchObject({ state: "SEALED", epoch: job.epoch });
      const [receipt] = await cli("receipt", "ls");
      expect(await cli("receipt", "verify", receipt.id)).toMatchObject({ status: "verified" });
      expect(await Bun.file(join(publication, "note.txt")).text()).toBe("data to publish\n");
    } finally {
      rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    }
  });

  test("advances a triggered Line through the scheduler after the Registry changes", { timeout: 60_000 }, async () => {
    const root = join("tmp", `scheduled-cli-${randomUUID()}`);
    const registry = join(root, "registry");
    mkdirSync(root, { recursive: true });
    try {
      cpSync("tests/fixtures/registry-m2", registry, { recursive: true });
      const note = join(root, "note.txt");
      const config = join(root, "scheduler.json");
      const commands = join(root, "commands.json");
      await Bun.write(note, "hello\n");
      await Bun.write(config, JSON.stringify({ pools: { cpu: 0, memory_mb: 256, gpu: 0 } }));
      await Bun.write(commands, JSON.stringify(Object.fromEntries(
        ["upper-text", "count-text"].map((name) =>
          [name, [Bun.argv[0], join(import.meta.dir, "../../tests/fakes/tools", name.replaceAll("-", "_") + ".ts")]]),
      )));
      const cli = async (...args: string[]) => {
        const result = await capture(() => main(["--root", root, "--registry", registry,
          "--provider", "local", "--local-commands", commands, "--json", ...args]));
        expect(result).toMatchObject({ code: 0, errors: [] });
        return JSON.parse(result.output[0]);
      };
      const bean = await cli("bean", "import", "--type", "note.text@1", "--file", `note.txt=${note}`);
      const context = Context.open(root);
      try { context.warehouse.label(bean.id, "line", "note-trigger", "test"); }
      finally { context.close(); }
      const first = await cli("schedule", "tick", "--config", config);
      expect(first.triggered).toHaveLength(1);
      expect(first.pumped).toEqual([]);
      const [runId] = first.triggered;
      const [node] = await cli("line", "nodes", runId);
      expect(node.state).toBe("SUBMITTED");
      const pending = await cli("job", "show", node.jobId);

      await Bun.write(config, JSON.stringify({ pools: { cpu: 1, memory_mb: 256, gpu: 0 } }));
      const machine = join(registry, "machines", "toolbox", "machine.yaml");
      const original = await Bun.file(machine).text();
      await Bun.write(machine, original.replace("upper-text", "changed-executable"));
      expect(await cli("schedule", "tick", "--config", config)).toMatchObject({ triggered: [], pumped: [node.jobId] });
      expect(await cli("line", "show", runId)).toMatchObject({ state: "SUCCEEDED", epoch: pending.epoch });
      expect(await cli("job", "show", node.jobId)).toMatchObject({ state: "SEALED", epoch: pending.epoch });
    } finally {
      rmSync(root, { recursive: true, force: true, maxRetries: 3 });
    }
  });
});
