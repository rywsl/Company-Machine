# Development

The active product is Bun and TypeScript. The former Python implementation and its acceptance fixtures live under `removed/python/` as historical evidence.

## Verify before submitting

Run the Bun-native verifier before committing or pushing.

```bash
bun run verify:bun
```

The command runs the Bun business control plane, MRL, Podman and Kiri runtime tests. The full acceptance entry point is `bun run acceptance:bun`.

Use `bun run verify:official-harness` on a host with installed official CLIs to record executable versions and negotiate ACP `initialize` without sending a model prompt.

## Runtime selection

Podman is the default runtime. Set `MAO_PODMAN_EXECUTABLE` when a desktop process has an older PATH snapshot.

```bash
podman machine init --now podman-machine-default
bun run backend/src/index.ts
```

For headless Linux deployments, use the official `kiri.service` and its Unix socket. The setup and optional endpoint settings are in the [getting started guide](getting-started.md#use-headless-kiri-on-linux).

## Engineering rules

Do not add dependencies, call an LLM API from the control plane, use shell concatenation, or weaken tests to make verification pass. Keep credentials out of logs, commits, and replies. Preserve unrelated changes. Commit and push only when explicitly requested.
