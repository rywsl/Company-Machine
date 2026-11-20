import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { rm } from "node:fs/promises";
import { createApp, type RuntimeProbe } from "../src/server";
import type { RuntimeHealth } from "../runtime/podman";

const unavailable: RuntimeHealth = {
  available: false,
  platform: "win32",
  executable: "podman",
  machine: "podman-machine-default",
  rootless: null,
  reason: "podman-missing",
};

describe("Bun Podman runtime boundary", () => {
  test("reports the embedded provider and caches health probes", async () => {
    const dbPath = `.factory/runtime-${randomUUID()}.db`;
    let probes = 0;
    let ready = 0;
    const runtime: RuntimeProbe = {
      health: async () => { probes += 1; return unavailable; },
      ensureReady: async () => { ready += 1; return unavailable; },
    };
    const app = createApp({ dbPath, legacyPath: dbPath, runtime });
    try {
      const first = await app.fetch(new Request("http://localhost/api/v1/runtime"));
      const second = await app.fetch(new Request("http://localhost/api/v1/runtime"));
      expect(first.status).toBe(200);
      expect((await first.json()).dependency.provider).toBe("podman");
      expect((await second.json()).dependency.state).toBe("offline");
      expect(probes).toBe(1);

      const readiness = await app.fetch(new Request("http://localhost/readyz"));
      expect(readiness.status).toBe(503);
      expect((await readiness.json()).ok).toBe(false);

      const response = await app.fetch(new Request("http://localhost/api/v1/runtime/ensure-ready", { method: "POST" }));
      expect(response.status).toBe(503);
      const failed = await response.json();
      expect(failed.health.reason).toBe("podman-missing");
      expect(failed.message).toContain("podman-missing");
      expect(ready).toBe(1);
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });

  test("deduplicates concurrent readiness requests", async () => {
    const dbPath = `.factory/runtime-ready-${randomUUID()}.db`;
    let starts = 0;
    const runtime: RuntimeProbe = {
      health: async () => ({ ...unavailable, reason: "not-started" }),
      ensureReady: async () => {
        starts += 1;
        await new Promise((resolve) => setTimeout(resolve, 10));
        return { ...unavailable, available: true, reason: undefined };
      },
    };
    const app = createApp({ dbPath, legacyPath: dbPath, runtime });
    try {
      const responses = await Promise.all([
        app.fetch(new Request("http://localhost/api/v1/runtime/ensure-ready", { method: "POST" })),
        app.fetch(new Request("http://localhost/api/v1/runtime/ensure-ready", { method: "POST" })),
      ]);
      expect(responses.map((item) => item.status)).toEqual([200, 200]);
      expect(starts).toBe(1);
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });

  test("shares a passive probe and retains newer runtime readiness", async () => {
    let probes = 0;
    let completeProbe: (health: RuntimeHealth) => void = () => {};
    const passive = new Promise<RuntimeHealth>((resolve) => { completeProbe = resolve; });
    const ready = { ...unavailable, available: true, reason: undefined };
    const app = createApp({ dbPath: ":memory:", legacyPath: ":memory:", runtime: {
      health: async () => { probes += 1; return passive; },
      ensureReady: async () => ready,
    } });
    try {
      const status = () => app.fetch(new Request("http://localhost/api/v1/runtime"));
      const pending = [status(), status(), status()];
      expect(probes).toBe(1);
      expect((await app.fetch(new Request("http://localhost/api/v1/runtime/ensure-ready", { method: "POST" }))).status).toBe(200);
      completeProbe(unavailable);
      for (const response of await Promise.all(pending)) {
        expect(await response.json()).toMatchObject({ health: { available: true } });
      }
      expect(await (await status()).json()).toMatchObject({ health: { available: true } });
      expect(probes).toBe(1);
    } finally {
      completeProbe(unavailable);
      app.stop();
    }
  });
});
