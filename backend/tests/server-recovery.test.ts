import { expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { SqliteStore } from "../src/db";
import type { OfficialProcess } from "../src/harness";
import { createApp, startServer } from "../src/server";
import { VaultStore } from "../src/vault";

function fixtureProcess(text: string, outputRef?: string): OfficialProcess {
  const ref = outputRef ?? `material://recovery-${text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "result"}`;
  const bytes = new TextEncoder().encode([
    JSON.stringify({ type: "mao.record", record_type: "material", kind: "recovery.result", ref, summary: text }),
    JSON.stringify({ type: "result", text }),
    "",
  ].join("\n"));
  return {
    stdout: new ReadableStream({ start(controller) { controller.enqueue(bytes); controller.close(); } }),
    kill() {},
  };
}

async function waitFor(condition: () => boolean) {
  const deadline = Date.now() + 2_000;
  while (!condition() && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  expect(condition()).toBe(true);
}

test("dispatches a business run queued by the separate factory CLI", { timeout: 15_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-cli-dispatch-"));
  const dbPath = join(root, "factory.db");
  const calls: string[] = [];
  const app = startServer({
    port: 0, dbPath, legacyPath: dbPath,
    operator: { spawn: (argv) => { calls.push(argv.join("\n")); return fixtureProcess("Completed queued work"); } },
  });
  try {
    app.store.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "", model: "fixture", mode: "session" });
    const system = app.store.createBusinessSystem({ name: "CLI dispatch" });
    app.store.createIntake({ business_system_id: system.id, name: "Project" });
    const bun = Bun as unknown as { spawn(argv: string[], options: object): { stdout: ReadableStream; stderr: ReadableStream; exited: Promise<number> } };
    const child = bun.spawn([Bun.argv[0], join(import.meta.dir, "../src/cli.ts"), "business", "run", system.id, "--input", "cas://trigger"], {
      env: { ...process.env, FACTORY_ROOT: root, FACTORY_DB_PATH: dbPath, BUSINESS_DB_PATH: dbPath }, stdout: "pipe", stderr: "pipe",
    });
    const [exit, stdout, stderr] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()]);
    if (exit !== 0) throw new Error(stderr);
    const run = JSON.parse(stdout) as { id: string };
    await waitFor(() => app.store.getBusinessRun(run.id)?.status === "completed");
    expect(calls).toHaveLength(1);
    expect(calls[0]).toContain("cas://trigger");
  } finally {
    await app.operator.close();
    app.stop();
    await rm(root, { recursive: true, force: true });
  }
});

