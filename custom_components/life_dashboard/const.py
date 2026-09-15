"""Constants for the Life Dashboard integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "life_dashboard"

# Config entry data keys.
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_SECRET: Final = "secret"
CONF_URL_CHOICE: Final = "url_choice"
CONF_CLOUDHOOK_URL: Final = "cloudhook_url"

# Values for CONF_URL_CHOICE.
URL_CHOICE_INTERNAL: Final = "internal"
URL_CHOICE_EXTERNAL: Final = "external"
URL_CHOICE_CLOUD: Final = "cloud"

DEFAULT_NAME: Final = "Life Dashboard"

# Shown as the device manufacturer and model.
MANUFACTURER: Final = "owen282000"
MODEL: Final = "Life Dashboard Companion"
