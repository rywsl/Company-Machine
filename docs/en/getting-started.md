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

## Use headless Kiri on Linux

For a headless host, install the official `kiri-lite` package and a Docker-compatible CLI. Grant the MAO account access to `kiri.service`, then refresh the login session before checking readiness.

```bash
sudo kiri host-access "$USER"
MAO_CONTAINER_RUNTIME=kiri bun run factory runtime ensure-ready
MAO_CONTAINER_RUNTIME=kiri bun run backend/src/index.ts
```

MAO connects through `/run/kiri/docker.sock`. Set `MAO_KIRI_SOCKET` for a custom deployment and `MAO_DOCKER_EXECUTABLE` to select the CLI. The official Linux path uses the Unix socket directly and works without a desktop session.

For station jobs inside WSL, point `FACTORY_ROOT` to a Linux filesystem such as `$HOME/.local/share/mao`. The mailbox uses Linux directory permissions to protect host responses.

## Hand a project to Foreman

```bash
bun run factory project intake --name "Video production" --repo https://example.test/repo.git
bun run factory status
```

The Web App offers the same intake flow, Foreman conversation, DAG view, material lineage, resource requests and runtime settings. `bun run acceptance:bun` runs the active Bun acceptance suites.
