# Bun control plane

The Bun backend is the TypeScript control-plane entry point for the business-system model. It owns deterministic storage, intake, event delivery, the vault boundary, and the Web API. Official Harness processes remain external runtimes.

Run the development server from the repository root with `bun run backend/src/index.ts`. The default listener is `127.0.0.1:3000`. `BUSINESS_DB_PATH` or `FACTORY_DB_PATH` can point at an existing SQLite database. The default is `.factory/factory.db`, so new business tables share the existing MAO database.

The Web App is served from `src/mao/webapp/static/dist` when that directory exists. Build the frontend before packaging. The event stream is available at `/api/v1/events` and accepts a `Last-Event-ID` header or `cursor` query parameter.

Secret values enter through `/api/v1/secrets/requests/:id/value` and are encrypted in the local vault. The database and event stream contain only the vault reference and status.

Run `bun run backend/bench.ts 100` to measure the factory graph, event replay and project list against a seeded SQLite database. Set `BENCH_DB_PATH` to keep the benchmark database in a chosen evidence directory.
