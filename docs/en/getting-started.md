# Getting Started

The active project is a Bun + TypeScript business control plane. Python source and legacy fixtures are archived under `removed/python/`.

## Prerequisites

- Bun
- Podman 6 or Kiri 0.2+
- An official Harness CLI such as Claude Code or Codex, when real operator sessions are needed

## Install and verify

```bash
bun install
bun run verify:bun
pnpm check:web
pnpm build:web
```

## Start the local factory

```bash
bun run backend/src/index.ts
```

The service listens on `127.0.0.1:3000` and serves the Web App from `frontend/dist`.

## Prepare Podman

```bash
podman machine init --now podman-machine-default
bun run factory status
```

Set `MAO_PODMAN_EXECUTABLE` when the desktop process has an old PATH snapshot.

## Use headless Kiri

```powershell
$env:MAO_CONTAINER_RUNTIME = "kiri"
$env:MAO_KIRI_EXECUTABLE = "kiri"
bun run backend/src/index.ts
```

On Linux, use the native headless package and export the same settings before starting Bun.

```bash
export MAO_CONTAINER_RUNTIME=kiri
export MAO_KIRI_EXECUTABLE=kiri
bun run backend/src/index.ts
```

Readiness can start `kiri daemon` without opening a window. If the Linux package already runs the official systemd service, point the control plane at its socket with `MAO_KIRI_SOCKET=/run/kiri/docker.sock` and optionally `MAO_KIRI_DATA_DIR=/var/lib/kiri-daemon`, then the health probe reuses it. Use `MAO_KIRI_MODE=docker-context` when Kiri exposes the `kiri` Docker context instead of direct CLI commands.

## Hand a project to Foreman

```bash
bun run factory project intake --name "Video production" --repo https://example.test/repo.git
bun run factory status
```

The Web App offers the same intake flow, Foreman conversation, DAG view, material lineage, resource requests and runtime settings. `bun run acceptance:bun` runs the active Bun acceptance suites.