test("serving resumes queued work from SQLite without replaying unresolved work", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-server-recovery-"));
  const dbPath = join(root, "business.db");
  const seed = createApp({ dbPath, legacyPath: dbPath });
  let resumed: ReturnType<typeof startServer> | undefined;

  try {
    for (const role of ["foreman", "operator"] as const) {
      seed.store.saveHarnessProfile({ runtime: "codex", role, baseUrl: "", model: "fixture", mode: "session" });
    }
    const queued = seed.store.queueForemanMessage("queued-turn", "Continue queued turn");
    const claimed = seed.store.queueForemanMessage("claimed-turn", "Do not repeat claimed turn");
    seed.store.claimForemanMessage(claimed.id);

    const system = seed.store.createBusinessSystem({ name: "Single-operator system" });
    const project = seed.store.createIntake({ business_system_id: system.id, name: "Single operator" });
    const authorized = seed.store.createBusinessRun(system.id, "test", {}, ["cas://authorized"], true)!;
    const waiting = seed.store.createBusinessRun(system.id, "test", {}, ["cas://waiting"], true)!;
    const waitingUnit = seed.store.claimUnitRun(waiting.id, `${project.id}:operator`, "waiting-session")!;
    seed.store.completeUnitRun(waitingUnit.id, "waiting_resources");

    const deferredResponse = await seed.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/runs`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ dispatch: false, inputs: ["cas://deferred"] }),
    }));
    expect(deferredResponse.status).toBe(201);
    const deferred = await deferredResponse.json() as { id: string };

    const dag = seed.store.createBusinessSystem({ name: "Two-operator system" });
    const upstream = seed.store.createIntake({ business_system_id: dag.id, name: "Upstream operator" });
    const downstream = seed.store.createIntake({ business_system_id: dag.id, name: "Downstream operator" });
    const source = `${upstream.id}:operator`;
    const target = `${downstream.id}:operator`;
    seed.store.updateBusinessSystemMetadata(dag.id, { graph: { edges: [{ source, target }] } });
    const interrupted = seed.store.createBusinessRun(dag.id, "test", {}, ["cas://interrupted"], true)!;
    seed.store.claimUnitRun(interrupted.id, source, "interrupted-session");
    const progressed = seed.store.createBusinessRun(dag.id, "historical")!;
    const completed = seed.store.claimUnitRun(progressed.id, source, "completed-session")!;
    seed.store.completeUnitRun(completed.id, "completed", ["cas://completed-upstream"]);

    seed.stop();
    const foremanCalls: string[] = [];
    const operatorCalls: string[] = [];
    resumed = startServer({
      port: 0, dbPath, legacyPath: dbPath,
      foreman: { spawn: (argv) => { foremanCalls.push(argv.join("\n")); return fixtureProcess("Recovered reply"); } },
      operator: { spawn: (argv) => { operatorCalls.push(argv.join("\n")); return fixtureProcess("Recovered run"); } },
    });
    const store = resumed.store;
    expect((await fetch(new URL("/healthz", resumed.server.url))).status).toBe(200);
    await waitFor(() => store.getForemanMessage(queued.id)?.status === "done"
      && store.getBusinessRun(authorized.id)?.status === "completed"
      && store.getBusinessRun(progressed.id)?.status === "completed");

    expect(foremanCalls).toHaveLength(1);
    expect(foremanCalls[0]).toContain("Continue queued turn");
    expect(store.getForemanMessage(claimed.id)?.status).toBe("claimed");
    expect(operatorCalls).toHaveLength(2);
    expect(operatorCalls.some((task) => task.includes("cas://authorized"))).toBe(true);
    expect(operatorCalls.some((task) => task.includes("cas://completed-upstream"))).toBe(true);
    expect(store.listUnitRuns(interrupted.id).find(({ unitId }) => unitId === source)).toMatchObject({ status: "running", sessionId: "interrupted-session" });
    expect(store.listUnitRuns(interrupted.id).find(({ unitId }) => unitId === target)?.status).toBe("queued");
    expect(store.listUnitRuns(waiting.id)[0]?.status).toBe("waiting_resources");
    expect(store.getBusinessRun(deferred.id)?.status).toBe("queued");

    const dispatch = await fetch(new URL(`/api/v1/runs/${deferred.id}/dispatch`, resumed.server.url), { method: "POST" });
    expect(dispatch.status).toBe(202);
    await waitFor(() => store.getBusinessRun(deferred.id)?.status === "completed");
    expect(operatorCalls).toHaveLength(3);
    expect(operatorCalls[2]).toContain("cas://deferred");
  } finally {
    if (resumed) {
      await resumed.operator.close();
      await resumed.foreman.close();
      resumed.stop();
    }
    seed.stop();
    await rm(root, { recursive: true, force: true });
  }
});

test("recovers only authorized resource waiters with fully available project requests", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-resource-recovery-"));
  const dbPath = join(root, "factory.db");
  const vaultPath = join(root, "vault.json");
  const keyPath = join(root, "vault.key");
  const vault = new VaultStore(vaultPath, keyPath);
  const seed = new SqliteStore(dbPath, dbPath);
  let resumed: ReturnType<typeof startServer> | undefined;
  try {
    seed.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "", model: "fixture", mode: "session" });
    const system = seed.createBusinessSystem({ name: "Resource recovery" });
    const project = seed.createIntake({ business_system_id: system.id, name: "Waiting project" });
    const other = seed.createIntake({ name: "Unrelated project" });
    const available = seed.createSecretRequest({ projectId: project.id, name: "PROJECT_API_KEY", vaultRef: "vault://fixture/project" });
    const unrelated = seed.createSecretRequest({ projectId: other.id, name: available.name, vaultRef: "vault://fixture/unrelated" });
    for (const resource of [available, unrelated]) {
      vault.put(resource.vaultRef, resource === available ? "only-this-project" : "unrelated-value");
      seed.markSecretAvailable(resource.id);
    }
    const requested = seed.createSecretRequest({ projectId: project.id, name: "SECOND_API_KEY", vaultRef: "vault://fixture/pending" });
    vault.put(requested.vaultRef, "submitted-without-sqlite-commit");
    function waiter(requestIds: string[], authorized = true) {
      const run = seed.createBusinessRun(system.id, "test", {}, [], authorized)!;
      const unit = seed.claimUnitRun(run.id, `${project.id}:operator`, "previous-session")!;
      for (const requestId of requestIds) seed.linkUnitSecret(unit.id, requestId);
      seed.completeUnitRun(unit.id, "waiting_resources", ["material://previous-output"], "previous-error");
      return { runId: run.id, unitId: unit.id };
    }
    const ready = waiter([available.id]);
    const stillWaiting = [
      waiter([]), waiter([available.id, requested.id]), waiter([available.id, "missing-request"]),
      waiter([available.id, unrelated.id]), waiter([available.id], false),
    ];
    seed.close();

    const launches: Record<string, string>[] = [];
    resumed = startServer({
      port: 0, dbPath, legacyPath: dbPath, storageRoot: root,
      vault: new VaultStore(vaultPath, keyPath),
      operator: { spawn: (_argv, { env }) => { launches.push(env); return fixtureProcess("Recovered resource waiter", "material://previous-output"); } },
    });
    const app = resumed;
    await waitFor(() => app.store.getBusinessRun(ready.runId)?.status === "completed");
    expect(launches).toEqual([{ PROJECT_API_KEY: "only-this-project" }]);
    expect(app.store.getUnitRun(ready.runId, ready.unitId)).toMatchObject({ status: "completed", outputs: ["material://previous-output"] });
    expect(app.store.getUnitRun(ready.runId, ready.unitId)?.error).toBeUndefined();
    expect(app.store.getSecretRequest(requested.id)?.status).toBe("requested");
    for (const { runId, unitId } of stillWaiting) {
      expect(app.store.getUnitRun(runId, unitId)).toMatchObject({ status: "waiting_resources", sessionId: "previous-session" });
    }
    app.recoverQueuedWork();
    await Promise.resolve();
    expect(launches).toHaveLength(1);
    expect(app.events.after().filter(({ type }) => type === "secret.available")).toEqual([]);
  } finally {
    if (resumed) {
      await resumed.operator.close();
      await resumed.foreman.close();
      resumed.stop();
    }
    seed.close();
    await rm(root, { recursive: true, force: true });
  }
});

test("stopping during startup readiness leaves the pending work durable", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-server-stop-"));
  const dbPath = join(root, "business.db");
  const seed = new SqliteStore(dbPath, dbPath);
  const image = `registry.example/harness@sha256:${"a".repeat(64)}`;
  for (const role of ["foreman", "operator"] as const) {
    seed.saveHarnessProfile({ runtime: "codex", role, baseUrl: "", model: "fixture", mode: "session", image });
  }
  const envelope = seed.queueForemanMessage("queued", "Wait for container runtime");
  const system = seed.createBusinessSystem({ name: "Stopping system" });
  seed.createIntake({ business_system_id: system.id, name: "Wait for container runtime" });
  const run = seed.createBusinessRun(system.id, "test", {}, [], true)!;
  seed.close();

  const health = { available: true, platform: "linux", executable: "kiri", machine: "linux-host", rootless: null };
  let releaseRuntime = () => {};
  const readiness = new Promise<void>((resolve) => { releaseRuntime = resolve; });
  let ensures = 0;
  let launches = 0;
  const app = startServer({
    port: 0, dbPath, legacyPath: dbPath,
    runtime: {
      health: async () => health,
      ensureReady: async () => { ensures += 1; await readiness; return health; },
    },
    foreman: { spawn: () => { launches += 1; return fixtureProcess("Unexpected Foreman session"); } },
    operator: { spawn: () => { launches += 1; return fixtureProcess("Unexpected Operator session"); } },
  });
  try {
    await waitFor(() => ensures === 1);
    await Promise.resolve();
    app.stop();
    releaseRuntime();
    await Promise.all([app.foreman.close(), app.operator.close()]);
    expect(launches).toBe(0);
    const reopened = new SqliteStore(dbPath, dbPath);
    try {
      expect(reopened.getForemanMessage(envelope.id)?.status).toBe("queued");
      expect(reopened.listUnitRuns(run.id)[0]?.status).toBe("queued");
    } finally {
      reopened.close();
    }
  } finally {
    releaseRuntime();
    app.stop();
    await rm(root, { recursive: true, force: true });
  }
});

test("keeps containerized Operators queued offline and resumes their DAG once after explicit readiness", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-runtime-recovery-"));
  const dbPath = join(root, "factory.db");
  const seed = new SqliteStore(dbPath, dbPath);
  let app: ReturnType<typeof startServer> | undefined;
  let settleNative = () => {};
  const image = `registry.example/harness@sha256:${"a".repeat(64)}`;
  try {
    seed.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "", model: "fixture", mode: "session", image });
    seed.saveHarnessProfile({ runtime: "claude-code", role: "operator", baseUrl: "", model: "fixture", mode: "session" });
    const system = seed.createBusinessSystem({ name: "Runtime recovery" });
    const source = seed.createIntake({ business_system_id: system.id, name: "Container source" });
    const target = seed.createIntake({ business_system_id: system.id, name: "Container downstream" });
    const independent = seed.createIntake({ business_system_id: system.id, name: "Native branch" });
    const sourceId = `${source.id}:operator`, targetId = `${target.id}:operator`, independentId = `${independent.id}:operator`;
    seed.updateOperatorBinding(system.id, sourceId, { harnessRuntime: "codex" });
    seed.updateOperatorBinding(system.id, targetId, { harnessRuntime: "codex" });
    seed.updateOperatorBinding(system.id, independentId, { harnessRuntime: "claude-code" });
    seed.updateBusinessSystemMetadata(system.id, { graph: { edges: [{ source: sourceId, target: targetId }] } });
    const run = seed.createBusinessRun(system.id, "test", {}, [], true)!;
    const deferred = seed.createBusinessRun(system.id, "manual", {}, [], false)!;
    seed.close();

    let online = false;
    let readinessChecks = 0;
    let containerLaunches = 0;
    const health = () => ({ available: online, platform: "linux", executable: "kiri", machine: "linux-host", rootless: null, reason: online ? undefined : "offline" });
    app = startServer({
      port: 0, dbPath, legacyPath: dbPath,
      runtime: { health: async () => health(), ensureReady: async () => { readinessChecks++; return health(); } },
      operator: { spawn: (_argv, options) => {
        if (options.containerImage) {
          containerLaunches++;
          return fixtureProcess("Container turn completed");
        }
        return {
          stdout: new ReadableStream<Uint8Array>({ start(controller) {
            settleNative = () => {
              controller.enqueue(new TextEncoder().encode([
                JSON.stringify({ type: "mao.record", record_type: "material", kind: "recovery.result", ref: "material://native-complete", summary: "Native turn completed" }),
                JSON.stringify({ type: "result", text: "Native turn completed" }),
                "",
              ].join("\n")));
              controller.close();
            };
          } }),
          kill() {},
        };
      } },
    });
    const { store } = app;
    const statusOf = (unitId: string) => store.listUnitRuns(run.id).find(({ unitId: id }) => id === unitId)?.status;
    await waitFor(() => readinessChecks === 1 && statusOf(independentId) === "running");
    await new Promise((resolve) => setTimeout(resolve, 35));
    expect(readinessChecks).toBe(1);
    expect(containerLaunches).toBe(0);
    expect(statusOf(sourceId)).toBe("queued");
    expect(statusOf(targetId)).toBe("queued");
    expect(app.events.after().filter(({ type }) => type === "operator.run.failed")).toEqual([]);

    online = true;
    const readinessUrl = new URL("/api/v1/runtime/ensure-ready", app.server.url);
    const checks = await Promise.all([fetch(readinessUrl, { method: "POST" }), fetch(readinessUrl, { method: "POST" })]);
    expect(checks.map(({ status }) => status)).toEqual([200, 200]);
    await waitFor(() => statusOf(sourceId) === "completed" && statusOf(targetId) === "completed");
    expect(containerLaunches).toBe(2);
    expect(statusOf(independentId)).toBe("running");
    expect(store.listUnitRuns(deferred.id).every(({ status }) => status === "queued")).toBe(true);
    settleNative();
    settleNative = () => {};
    await waitFor(() => store.getBusinessRun(run.id)?.status === "completed");
  } finally {
    settleNative();
    if (app) { await app.operator.close(); app.stop(); }
    seed.close();
    await rm(root, { recursive: true, force: true });
  }
});

test("exhausting the startup deadline leaves an Operator queued and ignores late readiness", async () => {
  const root = mkdtempSync(join(tmpdir(), "mao-runtime-deadline-"));
  const dbPath = join(root, "factory.db");
  const seed = new SqliteStore(dbPath, dbPath);
  let app: ReturnType<typeof startServer> | undefined;
  let resolveReady = () => {};
  try {
    seed.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "", model: "fixture", mode: "session",
      image: `registry.example/harness@sha256:${"a".repeat(64)}`, sessionTimeoutSeconds: 1 });
    const system = seed.createBusinessSystem({ name: "Startup deadline" });
    seed.createIntake({ business_system_id: system.id, name: "Queued Operator" });
    const run = seed.createBusinessRun(system.id, "test", {}, [], true)!;
    seed.close();

    const health = { available: true, platform: "linux", executable: "kiri", machine: "linux-host", rootless: null };
    const ready = new Promise<void>((resolve) => { resolveReady = resolve; });
    let checks = 0;
    let launches = 0;
    app = startServer({
      port: 0, dbPath, legacyPath: dbPath,
      runtime: { health: async () => health, ensureReady: async () => { checks++; await ready; return health; } },
      operator: { spawn: () => { launches++; return fixtureProcess("Container turn completed"); } },
    });
    await waitFor(() => checks === 1);
    await app.operator.dispatch(run.id);
    expect(app.store.listUnitRuns(run.id)[0]?.status).toBe("queued");
    resolveReady();
    await new Promise((resolve) => setTimeout(resolve, 15));
    expect(launches).toBe(0);
    expect((await fetch(new URL("/api/v1/runtime/ensure-ready", app.server.url), { method: "POST" })).status).toBe(200);
    const store = app.store;
    await waitFor(() => store.getBusinessRun(run.id)?.status === "completed");
    expect(launches).toBe(1);
  } finally {
    resolveReady();
    if (app) { await app.operator.close(); app.stop(); }
    seed.close();
    await rm(root, { recursive: true, force: true });
  }
});
