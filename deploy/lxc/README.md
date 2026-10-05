# Docker deployment in an existing Proxmox LXC

Stage the versioned image archive into the selected guest, run `docker load -i <archive>`, then place these files in `/opt/energy-forecast`. Copy `env.example` to the private `.env` and set its actual LXC address, free host port, CPU/memory allowance and image tag. `install.sh` validates Compose and starts only the `energy-forecast` project.

Before installing, inspect the LXC allocation, `docker ps`, listening ports and free storage. Do not alter unrelated containers. If an Energy Forecast instance already exists, preserve its `energy-forecast-data` volume and take a private backup before upgrading. Record its old image/configuration for rollback.

The default two CPU/two GB application limit is conservative for shared minor services. It is a limit to verify with this host, not a proven capacity for long training jobs. Initial live verification should begin with data collection and baseline forecasts; adjust resource limits only after measuring this installation.

A named volume retains all installation data. The image runs as 10001:10001 and keeps HA credentials in a private data-volume file and has no device control. Retrieve the local claim key with `docker exec energy-forecast cat /data/setup-key`, then choose your own administrator password in the GUI. Connect HA directly in Setup. Optionally create a scoped integration token for HACS forecast publication using the reachable service URL.

Keep `.env`, backups, credentials and commissioning records private. Container health and verified input freshness are separate from export readiness. The alpha authorizes no discretionary export.
