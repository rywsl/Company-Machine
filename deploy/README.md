# MAO Podman deployment

The control plane is a Bun process and launches official Harnesses through the embedded rootless Podman or headless Kiri provider. The supported production topology runs Bun as the same user that owns the local container runtime. This keeps project and material bind mounts in one host path namespace.

Build the production image with the repository's pinned Bun and frontend lockfile.

```bash
podman build -f deploy/mao-control-plane.Containerfile -t localhost/mao-control-plane:latest .
```

Install Bun and the repository under `~/mao`, then enable the user service. The service keeps SQLite, CAS and the encrypted vault under `~/.local/share/mao`. Runtime readiness is checked by the Bun control plane, so the same unit supports either rootless Podman or headless Kiri.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/mao-control-plane.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mao-control-plane.service
curl --fail http://127.0.0.1:3000/healthz
```

`/healthz` checks process liveness. `/readyz` checks SQLite and the configured Podman or Kiri runtime. A failed readiness response keeps the service running for recovery while preventing a process manager from treating the control plane as ready.

For Linux Podman, enable the rootless socket for the same user. The control plane and Podman CLI intentionally share the user and filesystem namespace so local project sources, material files and Foreman mailbox mounts remain usable.

```bash
systemctl --user enable --now podman.socket
curl --fail http://127.0.0.1:3000/readyz
```

The service unit does not run a Podman preflight and does not force a runtime provider. When Podman is selected, enable `podman.socket` before or alongside the control plane. `/readyz` reports the runtime state while the process remains available for recovery.

For Linux headless Kiri, create `~/.config/mao/control-plane.env` with `MAO_CONTAINER_RUNTIME=kiri`, then reload and restart the same Bun service. Do not enable `podman.socket` for a Kiri-only host. Set `MAO_KIRI_SOCKET` when the socket is outside the standard locations. The runtime provider can request `kiri.service` through `ensure-ready` when the socket is temporarily absent.

```bash
mkdir -p ~/.config/mao
cat ~/.config/mao/control-plane.env
# Ensure the file contains: MAO_CONTAINER_RUNTIME=kiri
systemctl --user daemon-reload
systemctl --user restart mao-control-plane.service
curl --fail http://127.0.0.1:3000/readyz
```

`mao-control-plane.Containerfile` remains available for building a portable Web image. Running the full control plane inside a container requires an explicit host-path mapping design for Podman and local project mounts. The service unit above is the production path for the complete business workflow.
