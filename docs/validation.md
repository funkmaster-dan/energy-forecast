# Validation workflow

Run basic service checks with `uv run ruff check .` and `uv run pytest`; use `uv sync --extra cnn` to include the ordered direct TCN check. The focused suite covers immutable revisions/batches, reset-aware units/epochs, aligned disjoint sums, missing components, genuine extremes, AC/DC conversion, clock folds/half-hour zones, partial rules, battery conservation, power/energy caps, free-charge duration/origin, expiry, scopes/CSRF, chronological embargoes, paired dependence, partial calendar events, and passive parameter identifiability.

The HA repository has pure protocol/retry tests plus a real-runtime lifecycle script. It was tested against HA Core 2026.9.4 in an isolated Python 3.14 runtime, with its own SQLite Recorder and synthetic entities. The service uses Python 3.13 independently.

## Synthetic container smoke

Use a distinct demo volume and loopback port 18081. The development/household volume must remain separate.

```sh
podman run -d --name energy-forecast-demo \
  -p 127.0.0.1:18081:8080 -v energy-forecast-demo-data:/data \
  --cpus 4 --memory 8g localhost/energy-forecast:dev
podman exec energy-forecast-demo cat /data/setup-key
uv run python deploy/seed-demo.py http://127.0.0.1:18081 \
  --password synthetic-demo-password --setup-key '<demo-setup-key>'
```

The demo password is deliberately synthetic and the port is loopback-only. Do not use this password or dataset for an actual household. The loader refuses an already configured service. It queues weather/inference; the worker may run a weekly training candidate immediately when sufficient history arrives. Training fit is not acceptance: an exact recurring synthetic profile should cause a weaker CNN candidate to fail promotion.

Check `GET /health/live` and `/health/ready`, then authenticate and inspect observation counts, forecast rows, weather vintage, jobs, model gates, and zero authorized export. Test restart and container replacement against the same volume. Download an authenticated backup, restore into a clean private directory/volume and compare configuration, immutable raw record count, archived forecasts/weather, and model metadata. A stale restored forecast must stay stale.

## Playwright browser verification

```sh
cd web
npm ci
npx playwright install chromium
npm run check:browser
```

The script targets the seeded synthetic instance above. Override `ENERGY_BROWSER_URL` and `ENERGY_BROWSER_PASSWORD` only for a dedicated test environment. It checks all 11 pages, tariff preview, 24/48-hour chart switching, desktop/mobile layouts, page errors and horizontal overflow. Screenshots go to `/tmp/energy-overview-desktop.png` and `/tmp/energy-overview-mobile.png` for manual visual inspection. Additional manual checks performed during development cover first-run setup, rejected invalid tariff/configuration revisions, configuration-driven lease invalidation, archive replay, quality review, themes and backup download.

## Measured validation limitations

Cached forecast API p95 was 6.73 ms in a 30-request sequential local smoke; this is not a concurrent production benchmark. The CPU-only image was built/run rootless with four CPU/eight GB limits, non-root UID 10001. Full baseline refresh, synthetic training, weather receipt, restart/replacement and clean-volume restore were exercised.

Docker startup was subsequently verified in a Debian LXC using Docker 29.7.2 and Compose 5.5.0: health, non-root operation, authentication, persistent restart and outbound weather passed. Browser setup rendered on desktop/mobile without errors. The image was built with Podman and imported into Docker; an in-Docker build and ARM64 compatibility remain unverified. The existing household HA could not be commissioned because passwordless access was unavailable; live PV/load/battery semantics, sensor continuity, installed geometry/tariff and passive parameter independence remain unverified. Nothing was installed into that HA and no battery controls were operated.

High-confidence export acceptance requires subsequent measured outcomes, source/battery commissioning, independent operating-envelope validation and a supported effective sample size. Overlapping hourly/minute plans do not count as independent trials. Bootstrap/scenario sample counts do not replace operating trials. The alpha keeps live export authorization at zero.
