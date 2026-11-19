import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rm } from "node:fs/promises";
import { createApp } from "../src/server";
import type { OfficialProcess } from "../src/harness";

function fakeProcess(lines: string[]): OfficialProcess {
  const body = new TextEncoder().encode(`${lines.join("\n")}\n`);
  return { stdout: new ReadableStream({ start(controller) { controller.enqueue(body); controller.close(); } }), kill() {} };
}

describe("Foreman official Harness dispatch", () => {
  test("claims queued intent, streams deltas and persists a reply", async () => {
    const dbPath = `.factory/foreman-${randomUUID()}.db`;
    const app = createApp({
      dbPath,
      legacyPath: dbPath,
      foreman: { spawn: () => fakeProcess([
        JSON.stringify({ type: "assistant", message: { content: [{ type: "text", text: "我会先建立厂区。" }] } }),
        JSON.stringify({ type: "result", text: "已开始" }),
      ]) },
    });
    try {
      const profile = app.store.saveHarnessProfile({ runtime: "codex", role: "foreman", baseUrl: "https://example.invalid", model: "codex-test", mode: "session" });
      expect(profile.role).toBe("foreman");
      const response = await app.fetch(new Request("http://localhost/api/v1/talk", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: "建立一个视频业务系统" }) }));
      expect(response.status).toBe(201);
      for (let attempt = 0; attempt < 20 && app.store.listTalkMessages().filter((item) => item.role === "foreman").length === 0; attempt++) await new Promise((resolve) => setTimeout(resolve, 5));
      expect(app.store.listTalkMessages().some((item) => item.role === "foreman" && item.body.includes("我会先建立厂区。"))).toBe(true);
      expect(app.events.after().map((event) => event.type)).toContain("foreman.reply.delta");
      expect(app.store.listForemanInbox()).toHaveLength(0);
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });
});
