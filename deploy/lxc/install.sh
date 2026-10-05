#!/bin/sh
# Run inside the selected LXC after its Docker image and deployment files are staged.
set -eu
cd /opt/energy-forecast
test -f .env || { echo 'Create /opt/energy-forecast/.env from env.example first.' >&2; exit 1; }
docker info >/dev/null
if docker compose version >/dev/null 2>&1; then
  docker compose -p energy-forecast --env-file .env -f compose.yaml config --quiet
  docker compose -p energy-forecast --env-file .env -f compose.yaml up -d --no-build
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose -p energy-forecast --env-file .env -f compose.yaml config --quiet
  docker-compose -p energy-forecast --env-file .env -f compose.yaml up -d --no-build
else
  echo 'Docker Compose is required; no existing Docker services have been changed.' >&2
  exit 1
fi
for attempt in $(seq 1 30); do
  if docker exec energy-forecast python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready',timeout=3)" >/dev/null 2>&1; then
    docker inspect --format '{{.Name}} {{.Config.Image}} {{.Config.User}} {{.State.Status}}' energy-forecast
    exit 0
  fi
  sleep 1
done
docker logs --tail 80 energy-forecast
exit 1
