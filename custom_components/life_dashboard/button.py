"""Button platform for Life Dashboard: send the history to the phone.

One button per phone, and only while an entity is mapped to it: without a mapping
there is nothing to send. Pressing it queues the last thirty days from the recorder;
the service of the same name takes another window and a choice of types.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LifeDashboardConfigEntry
from .const import DOMAIN, MANUFACTURER, MODEL
from .writeback import BACKFILL_DEFAULT_DAYS

KEY_QUEUE_HISTORY = "queue_history"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LifeDashboardConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the button when something is mapped; the options flow reloads the entry."""
    writeback = entry.runtime_data.writeback
    if writeback is not None and writeback.configured:
        async_add_entities([QueueHistoryButton(entry)])


class QueueHistoryButton(ButtonEntity):
    """Queues the recorder's history of the mapped entities for the phone."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = KEY_QUEUE_HISTORY

    def __init__(self, entry: LifeDashboardConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{KEY_QUEUE_HISTORY}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
            sw_version=entry.runtime_data.app_version,
        )

    async def async_press(self) -> None:
        await self._entry.runtime_data.writeback.async_queue_history(days=BACKFILL_DEFAULT_DAYS)
