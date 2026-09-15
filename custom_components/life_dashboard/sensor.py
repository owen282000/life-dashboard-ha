"""Sensor platform for Life Dashboard.

Phase 4 stub: the platform loads so the entry can forward to it, but creates no
entities yet. Phase 5 turns the dispatched updates into entities under one device.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LifeDashboardConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LifeDashboardConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Life Dashboard sensors."""
    return
