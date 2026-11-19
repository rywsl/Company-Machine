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

      const response = await app.fetch(new Request("http://localhost/api/v1/runtime/ensure-ready", { method: "POST" }));
      expect(response.status).toBe(503);
      expect((await response.json()).health.reason).toBe("podman-missing");
      expect(ready).toBe(1);
    } finally {
      app.stop();
      await rm(dbPath, { force: true });
    }
  });
});
