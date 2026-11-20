import { describe, expect, test } from "bun:test";
import { existsSync } from "node:fs";
import { createApp, type BackendApp } from "../src/server";
import type { AcpSpawn } from "../src/acp";
import type { OfficialSpawn } from "../src/harness";
import type { RuntimeHealth } from "../runtime/podman";

function post(app: BackendApp, route: string, body: object) {
  return app.fetch(new Request(`http://localhost/api/web/harnesses/${route}`, { method: "POST", body: JSON.stringify(body) }));
}

function configure(app: BackendApp, runtime: string, settings: object = {}) {
  return post(app, "configure", { runtime, role: "foreman", model: "selected-model", base_url: "https://provider.example", ...settings });
}

function completed(app: BackendApp): Promise<void> {
  return new Promise((resolve, reject) => {
    const unsubscribe = app.events.subscribe((event) => {
      if (event.type !== "harness.profile.verification.completed") return;
      clearTimeout(timer);
      unsubscribe();
      resolve();
    });
    const timer = setTimeout(() => {
      unsubscribe();
      reject(new Error("Harness verification did not complete"));
    }, 1_000);
  });
}

const encoder = new TextEncoder();
function line(record: object): Uint8Array { return encoder.encode(`${JSON.stringify(record)}\n`); }
const online: RuntimeHealth = { available: true, platform: "linux", executable: "kiri", machine: "not-needed", rootless: true };

