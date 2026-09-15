"""The Life Dashboard integration.

Receives webhook payloads from the Life Dashboard Companion apps (Android and iOS)
and turns them into sensor entities. Phase 1 is the skeleton: an entry loads and
unloads, with no webhook and no platforms yet.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

PLATFORMS: list[str] = []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Life Dashboard from a config entry."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return True
