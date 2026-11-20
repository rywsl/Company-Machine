# Development

The active product is Bun and TypeScript. The former Python implementation and its acceptance fixtures live under `removed/python/` as historical evidence.

## Verify before submitting

Run the Bun-native verifier before committing or pushing.

```bash
bun run verify:bun
```

The command runs the Bun business control plane, MRL, Podman and Kiri runtime tests. The full acceptance entry point is `bun run acceptance:bun`.

## Runtime selection

Podman is the default runtime. Set `MAO_PODMAN_EXECUTABLE` when a desktop process has an older PATH snapshot.

```bash
podman machine init --now podman-machine-default
bun run backend/src/index.ts
```

Kiri can run through its headless CLI path.

```powershell
$env:MAO_CONTAINER_RUNTIME = "kiri"
$env:MAO_KIRI_EXECUTABLE = "kiri"
bun run backend/src/index.ts
```

On Linux, the same configuration uses Kiri's native headless package.

```bash
export MAO_CONTAINER_RUNTIME=kiri
export MAO_KIRI_EXECUTABLE=kiri
bun run backend/src/index.ts
```

Kiri readiness may start `kiri daemon` without a UI, or reuse a systemd-managed daemon. `MAO_KIRI_MODE=docker-context` selects Kiri's Docker Engine API context when the installation exposes `docker --context kiri`.

## Engineering rules

Do not add dependencies, call an LLM API from the control plane, use shell concatenation, or weaken tests to make verification pass. Keep credentials out of logs, commits, and replies. Preserve unrelated changes. Commit and push only when explicitly requested.
