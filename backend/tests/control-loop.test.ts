import { expect, test } from "bun:test";
import { createApp } from "../src/server";
import type { OfficialProcess } from "../src/harness";

function completedProcess(ref: string): OfficialProcess {
  const lines = [
    { type: "mao.record", record_type: "material", kind: "build.report", ref, title: "构建报告", summary: "重试后完成" },
    { type: "result", text: "完成" },
  ];
  const body = new TextEncoder().encode(lines.map((line) => JSON.stringify(line)).join("\n") + "\n");
  return { stdout: new ReadableStream({ start(controller) { controller.enqueue(body); controller.close(); } }), kill() {} };
}

test("cancelling a running unit stops its Harness and retry starts one fresh attempt", async () => {
  let launches = 0;
  let kills = 0;
  let started!: () => void;
  const firstStarted = new Promise<void>((resolve) => { started = resolve; });
  let closeFirst!: () => void;
  const app = createApp({
    dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false,
    operator: {
      spawn: () => {
        launches += 1;
        if (launches === 1) {
          started();
          return {
            stdout: new ReadableStream<Uint8Array>({ start(controller) { closeFirst = () => controller.close(); } }),
            kill: () => { kills += 1; closeFirst(); },
          } satisfies OfficialProcess;
        }
        return completedProcess("material://retry-output");
      },
    },
  });
  try {
    const headers = { "content-type": "application/json" };
    const system = await (await app.fetch(new Request("http://localhost/api/v1/business-systems", {
      method: "POST", headers, body: JSON.stringify({ name: "控制闭环" }),
    }))).json() as { id: string };
    const project = app.store.createIntake({ business_system_id: system.id, name: "项目", repo_url: "https://example.test/retry.git" });
    app.store.saveHarnessProfile({ runtime: "codex", role: "operator", baseUrl: "", model: "fixture", mode: "session" });
    const run = await (await app.fetch(new Request(`http://localhost/api/v1/business-systems/${system.id}/runs`, {
      method: "POST", headers, body: JSON.stringify({ dispatch: false }),
    }))).json() as { id: string };
    void app.operator.dispatch(run.id);
    await firstStarted;

    const cancelled = await app.fetch(new Request(`http://localhost/api/v1/runs/${run.id}/cancel`, { method: "POST" }));
    expect(cancelled.status).toBe(200);
    expect(app.store.getBusinessRun(run.id)?.status).toBe("cancelled");
    expect(kills).toBe(1);

    const retried = await app.fetch(new Request(`http://localhost/api/v1/runs/${run.id}/retry`, { method: "POST" }));
    app.store.listReadyUnitRuns(run.id);
    await app.operator.dispatch(run.id);
    expect(retried.status).toBe(200);
    for (let attempt = 0; attempt < 50 && app.store.getBusinessRun(run.id)?.status !== "completed"; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 5));
    expect(launches).toBe(2);
    expect(app.store.getBusinessRun(run.id)?.status).toBe("completed");
    expect(app.store.listArtifacts(project.id).map((item) => item.ref)).toEqual(["material://retry-output"]);
  } finally {
    app.stop();
  }
});

test("CORS headers are attached only to the configured origin on JSON and SSE responses", async () => {
  const previous = process.env.MAO_CORS_ORIGIN;
  process.env.MAO_CORS_ORIGIN = "https://console.example";
  const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", dispatchWork: false });
  try {
    const allowed = await app.fetch(new Request("http://localhost/api/v1/business-systems", { headers: { origin: "https://console.example" } }));
    expect(allowed.headers.get("access-control-allow-origin")).toBe("https://console.example");
    const blocked = await app.fetch(new Request("http://localhost/api/v1/business-systems", { headers: { origin: "https://attacker.example" } }));
    expect(blocked.headers.get("access-control-allow-origin")).toBeNull();
    const preflight = await app.fetch(new Request("http://localhost/api/v1/events", { method: "OPTIONS", headers: { origin: "https://console.example" } }));
    expect(preflight.headers.get("access-control-allow-origin")).toBe("https://console.example");
    const stream = await app.fetch(new Request("http://localhost/api/v1/events", { headers: { origin: "https://console.example" } }));
    expect(stream.headers.get("content-type")).toContain("text/event-stream");
    expect(stream.headers.get("access-control-allow-origin")).toBe("https://console.example");
    await stream.body?.cancel();
  } finally {
    app.stop();
    if (previous === undefined) delete process.env.MAO_CORS_ORIGIN;
    else process.env.MAO_CORS_ORIGIN = previous;
  }
});
