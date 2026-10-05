# Operations

## Volumes and network

The image runs as 10001:10001 and owns `/data`. A fresh Podman/Docker named volume adopts the image directory ownership. For a bind mount, create a dedicated directory and set ownership through `podman unshare chown 10001:10001 <directory>` under rootless Podman. On SELinux hosts, label a private bind mount with `:Z`; use `:z` only for deliberately shared mounts. Do not use privileged mode. Keep the service's `/data` directory out of Git.

Bind to loopback for local use. For HA, bind a trusted LAN interface and pair with its reachable host address. Outbound HTTPS to Open-Meteo is needed; household data is not sent. There is no HA password or inverter credential in the service. Restrict proxy ingress to the intended private network.

Behind HTTPS, set `ENERGY_PUBLIC_ORIGIN=https://energy.example.net`, `ENERGY_COOKIE_SECURE=true`, `ENERGY_TRUST_PROXY=true`, and `ENERGY_PROXY_IPS` to the trusted proxy address. Example Caddy configuration:

```caddy
energy.example.net {
  reverse_proxy 127.0.0.1:18080
}
```

Do not trust forwarded headers from arbitrary clients. The GUI and API share the same origin. Public health endpoints disclose only process/version readiness; export readiness is separate. API payloads are bounded to 8 MB and observation batches to 5,000 records.

## Restart and managed startup

`podman restart energy-forecast-dev` preserves the named volume. `deploy/podman-dev.sh up` rebuilds/replaces only this dedicated development container and reuses its volume and ignored `.env.dev`. `stop` retains data. Health checks require Podman builds with `--format docker`; the helper includes this because OCI-format Podman builds discard Dockerfile HEALTHCHECK metadata.

For rootless managed startup, copy `deploy/energy-forecast.container` and `deploy/energy-forecast-data.volume` to `~/.config/containers/systemd/`, adjust the image tag/binding/origin, then `systemctl --user daemon-reload` and `systemctl --user start energy-forecast`. Quadlet is an example; it has not been installed on the development workstation. Enable user lingering only if you intentionally want service operation after logout.

Compose sets four CPUs/eight GB and rotating 10 MB logs. The worker log is bounded locally. Health checks run every 30 seconds. No job should block HTTP serving. Training checkpoints are retained on interruption; cancelled jobs are not promoted. One service process and one job writer are required.

## Backup and restore

Use Administration → Download backup, or authenticated `POST /v1/sites/home/backup`. SQLite's backup API creates a consistent database copy while ingestion runs. The archive contains `energy.sqlite`, model artifacts and any Parquet history; it includes all SQLite weather/forecast/configuration/source provenance. It also contains private credentials/telemetry: store it privately, ideally encrypted, and never commit/upload it publicly.

For a fully coordinated checkpoint/model backup during active training, pause learning and wait for running jobs before downloading; or stop the container and archive the entire volume. Protect an offline volume copy with the same care as the online database.

Restore into an empty dedicated data directory/volume with the container stopped. Extract only a trusted backup with Python `tarfile.extractall(..., filter='data')`; restore ownership to 10001:10001. Start the same application version first. The service invalidates stale leases by time and does not grant fresh authorization from a restored cached snapshot. Restore was exercised in a clean Podman container with configuration/observation/forecast counts compared.

## Upgrades and rollback

Build/tag a known source revision (initial release tag `v0.1.0-alpha.1`). Take a private backup before replacing a container. Keep the previous image and data backup. Inspect logs and health after upgrade, then verify HA connectivity and configuration. Schema version 1 is the only initial schema; later migrations must ship explicit versioned steps and rollback restrictions. Never downgrade a changed database without a compatible restore.

Model rollback switches to the retained validated immutable bundle and invalidates old leases. A failed validation gate never replaces an active model. Model rollback does not validate current sensor freshness or restore old export authorizations.

No retention deletion is automatic in this alpha: raw revisions and forecast vintages are retained. Monitor volume size. Minute planning snapshots and weather archives can accumulate; a provenance-preserving retention/compaction policy needs household measurements before destructive pruning is enabled.
