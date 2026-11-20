# MAO Podman deployment

The control plane is a Bun process and launches official Harnesses through the embedded Podman or Kiri provider. The deployment entrypoint does not require Docker or a Compose daemon.

Build the production image with the repository's pinned Bun and frontend lockfile.

```bash
podman build -f deploy/mao-control-plane.Containerfile -t localhost/mao-control-plane:latest .
```

The Quadlet unit keeps the SQLite database, CAS and encrypted vault under `~/.local/share/mao`. Install it for the MAO user and enable it with the user Podman service.

```bash
mkdir -p ~/.config/containers/systemd
cp deploy/mao-control-plane.container ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user enable --now mao-control-plane.service
curl --fail http://127.0.0.1:3000/healthz
curl --fail http://127.0.0.1:3000/readyz
```

`/healthz` checks process liveness. `/readyz` checks SQLite and the configured Podman or Kiri runtime. A failed readiness response keeps the service running for recovery while preventing a process manager from treating the control plane as ready.

For Linux Podman, enable the rootless socket for the same user.

```bash
systemctl --user enable --now podman.socket
```

For Linux headless Kiri, set `MAO_CONTAINER_RUNTIME=kiri` and use the official service socket. The runtime provider can request `kiri.service` through `ensure-ready` when the socket is temporarily absent.
