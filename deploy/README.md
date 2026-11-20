# MAO Podman deployment

The control plane is a Bun process and launches official Harnesses through the embedded rootless Podman or headless Kiri provider. The supported production topology runs Bun as the same user that owns the local container runtime. This keeps project and material bind mounts in one host path namespace.

Build the production image with the repository's pinned Bun and frontend lockfile.

```bash
podman build -f deploy/mao-control-plane.Containerfile -t localhost/mao-control-plane:latest .
```

Install Bun and the repository under `~/mao`, then enable the user service. The service keeps SQLite, CAS and the encrypted vault under `~/.local/share/mao` and starts only after the rootless Podman socket is available.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/mao-control-plane.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now podman.socket
systemctl --user enable --now mao-control-plane.service
curl --fail http://127.0.0.1:3000/healthz
curl --fail http://127.0.0.1:3000/readyz
```

`/healthz` checks process liveness. `/readyz` checks SQLite and the configured Podman or Kiri runtime. A failed readiness response keeps the service running for recovery while preventing a process manager from treating the control plane as ready.

For Linux Podman, enable the rootless socket for the same user. The control plane and Podman CLI intentionally share the user and filesystem namespace so local project sources, material files and Foreman mailbox mounts remain usable.

```bash
systemctl --user enable --now podman.socket
```

For Linux headless Kiri, install the repository and run the same Bun service with `MAO_CONTAINER_RUNTIME=kiri` in `~/.config/mao/control-plane.env`. Set `MAO_KIRI_SOCKET` when the socket is outside the standard locations. The runtime provider can request `kiri.service` through `ensure-ready` when the socket is temporarily absent.

`mao-control-plane.Containerfile` remains available for building a portable Web image. Running the full control plane inside a container requires an explicit host-path mapping design for Podman and local project mounts. The service unit above is the production path for the complete business workflow.
