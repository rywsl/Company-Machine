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

Download the official `kiri-lite` package for your Linux distribution and CPU architecture, and install a Docker-compatible CLI. On Debian or Ubuntu, use `sudo apt install ./KIRI_PACKAGE.deb`. On Fedora, use `sudo dnf install ./KIRI_PACKAGE.rpm`. Substitute the downloaded filename, then grant access to the account that runs MAO. [Kiri releases](https://runkiri.com/releases/)

```bash
sudo kiri host-access "$USER"
# Start a new login session before running these commands.
MAO_CONTAINER_RUNTIME=kiri bun run factory runtime ensure-ready
MAO_CONTAINER_RUNTIME=kiri bun run backend/src/index.ts
```

MAO connects through `/run/kiri/docker.sock`. Set `MAO_KIRI_SOCKET` for a custom deployment and `MAO_DOCKER_EXECUTABLE` to select the CLI. The official Linux path uses the Unix socket directly and works without a desktop session.

For station jobs inside WSL, point `FACTORY_ROOT` to a Linux filesystem such as `$HOME/.local/share/mao`. The mailbox uses Linux directory permissions to protect host responses.

Build the fixture images and verify a station job using Kiri.

```bash
export MAO_CONTAINER_RUNTIME=kiri
bun run build:test-images
bun run tools/verify-runtime.ts
```

If Kiri's image build request times out, run `bun run build:test-images --save-to /tmp/mao-test-images` on a Podman host with the same CPU architecture. Copy the directory to the Kiri host, run `MAO_CONTAINER_RUNTIME=kiri bun run build:test-images --load-from /tmp/mao-test-images`, then run the same station verification command.

## Hand a project to Foreman

```bash
bun run factory project intake --name "Video production" --repo https://example.test/repo.git
bun run factory status
```

The Web App offers the same intake flow, Foreman conversation, DAG view, material lineage, resource requests and runtime settings. `bun run acceptance:bun` runs the active Bun acceptance suites.
