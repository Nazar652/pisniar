#!/bin/sh
# Deploys the images of one sha: pull, stop the writers, migrate, start. Run from the checkout root.
# The caller wrote .env first (write-env.sh); this script prints no configuration and never uses `down`.
set -eu

PISNIAR_IMAGE_TAG=${1:-$(git rev-parse HEAD)}
COMPOSE_FILE=compose.yaml:compose.prod.yml
COMPOSE_PROFILES=funnel
export PISNIAR_IMAGE_TAG COMPOSE_FILE COMPOSE_PROFILES

for required_file in .env .env.tailscale; do
    if [ ! -f "$required_file" ]; then
        echo "deploy: $required_file is missing: run deploy/write-env.sh first" >&2
        exit 1
    fi
done

if ! docker volume inspect pisniar-tailscale-state >/dev/null 2>&1; then
    echo "deploy: the volume pisniar-tailscale-state is missing: docker volume create pisniar-tailscale-state" >&2
    exit 1
fi

# nothing is stopped before the images are here: a failed pull leaves the running version alone.
docker compose pull --quiet

# the writers stop before the migration; `rm` makes the new containers read the fresh .env.
docker compose stop bot webapp
docker compose stop worker worker-render
docker compose rm -f bot webapp worker worker-render

docker compose run --rm migrate
docker compose run --rm migrate alembic check

docker compose up -d --no-build --wait --wait-timeout 600 --remove-orphans
docker compose ps
