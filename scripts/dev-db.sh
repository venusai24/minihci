#!/usr/bin/env bash
# Idempotently start the dev/test Postgres container and ensure the test DB exists.
set -euo pipefail
NAME=minihci-pg17  # distinct from any pre-existing minihci-pg container
if ! podman container exists "$NAME"; then
  podman run -d --name "$NAME" \
    -e POSTGRES_USER=minihci -e POSTGRES_PASSWORD=minihci -e POSTGRES_DB=minihci \
    -p 5433:5432 docker.io/library/postgres:17
else
  podman start "$NAME" >/dev/null
fi
until podman exec "$NAME" pg_isready -U minihci >/dev/null 2>&1; do sleep 0.5; done
if ! podman exec "$NAME" psql -U minihci -tAc "SELECT 1 FROM pg_database WHERE datname='minihci_test'" | grep -q 1; then
  podman exec "$NAME" psql -U minihci -c "CREATE DATABASE minihci_test"
fi
echo "postgres ready on :5433"
