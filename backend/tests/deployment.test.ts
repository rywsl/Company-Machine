import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

describe("production service runtime selection", () => {
  test("leaves Podman and Kiri readiness to the Bun control plane", () => {
    const unit = readFileSync(resolve(import.meta.dir, "../../deploy/mao-control-plane.service"), "utf8");
    expect(unit).toContain("EnvironmentFile=-%h/.config/mao/control-plane.env");
    expect(unit).not.toContain("podman.socket");
    expect(unit).not.toContain("Environment=MAO_CONTAINER_RUNTIME=podman");
    expect(unit).not.toContain("ExecStartPre=/usr/bin/podman");
    expect(unit).toContain("ExecStart=/usr/bin/env bun run backend/src/index.ts");
  });
});