describe("Harness verification API", () => {
  test("verifies an official CLI response and projects the completed state without probe data", async () => {
    let processOptions: Parameters<OfficialSpawn>[1] | undefined;
    let processArgs: string[] = [];
    let killed = 0;
    let readinessChecks = 0;
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", foreman: { spawn: (argv, options) => {
      processArgs = argv;
      processOptions = options;
      return { stdout: new ReadableStream({ start(controller) {
        controller.enqueue(line({ type: "assistant", message: { content: [{ type: "text", text: "MAO-OK" }] } }));
        controller.enqueue(line({ type: "result", result: "MAO-OK" }));
        controller.close();
      } }), exited: Promise.resolve(0), kill: () => { killed++; } };
    } }, runtime: { health: async () => online, ensureReady: async () => { readinessChecks++; return online; } } });
    try {
      const image = `example.invalid/harness@sha256:${"1".repeat(64)}`;
      await configure(app, "claude-code", { image, network: "egress" });
      const done = completed(app);
      const response = await post(app, "verify", { profile_id: "foreman:claude-code" });
      expect(response.status).toBe(202);
      expect((await response.json()).profile.verification.state).toBe("requested");
      await done;
      expect(readinessChecks).toBe(1);
      expect(processArgs).toContain("selected-model");
      expect(processArgs.at(-1)).toContain("MAO-OK");
      expect(processOptions).toMatchObject({ env: { ANTHROPIC_BASE_URL: "https://provider.example" }, containerImage: image, network: "egress" });
      expect(Object.keys(processOptions?.env ?? {})).toEqual(["ANTHROPIC_BASE_URL"]);
      expect(existsSync(processOptions!.cwd)).toBe(false);
      expect(killed).toBe(1);
      const profiles = await app.fetch(new Request("http://localhost/api/web/harnesses"));
      const config = (await profiles.json()).find((item: { id: string }) => item.id === "claude-code").configuration;
      expect(config.verification.state).toBe("verified");
      expect(config.verification.checked_at).toBeString();
      expect(JSON.stringify(app.events.after())).not.toContain("MAO-OK");
    } finally { app.stop(); }
  });

  test("checks a fresh ACP session and requires the official prompt to end normally", async () => {
    const methods: string[] = [];
    let killed = 0;
    const acpSpawn: AcpSpawn = () => {
      let output!: ReadableStreamDefaultController<Uint8Array>;
      return {
        stdout: new ReadableStream({ start(controller) { output = controller; } }),
        stdin: { write(bytes) {
          const request = JSON.parse(new TextDecoder().decode(bytes));
          methods.push(request.method);
          if (request.method === "initialize") output.enqueue(line({ jsonrpc: "2.0", id: request.id, result: { protocolVersion: 1 } }));
          if (request.method === "session/new") output.enqueue(line({ jsonrpc: "2.0", id: request.id, result: { sessionId: "probe-session" } }));
          if (request.method === "session/prompt") {
            for (const text of ["MAO-", "OK"]) output.enqueue(line({ jsonrpc: "2.0", method: "session/update", params: { sessionId: "probe-session", update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text } } } }));
            output.enqueue(line({ jsonrpc: "2.0", id: request.id, result: { stopReason: "end_turn" } }));
          }
        } },
        kill: () => { killed++; },
      };
    };
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", operator: { acpSpawn } });
    try {
      await configure(app, "kimi-code", { role: "operator" });
      const done = completed(app);
      expect((await post(app, "verify", { profile_id: "operator:kimi-code" })).status).toBe(202);
      await done;
      expect(app.store.listHarnessProfiles()[0]?.verification?.state).toBe("verified");
      expect(methods).toEqual(["initialize", "session/new", "session/prompt"]);
      expect(killed).toBe(1);
    } finally { app.stop(); }
  });

  test("excludes transport diagnostics and blocks container startup when runtime health is unavailable", async () => {
    let launches = 0;
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", foreman: { spawn: () => {
      launches++;
      return { stdout: new ReadableStream({ start(controller) {
        controller.enqueue(line({ type: "result", result: "MAO-OK" }));
        controller.enqueue(line({ type: "error", text: "provider diagnostic with raw private value" }));
        controller.close();
      } }), exited: Promise.resolve(1), kill() {} };
    } }, runtime: { health: async () => ({ ...online, available: false }) } });
    try {
      const id = "foreman:claude-code";
      await configure(app, "claude-code");
      let done = completed(app);
      await post(app, "verify", { profile_id: id });
      await done;
      expect(app.store.listHarnessProfiles()[0]?.verification).toMatchObject({ state: "failed", reason: "官方 Harness 会话失败" });
      expect(JSON.stringify(app.events.after())).not.toContain("raw private value");

      await configure(app, "claude-code", { image: `example.invalid/harness@sha256:${"1".repeat(64)}` });
      done = completed(app);
      await post(app, "verify", { profile_id: id });
      await done;
      expect(app.store.listHarnessProfiles()[0]?.verification).toMatchObject({ state: "failed", reason: "容器运行时未就绪" });
      expect(launches).toBe(1);
    } finally { app.stop(); }
  });

  test("closes a timed-out startup, coalesces duplicate probes, and discards stale results after reconfiguration", async () => {
    let killed = 0;
    let spawned = 0;
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", harnessVerificationTimeoutMs: 100, foreman: { acpSpawn: () => {
      spawned++;
      let output!: ReadableStreamDefaultController<Uint8Array>;
      return { stdout: new ReadableStream({ start(controller) { output = controller; } }), stdin: { write() {} }, kill: () => { killed++; output.close(); } };
    } } });
    try {
      await configure(app, "kimi-code");
      const done = completed(app);
      const id = "foreman:kimi-code";
      await post(app, "verify", { profile_id: id });
      await post(app, "verify", { profile_id: id });
      await done;
      expect(spawned).toBe(1);
      expect(killed).toBe(1);
      expect(app.store.listHarnessProfiles()[0]?.verification).toMatchObject({ state: "failed", reason: "官方 Harness 验证超时" });

      await post(app, "verify", { profile_id: id });
      await configure(app, "kimi-code", { model: "replacement" });
      await new Promise((resolve) => setTimeout(resolve, 0));
      const current = app.store.listHarnessProfiles()[0];
      expect(spawned).toBe(2);
      expect(killed).toBe(2);
      expect(current?.model).toBe("replacement");
      expect(current?.verification?.state).toBe("pending");
      expect(app.events.after().filter((event) => event.type === "harness.profile.verification.completed")).toHaveLength(1);
    } finally { app.stop(); }
  });
});
