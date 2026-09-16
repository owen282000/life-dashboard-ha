#!/bin/sh
# Copy the integration into a development Home Assistant's config directory and restart
# the container, for a live check that the tests cannot give.
#
# The Docker stack for local testing lives in the companion app repository, and the
# container only sees what is inside its mount, so the files are copied rather than
# symlinked. Point LIFE_DASHBOARD_HA_CONFIG at that config directory; without it the
# script looks for the app repository next to this one.
set -eu

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/custom_components/life_dashboard"
CONFIG="${LIFE_DASHBOARD_HA_CONFIG:-$HERE/../life-dashboard-companion/scripts/dev/ha-config}"
CONTAINER="${LIFE_DASHBOARD_HA_CONTAINER:-lifedashboard-homeassistant}"

if [ ! -d "$CONFIG" ]; then
    echo "No Home Assistant config directory at $CONFIG." >&2
    echo "Set LIFE_DASHBOARD_HA_CONFIG to the config directory of the dev container." >&2
    exit 1
fi

DST="$CONFIG/custom_components/life_dashboard"
mkdir -p "$(dirname "$DST")"
rsync -a --delete --exclude __pycache__ "$SRC/" "$DST/"
docker restart "$CONTAINER" >/dev/null

echo "Synced to $DST and restarted $CONTAINER."
echo "Logs: docker logs -f $CONTAINER"
echo "UI:   http://localhost:8123/config/integrations"
