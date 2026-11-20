import { expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { SqliteStore } from "../src/db";
import type { OfficialProcess } from "../src/harness";
import { createApp, startServer } from "../src/server";

function fixtureProcess(text: string): OfficialProcess {
  const bytes = new TextEncoder().encode(JSON.stringify({ type: "result", text }) + "\n");
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
      expect(reopened.listUnitRuns(run.id)[0]?.status).toBe("running");
    } finally {
      reopened.close();
    }
  } finally {
    releaseRuntime();
    app.stop();
    await rm(root, { recursive: true, force: true });
  }
});
