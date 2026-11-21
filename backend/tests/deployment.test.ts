import { describe, expect, test } from "bun:test";
import { existsSync, readFileSync } from "node:fs";
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

  test("builds the production image with Bun's frozen lockfile", () => {
    const containerfile = readFileSync(resolve(import.meta.dir, "../../deploy/mao-control-plane.Containerfile"), "utf8");
    expect(containerfile).toContain("COPY package.json bun.lock ./");
    expect(containerfile).toContain("RUN bun install --frozen-lockfile");
    expect(containerfile).toContain("RUN bun run --cwd frontend build");
    expect(containerfile).not.toContain("pnpm");
    expect(existsSync(resolve(import.meta.dir, "../../bun.lock"))).toBe(true);
    const ignore = readFileSync(resolve(import.meta.dir, "../../.containerignore"), "utf8");
    expect(ignore).toContain(".factory");
    expect(ignore).toContain("node_modules");
  });
});
