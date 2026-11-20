# Getting Started

The active project is a Bun + TypeScript business control plane. Python source and legacy fixtures are archived under `removed/python/`.

## Prerequisites

- Bun 1.3.14 (validated with this project; the Registry and fixtures use `Bun.YAML`)
- Podman 6 or Kiri 0.2+
- An official Harness such as Claude Code, Codex, Pi, Qwen Code, DeepSeek Harness, Kimi Code or ZCode, when real operator sessions are needed

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

When `bun run build:test-images` builds its default `oven/bun` base, MAO checks the configured GeoIP endpoint. Mainland-China hosts probe the configured Docker Hub mirrors concurrently and use the fastest mirror that serves the requested manifest. Pass `--base` to select an exact base explicitly. Set `MAO_GEOIP_COUNTRY`, `MAO_GEOIP_URL` or `MAO_DOCKERHUB_MIRRORS` for a deployment-specific region, endpoint or mirror list.

## Use headless Kiri on Linux

Download the official `kiri-lite` package for your Linux distribution and CPU architecture, and install a Docker-compatible CLI. On Debian or Ubuntu, use `sudo apt install ./KIRI_PACKAGE.deb`. On Fedora, use `sudo dnf install ./KIRI_PACKAGE.rpm`. Substitute the downloaded filename, then grant access to the account that runs MAO. [Kiri releases](https://runkiri.com/releases/)

```bash
sudo systemctl enable --now kiri.service
sudo kiri host-access "$USER"
# If host-access reports unavailable filesystem ACLs, sign in again first.
export FACTORY_ROOT="$HOME/.local/share/mao"
MAO_CONTAINER_RUNTIME=kiri bun run factory runtime ensure-ready
MAO_CONTAINER_RUNTIME=kiri bun run backend/src/index.ts
```

MAO normally connects through `/run/kiri/docker.sock`. The headless Kiri distribution is also discovered when its socket is at `/var/lib/kiri-daemon/.kiri/run/docker.sock`. Set `MAO_KIRI_SOCKET` for a custom deployment and `MAO_DOCKER_EXECUTABLE` to select the CLI. Run `MAO_CONTAINER_RUNTIME=kiri bun run factory runtime doctor` before starting the service. The official Linux path uses the Unix socket directly and works without a desktop session.

For station jobs inside WSL, point `FACTORY_ROOT` to a Linux filesystem such as `$HOME/.local/share/mao`. The mailbox uses Linux directory permissions to protect host responses.

For a shared deployment, set `MAO_AUTH_TOKEN` and `MAO_CSRF_TOKEN`. Add `MAO_AUTH_SCOPES_JSON` to issue scoped Bearer credentials. Its keys are Bearer tokens and each value can declare `role`, `business_systems`, and `projects`. Scoped credentials are required even on loopback and cannot read or mutate resources outside their declared scope.

Build the fixture images and verify a station job and the Foreman mailbox using Kiri.

```bash
export MAO_CONTAINER_RUNTIME=kiri
bun run build:test-images
bun run tools/verify-runtime.ts
bun run tools/verify-runtime.ts --foreman
```

If Kiri's image build request times out, run `bun run build:test-images --save-to /tmp/mao-test-images` on a Podman host with the same CPU architecture. Copy the directory to the Kiri host, run `MAO_CONTAINER_RUNTIME=kiri bun run build:test-images --load-from /tmp/mao-test-images`, then run the same station verification command.

For a containerized Foreman, configure an image pinned by digest that contains the selected official Harness. The default `bridge` network gives the trusted Foreman freedom to fetch repositories and project tools inside its container. It never uses host networking or a host engine socket. Choose `none` when the Foreman must be offline. Operator containers can select `egress` when their model traffic needs the fixed allowlist relay. Set `MAO_EGRESS_RELAY_IMAGE` to a trusted, digest-pinned local image with Bun. The relay allows the configured provider host and any additional hosts listed in `MAO_HARNESS_EGRESS_HOSTS`.

Set the selected provider's deployment credential as `MAO_FOREMAN_ANTHROPIC_API_KEY` or `MAO_FOREMAN_CODEX_API_KEY`. For a custom endpoint, set the matching `MAO_FOREMAN_ANTHROPIC_BASE_URL` or `MAO_FOREMAN_CODEX_BASE_URL` and save the same URL in the Foreman profile. Users enter project credentials through the corresponding resource requests.

## Hand a project to Foreman

```bash
bun run factory project intake --name "Video production" --repo https://example.test/repo.git
bun run factory status
```

The Web App offers the same intake flow, Foreman conversation, DAG view, material lineage, resource requests and runtime settings. `bun run acceptance:bun` runs the active Bun acceptance suites.
