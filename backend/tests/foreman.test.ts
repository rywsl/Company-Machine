import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rm } from "node:fs/promises";
import { resolve } from "node:path";
import { createApp } from "../src/server";
import type { OfficialProcess } from "../src/harness";

function fakeProcess(lines: string[]): OfficialProcess {
  const body = new TextEncoder().encode(`${lines.join("\n")}\n`);
  return { stdout: new ReadableStream({ start(controller) { controller.enqueue(body); controller.close(); } }), kill() {} };
}

describe("Foreman official Harness dispatch", () => {
  test("claims queued intent, streams deltas and persists a reply", async () => {
    const dbPath = `.factory/foreman-${randomUUID()}.db`;
    let factoryEnv: Record<string, string> = {};
    const app = createApp({
      dbPath,
      legacyPath: dbPath,
      storageRoot: ".factory",
      foreman: { spawn: (_argv, options) => {
        factoryEnv = options.env;
        return fakeProcess([
          JSON.stringify({ type: "assistant", message: { content: [{ type: "text", text: "我会先建立厂区。" }] } }),
          JSON.stringify({ type: "result", text: "已开始" }),
        ]);
      } },
    });
    try {
      const profile = app.store.saveHarnessProfile({ runtime: "codex", role: "foreman", baseUrl: "https://example.invalid", model: "codex-test", mode: "session" });
      expect(profile.role).toBe("foreman");
      const response = await app.fetch(new Request("http://localhost/api/v1/talk", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: "建立一个视频业务系统" }) }));
      expect(response.status).toBe(201);
      for (let attempt = 0; attempt < 20 && app.store.listTalkMessages().filter((item) => item.role === "foreman").length === 0; attempt++) await new Promise((resolve) => setTimeout(resolve, 5));
      expect(app.store.listTalkMessages().some((item) => item.role === "foreman" && item.body.includes("我会先建立厂区。"))).toBe(true);
      expect(app.events.after().map((event) => event.type)).toContain("foreman.reply.delta");
      expect(factoryEnv).toMatchObject({ FACTORY_ROOT: resolve(".factory"), BUSINESS_DB_PATH: resolve(dbPath), FACTORY_DB_PATH: resolve(dbPath) });
      expect(app.store.listForemanInbox()).toHaveLength(0);
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });

  test("resumes the official session ID on the next native turn", async () => {
    const calls: string[][] = [];
    const sessionId = "a9919900-0000-4111-9111-100000000000";
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", foreman: { spawn: (argv) => {
      calls.push(argv);
      return fakeProcess([
        JSON.stringify({ type: "thread.started", thread_id: sessionId }),
        JSON.stringify({ type: "item.completed", item: { type: "agent_message", text: "进度已更新。" } }),
        JSON.stringify({ type: "turn.completed" }),
      ]);
    } } });
    try {
      const profile = app.store.saveHarnessProfile({ runtime: "codex", role: "foreman", baseUrl: "", model: "fixture", mode: "session" });
      const first = app.store.queueForemanMessage("turn-1", "创建项目");
      const second = app.store.queueForemanMessage("turn-2", "接着做");
      await Promise.all([app.foreman.dispatch(first.id), app.foreman.dispatch(second.id)]);
      expect(app.store.foremanSession(profile.id)).toBe(sessionId);
      expect(calls[0]).not.toContain("resume");
      expect(calls[1].slice(0, 3)).toEqual(["codex", "exec", "resume"]);
      expect(calls[1]).toContain(sessionId);
      expect(app.store.listTalkMessages().map(({ body }) => body)).toEqual(["进度已更新。", "进度已更新。"]);
    } finally {
      await app.foreman.close();
      app.stop();
    }
  });
});
