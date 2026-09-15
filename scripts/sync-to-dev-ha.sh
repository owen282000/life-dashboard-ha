#!/bin/sh
# Copy the integration into the development Home Assistant's config and restart it.
#
# The compose stack for local testing lives in the companion app repository, so its
# config directory is where the container expects to find custom_components. A
# symlink does not work: the container only sees what is inside the mount.
set -e

SRC="$(cd "$(dirname "$0")/.." && pwd)/custom_components/life_dashboard"
DST="${LIFE_DASHBOARD_HA_CONFIG:-$(dirname "$0")/../../life-dashboard-companion/scripts/dev/ha-config}/custom_components/life_dashboard"
CONTAINER="${LIFE_DASHBOARD_HA_CONTAINER:-lifedashboard-homeassistant}"

mkdir -p "$(dirname "$DST")"
rsync -a --delete --exclude __pycache__ "$SRC/" "$DST/"
docker restart "$CONTAINER" >/dev/null

echo "Synced to $DST and restarted $CONTAINER."
echo "Logs: docker logs -f $CONTAINER"
echo "UI:   http://localhost:8123/config/integrations"
