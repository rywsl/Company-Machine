# Bun control plane

The Bun backend is the TypeScript control-plane entry point for the business-system model. It owns deterministic storage, intake, event delivery, the vault boundary, and the Web API. Official Harness processes remain external runtimes.

Run the development server from the repository root with `bun run backend/src/index.ts`. The default listener is `127.0.0.1:3000`. Set `FACTORY_ROOT` to choose the shared data directory for SQLite, CAS, station runs and the vault. The default is `.factory`. `BUSINESS_DB_PATH` and `FACTORY_DB_PATH` can select existing SQLite databases.

The Web App is served from `frontend/dist` when that directory exists. Build the frontend before packaging. The event stream is available at `/api/v1/events` and accepts a `Last-Event-ID` header or `cursor` query parameter.

When a Foreman profile is configured, queued talk messages can be dispatched through the selected official Harness CLI or an explicitly configured ACP transport. The Bun process only starts the external Harness, forwards the business request, parses bounded transcript events and persists the human-readable reply. It never calls a model API itself or forwards the host environment wholesale.

Podman and Kiri health and readiness are exposed at `/api/v1/runtime` and `/api/v1/runtime/ensure-ready`. A missing runtime dependency is reported to the Web App as an explicit runtime state. `MAO_PODMAN_EXECUTABLE` can point at a user-local Podman CLI when the parent desktop process has an older PATH snapshot.

On Linux, `MAO_CONTAINER_RUNTIME=kiri` connects to the official headless `kiri.service` at `/run/kiri/docker.sock`. The control plane and its Harness and ACP commands use the same endpoint. `MAO_KIRI_SOCKET` selects a custom Unix socket and `MAO_DOCKER_EXECUTABLE` selects the Docker-compatible client. Compatibility modes are `MAO_KIRI_MODE=kiri` for the official CLI and `MAO_KIRI_MODE=docker-context` for a named context.

Secret values enter through `/api/v1/secrets/requests/:id/value` and are encrypted in the local vault. The database and event stream contain only the vault reference and status.

Run `bun run backend/bench.ts 100` to measure the factory graph, event replay and project list against a seeded SQLite database. Set `BENCH_DB_PATH` to keep the benchmark database in a chosen evidence directory.

The Bun CLI also exposes `business units`, `business run`, `business runs`, `business export`, `business graph`, `business artifact` and `business conversion` for Foreman or operator Harness workflows. These commands exchange business references and portable records, while the Web App renders their graph and timeline views.
