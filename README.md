# Energy Forecast

Self-hosted solar and household forecasts with a configurable Australian fixed TOU tariff, a causal battery simulator, and an optional [Home Assistant integration](https://github.com/funkmaster-dan/ha-energy-forecast).

**0.3.0 is a forecasting and shadow-planning alpha. Discretionary export authorization is disabled.** A successful build, point forecast, or Monte Carlo simulation does not establish 99% whole-horizon safety. See [delivery status](docs/status.md) for completed checks and remaining work. Neither repository operates a battery.

## Run with rootless Podman

```sh
./deploy/podman-dev.sh up
./deploy/podman-dev.sh key
```

Open <http://127.0.0.1:18080>. Use the displayed local setup key to create your own administrator password (at least 12 characters). There is no default administrator password. The setup key is removed after claiming the installation. The named volume persists configuration, the raw observation journal, weather vintages, forecasts, jobs and model artifacts.

The image listens on 8080 and runs as UID/GID 10001. It includes the React GUI and CPU-only Python worker; no separate database, GPU, privileged mode or Docker socket is required. The development helper defaults to a loopback binding. For HA on another machine, choose a trusted LAN interface with `ENERGY_BIND_ADDRESS=<host-LAN-IP> ./deploy/podman-dev.sh up`, and update `ENERGY_PUBLIC_ORIGIN` in the ignored `.env.dev` to match the browser origin. Use HTTPS for remote access.

The packaged CPU CNN makes the image approximately 2.8 GB. Start with four CPUs/eight GB; this is a tested container limit, not a proven minimum hardware requirement.

## Docker Compose

```sh
cp .env.dev.example .env
# Adjust origin/bind/port when needed.
docker compose up -d --build
docker compose exec energy-forecast cat /data/setup-key
docker compose logs -f
```

Docker Engine is not installed on the development workstation. The image has been built with rootless Podman, loaded into Docker 29.7.2, and run with Docker Compose 5.5.0 in a Debian LXC. Docker startup, non-root operation, outbound weather, authentication and restart persistence passed there. An in-Docker image build remains unrun. See [operations](docs/operations.md) for ownership, TLS, Quadlet, upgrades and backup/restore.

## Configure and pair HA

1. Open **Setup** and connect with your HA URL and a long-lived access token. The service reads HA directly through its REST/WebSocket APIs; no custom integration is required for ingestion. The token is stored in a private mode-0600 file in the data volume and excluded from diagnostics. The optional HACS integration publishes forecasts back to HA.

2. Review the home location imported from HA on the map. Click the map to adjust it if needed.
3. Select household consumption and battery SoC from context-filtered HA sensor lists. Use composition forms for multiple meters or historical replacements.
4. Add each panel bank, select its generation sensor, and enter tilt and azimuth (north 0°, east 90°, south 180°, west 270°). No nameplate capacity or efficiency is required: historical generation and Open-Meteo irradiance calibrate its effective output scale. At least 14 days of covered history is needed; validation uses chronological training/calibration/test splits with gaps. Bank-specific boosted candidates activate only when held-out checks pass; failed candidates retain the previous working fit. An optional measured AC solar total validates DC-bank to AC conversion separately. Unverified AC/DC inputs do not authorize an AC export plan.
5. Review and save. Configure battery parameters, TOU periods and calendars through their forms; no JSON editing is required. Verify battery capacity, efficiencies, reserve and limits, and set a terminal obligation beyond the forecast horizon.

Measured inputs are read directly from HA; Open-Meteo is fetched directly by the service. Raw data and revisions remain local. Source changes and configuration edits invalidate existing leases.

## Development

Python 3.13, uv, Node 22 and npm:

```sh
uv sync --extra cnn
uv run ruff check .
uv run pytest
cd web
npm ci
npm run lint
npm run build
```

The committed uv/npm lockfiles pin the resolved toolchain. `uv sync` without `--extra cnn` supports the physical/profile baselines and PV trees; CNN training needs the extra. The deployable image includes it.

For separate hot-reload development, run the API with `ENERGY_DATA_DIR=./local/dev ENERGY_PUBLIC_ORIGIN=http://127.0.0.1:5173 uv run python -m energy_forecast`, then `npm run dev` in `web`. Read `local/dev/setup-key` for initial setup.

[API v1 schemas](contracts/) are authoritative for released payloads. Refresh exported contracts with `uv run python deploy/export-contracts.py`. Integration tokens allow observation ingestion and forecast/diagnostic reads; they cannot edit configuration, train models, create tokens or download backups.

A repeatable synthetic demo loader is in `deploy/seed-demo.py`. Use a separate empty demo volume; it refuses an already configured instance. Never seed a household dataset. [Validation instructions](docs/validation.md) include Playwright browser smoke checks and the isolated HA lifecycle check.

MIT licensed. Household telemetry, credentials and installed-site commissioning records must never be committed to either public repository.
