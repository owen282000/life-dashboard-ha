"""Sensor platform for Life Dashboard.

Entities are created on the first value rather than all thirty up front, so a user
who syncs four data types sees four sensors. That is how the app's MQTT Discovery
behaves as well, and it keeps a phone's absent types out of the entity list.

Across a restart the entities come back before any payload does: they are rebuilt
from the entity registry and restore their own last value, which also reseeds the
ordering rule so a backfill arriving right after startup cannot overwrite newer
readings.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LifeDashboardConfigEntry, signal_update
from .const import DOMAIN, MANUFACTURER, MODEL
from .payload import SENSOR_SPECS, SensorSpec, SensorUpdate, parse_instant

_LOGGER = logging.getLogger(__name__)

# The attributes a payload can put on a sensor. Restoring by an allowlist keeps
# Home Assistant's own additions (friendly_name, unit_of_measurement, device_class
# and so on) out of the restored update, which a blocklist would keep missing as
# Home Assistant adds more of them.
_PAYLOAD_ATTRIBUTES = frozenset(
    {
        "source",
        "uuid",
        "date",
        "sample_count",
        "min",
        "max",
        "sources",
        "app_count",
        "top_apps",
        "package",
        "minutes",
        "app_version",
        "backfill",
        "record_count",
        "device",
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LifeDashboardConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors, and keep adding them as new data types arrive."""
    known: set[str] = set()

    # Entities that already existed keep existing, so history and dashboards
    # survive a restart even before the phone syncs again.
    prefix = f"{entry.entry_id}_"
    restored = [
        registry_entry.unique_id.removeprefix(prefix)
        for registry_entry in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if registry_entry.domain == "sensor"
        and registry_entry.unique_id.startswith(prefix)
        and registry_entry.unique_id.removeprefix(prefix) in SENSOR_SPECS
    ]
    if restored:
        known.update(restored)
        async_add_entities(LifeDashboardSensor(entry, key) for key in restored)

    @callback
    def _new_key(update: SensorUpdate) -> None:
        """Add an entity the first time its data type shows up.

        Entities that already exist get the same signal themselves, so there is
        nothing to hand over here.
        """
        if update.key in known or update.key not in SENSOR_SPECS:
            return
        known.add(update.key)
        async_add_entities([LifeDashboardSensor(entry, update.key)])

    entry.async_on_unload(async_dispatcher_connect(hass, signal_update(entry.entry_id), _new_key))


class LifeDashboardSensor(RestoreSensor):
    """One value from the phone."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: LifeDashboardConfigEntry, key: str) -> None:
        """Set up the sensor from its spec."""
        spec: SensorSpec = SENSOR_SPECS[key]
        self._key = key
        self._entry = entry

        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = (
            SensorDeviceClass(spec.device_class) if spec.device_class else None
        )
        self._attr_state_class = SensorStateClass(spec.state_class) if spec.state_class else None
        self._attr_suggested_display_precision = spec.precision
        self._attr_entity_category = EntityCategory.DIAGNOSTIC if spec.diagnostic else None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
            sw_version=entry.runtime_data.app_version,
        )

    async def async_added_to_hass(self) -> None:
        """Take the value we have, or the one we had before the restart."""
        await super().async_added_to_hass()

        if (held := self._entry.runtime_data.latest.get(self._key)) is not None:
            # The payload that created this entity is already in.
            self._apply(held)
        elif (restored := await self._restore()) is not None:
            self._apply(restored)
            # Seed the ordering rule, so an old batch arriving now is still old.
            self._entry.runtime_data.latest.setdefault(self._key, restored)

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_update(self._entry.entry_id), self._handle_update
            )
        )

    async def _restore(self) -> SensorUpdate | None:
        """Rebuild the last update from what Home Assistant kept for us.

        Without a measured_at there is nothing to order on, so the value is dropped
        rather than treated as infinitely old or infinitely new.
        """
        stored = await self.async_get_last_sensor_data()
        state = await self.async_get_last_state()
        if stored is None or state is None or stored.native_value is None:
            return None

        try:
            measured_at = parse_instant(state.attributes.get("measured_at"))
        except ValueError:
            return None

        last_reset = None
        if raw_reset := state.attributes.get("last_reset"):
            try:
                last_reset = parse_instant(raw_reset)
            except ValueError:
                last_reset = None

        attributes = {
            name: value for name, value in state.attributes.items() if name in _PAYLOAD_ATTRIBUTES
        }
        return SensorUpdate(
            key=self._key,
            value=stored.native_value,
            measured_at=measured_at,
            attributes=attributes,
            last_reset=last_reset,
        )

    @callback
    def _handle_update(self, update: SensorUpdate) -> None:
        """Take an update meant for this sensor."""
        if update.key != self._key:
            return
        self._apply(update)
        self.async_write_ha_state()

    @callback
    def _apply(self, update: SensorUpdate) -> None:
        """Hold a value, without writing state: callers decide when to do that."""
        self._attr_native_value = update.value
        self._attr_extra_state_attributes = {
            **update.attributes,
            "measured_at": update.measured_at.isoformat(),
        }
