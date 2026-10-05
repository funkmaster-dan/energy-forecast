#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
case "${1:-up}" in
  up)
    test -f .env.dev || cp .env.dev.example .env.dev
    podman volume exists energy-forecast-dev-data || podman volume create energy-forecast-dev-data
    podman build --format docker -t localhost/energy-forecast:dev .
    podman run -d --replace --name energy-forecast-dev --env-file .env.dev \
      -p "${ENERGY_BIND_ADDRESS:-127.0.0.1}:${ENERGY_HOST_PORT:-18080}:8080" \
      --cpus 4 --memory 8g -v energy-forecast-dev-data:/data localhost/energy-forecast:dev
    ;;
  logs) podman logs --tail 100 -f energy-forecast-dev ;;
  stop) podman stop energy-forecast-dev ;;
  key) podman exec energy-forecast-dev cat /data/setup-key ;;
  *) echo 'Usage: deploy/podman-dev.sh [up|logs|stop|key]' >&2; exit 2 ;;
esac
