"""Receiving measurements on the phone: the Home Assistant half.

writeback_queue.py decides what a measurement is and what the phone is told; this
module is the thin layer around it that listens to the mapped entities, turns a state
into a reading in the units Health Connect wants, keeps the queue in .storage and
answers the phone's request. The options flow stores the mapping under
entry.options["writeback"]; a change there reloads the entry, which sets the
listeners again.

What a state event becomes:

- A state that is unknown, unavailable or not a number is skipped. So is the first
  state of an entity and a state Home Assistant restored at startup: a restored
  value's last_changed is the restart, not a measurement.
- The measured moment is the timestamp entity's value when one is mapped and it
  changed within the window around the event; otherwise last_changed (last_reported
  for an unchanged value that was reported again), marked time_source "state".
  Never last_updated. A timestamp entity that changes just after its value moves the
  reading it belongs to.
- Units are converted per event with Home Assistant's own converters, on the unit the
  state carries at that moment, without rounding. An entity with a unit outside the
  family (a weight in litres) is skipped and warned about once.
- Blood pressure is two entities; the two halves within ninety seconds of each other
  are one reading, timed on the systolic one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_RESTORED,
    ATTR_UNIT_OF_MEASUREMENT,
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfLength,
    UnitOfMass,
    UnitOfPressure,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    EventStateReportedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import (
    DistanceConverter,
    MassConverter,
    PressureConverter,
)

from .const import DOMAIN
from .payload import parse_instant, response_body
from .writeback_queue import (
    CODE_TOO_OLD,
    FIELD_DIASTOLIC,
    FIELD_SYSTOLIC,
    RECORDING_ACTIVE,
    RECORDING_AUTO,
    RECORDING_MANUAL,
    TIME_SOURCE_STATE,
    TYPE_BLOOD_PRESSURE,
    TYPE_BODY_FAT,
    TYPE_HEIGHT,
    VALUE_FIELDS,
    WRITEBACK_TYPES,
    Reading,
    WritebackQueue,
    configured_types,
    reading_id,
)

_LOGGER = logging.getLogger(__name__)

OPTION_WRITEBACK: Final = "writeback"
CONF_ENTITY: Final = "entity"
CONF_TIME_ENTITY: Final = "time_entity"
CONF_SYSTOLIC: Final = FIELD_SYSTOLIC
CONF_DIASTOLIC: Final = FIELD_DIASTOLIC
CONF_BODY_POSITION: Final = "body_position"
CONF_MEASUREMENT_LOCATION: Final = "measurement_location"

STORAGE_VERSION: Final = 1
SAVE_DELAY_SECONDS: Final = 5

#: The two halves of a blood pressure reading, and a timestamp entity and its value,
#: belong together when they are this close.
WINDOW: Final = timedelta(seconds=90)

DEVICE_TYPE_SCALE: Final = "scale"
DEVICE_TYPE_UNKNOWN: Final = "unknown"
DEVICE_CLASS_WEIGHT: Final = "weight"


# --- Units -----------------------------------------------------------------------


@dataclass(frozen=True)
class UnitFamily:
    """How a type's values reach the unit Health Connect wants."""

    name: str
    target: str
    error: str
    converter: type[MassConverter] | type[DistanceConverter] | type[PressureConverter] | None

    def accepts(self, unit: str | None) -> bool:
        if self.converter is None:
            return unit == self.target
        return unit in self.converter.VALID_UNITS

    def convert(self, value: float, unit: str | None) -> float | None:
        if not self.accepts(unit):
            return None
        if self.converter is None:
            return value
        return self.converter.convert(value, unit, self.target)


_MASS = UnitFamily("mass", UnitOfMass.KILOGRAMS, "unit_not_mass", MassConverter)
_LENGTH = UnitFamily("length", UnitOfLength.METERS, "unit_not_length", DistanceConverter)
_PERCENT = UnitFamily("percentage", PERCENTAGE, "unit_not_percentage", None)
_PRESSURE = UnitFamily("pressure", UnitOfPressure.MMHG, "unit_not_pressure", PressureConverter)

UNIT_FAMILIES: Final[dict[str, UnitFamily]] = {
    TYPE_HEIGHT: _LENGTH,
    TYPE_BODY_FAT: _PERCENT,
    TYPE_BLOOD_PRESSURE: _PRESSURE,
    **{
        kind: _MASS
        for kind in WRITEBACK_TYPES
        if kind not in (TYPE_HEIGHT, TYPE_BODY_FAT, TYPE_BLOOD_PRESSURE)
    },
}


def unit_error(kind: str, unit: str | None) -> str | None:
    """The options flow error for an entity whose unit does not fit the type, or None."""
    family = UNIT_FAMILIES[kind]
    return None if family.accepts(unit) else family.error


# --- The mapping -----------------------------------------------------------------


@dataclass(frozen=True)
class TypeMapping:
    """One type as the options map it: which entities, and the fixed fields."""

    kind: str
    #: role -> entity_id. "entity" for a single value; "systolic" and "diastolic".
    entities: dict[str, str]
    time_entity: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def anchor(self) -> str:
        """The entity the id is formed on: the value, or the systolic half."""
        return self.entities.get(CONF_ENTITY) or self.entities[CONF_SYSTOLIC]


def parse_mapping(options: dict[str, Any] | None) -> dict[str, TypeMapping]:
    """The mappings the options hold, only for types that are complete."""
    raw = (options or {}).get(OPTION_WRITEBACK)
    mappings: dict[str, TypeMapping] = {}
    for kind in configured_types(raw):
        slots = raw[kind]
        time_entity = slots.get(CONF_TIME_ENTITY) or None
        if kind == TYPE_BLOOD_PRESSURE:
            extra = {
                name: slots[name]
                for name in (CONF_BODY_POSITION, CONF_MEASUREMENT_LOCATION)
                if isinstance(slots.get(name), str) and slots[name]
            }
            mappings[kind] = TypeMapping(
                kind,
                {CONF_SYSTOLIC: slots[CONF_SYSTOLIC], CONF_DIASTOLIC: slots[CONF_DIASTOLIC]},
                time_entity,
                extra,
            )
        else:
            mappings[kind] = TypeMapping(kind, {CONF_ENTITY: slots[CONF_ENTITY]}, time_entity)
    return mappings


# --- Blood pressure -------------------------------------------------------------


@dataclass
class _Half:
    role: str
    value: float
    at: datetime


class BloodPressurePairer:
    """Joins the systolic and diastolic halves that arrive as separate events.

    The first half opens a window; the other half inside it closes it and yields the
    pair, timed on the systolic event. A half that is never joined is dropped by the
    next one. The same class serves the live listeners and the backfill, so both form
    the same readings from the same events.
    """

    def __init__(self, window: timedelta = WINDOW) -> None:
        self._window = window
        self._half: _Half | None = None

    def offer(self, role: str, value: float, at: datetime) -> tuple[float, float, datetime] | None:
        half = self._half
        if half is not None and half.role != role and abs(at - half.at) <= self._window:
            self._half = None
            systolic, diastolic = (half, _Half(role, value, at))
            if role == CONF_SYSTOLIC:
                systolic, diastolic = diastolic, systolic
            return systolic.value, diastolic.value, systolic.at
        self._half = _Half(role, value, at)
        return None


# --- The manager -------------------------------------------------------------------


class WritebackManager:
    """Keeps one phone's queue and feeds it from the mapped entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.writeback"
        )
        self.queue = WritebackQueue()
        self.mappings = parse_mapping(entry.options)
        self._pairers: dict[str, BloodPressurePairer] = {}
        self._warned_units: set[str] = set()
        #: entity_id -> (kind, role) for the value entities.
        self._roles: dict[str, tuple[str, str]] = {}
        #: time entity -> kinds it times.
        self._timed: dict[str, list[str]] = {}
        for kind, mapping in self.mappings.items():
            for role, entity_id in mapping.entities.items():
                self._roles[entity_id] = (kind, role)
            if mapping.time_entity:
                self._timed.setdefault(mapping.time_entity, []).append(kind)

    @property
    def configured(self) -> list[str]:
        return list(self.mappings)

    @property
    def version(self) -> str:
        return self._entry.runtime_data.version

    # --- Lifecycle -------------------------------------------------------------------

    async def async_load(self) -> None:
        self.queue = WritebackQueue.from_dict(await self._store.async_load())

    async def async_flush(self) -> None:
        await self._store.async_save(self.queue.to_dict())

    @callback
    def async_start(self) -> None:
        """Listen to the mapped entities, changed and reported alike."""
        self._seed()
        self._sync_issues()
        entity_ids = [*self._roles, *self._timed]
        if not entity_ids:
            return
        self._entry.async_on_unload(
            async_track_state_change_event(self._hass, entity_ids, self._on_changed)
        )
        self._entry.async_on_unload(
            async_track_state_report_event(self._hass, entity_ids, self._on_reported)
        )

    def _seed(self) -> None:
        """Let the debounce start from the values the entities hold right now.

        Polled integrations write their state again after a restart without anything
        having been measured. A value equal to the last measurement of that entity
        continues it, so the first reports after startup do not become a reading.
        """
        now = dt_util.utcnow()
        for entity_id, (kind, _) in self._roles.items():
            state = self._hass.states.get(entity_id)
            if state is None or (seen := self.queue.seen.get(entity_id)) is None:
                continue
            value = self._converted(kind, entity_id, state)
            if value is None:
                continue
            field_name = FIELD_SYSTOLIC if kind == TYPE_BLOOD_PRESSURE else VALUE_FIELDS[kind]
            if seen.values.get(field_name) == value and seen.time < now:
                seen.time = now

    def _save(self) -> None:
        self._store.async_delay_save(self.queue.to_dict, SAVE_DELAY_SECONDS)

    # --- Events -----------------------------------------------------------------------

    @callback
    def _on_changed(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        old_state = event.data["old_state"]
        if new_state is None or old_state is None or new_state.attributes.get(ATTR_RESTORED):
            return
        entity_id = event.data["entity_id"]
        if entity_id in self._timed:
            self._on_time_entity(entity_id, new_state)
        if entity_id in self._roles:
            # An unchanged value whose attributes changed is a report of that value.
            moment = (
                new_state.last_changed
                if old_state.state != new_state.state
                else new_state.last_reported
            )
            self._on_value(entity_id, new_state, moment)

    @callback
    def _on_reported(self, event: Event[EventStateReportedData]) -> None:
        entity_id = event.data["entity_id"]
        if entity_id in self._roles:
            self._on_value(entity_id, event.data["new_state"], event.data["last_reported"])

    def _on_value(self, entity_id: str, state: State, moment: datetime) -> None:
        kind, role = self._roles[entity_id]
        value = self._converted(kind, entity_id, state)
        if value is None:
            return
        mapping = self.mappings[kind]

        if kind == TYPE_BLOOD_PRESSURE:
            pairer = self._pairers.setdefault(kind, BloodPressurePairer())
            pair = pairer.offer(role, value, moment)
            if pair is None:
                return
            systolic, diastolic, moment = pair
            values = {FIELD_SYSTOLIC: systolic, FIELD_DIASTOLIC: diastolic}
        else:
            values = {VALUE_FIELDS[kind]: value}

        measured_at, time_source = self._measured_time(mapping, moment)
        self._offer(mapping, values, measured_at, time_source)

    def _on_time_entity(self, entity_id: str, state: State) -> None:
        """The timestamp entity changed: move the reading it belongs to, if any."""
        try:
            measured_at = parse_instant(state.state)
        except ValueError:
            return
        for kind in self._timed[entity_id]:
            anchor = self.mappings[kind].anchor
            recent = [
                reading
                for reading in self.queue.pending.values()
                if reading.entity_id == anchor
                and reading.time_source == TIME_SOURCE_STATE
                and abs(state.last_changed - reading.time) <= WINDOW
            ]
            if not recent:
                continue
            latest = max(recent, key=lambda reading: reading.time)
            if self.queue.rekey(latest.id, measured_at) is not None:
                _LOGGER.debug("Moved a %s reading of %s to its timestamp", kind, anchor)
                self._save()

    # --- From a state to a reading --------------------------------------------------

    def _converted(self, kind: str, entity_id: str, state: State) -> float | None:
        """The state as a number in the unit Health Connect wants, or None to skip."""
        if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        converted = UNIT_FAMILIES[kind].convert(value, unit)
        if converted is None and entity_id not in self._warned_units:
            self._warned_units.add(entity_id)
            _LOGGER.warning(
                "%s has unit %r, which is not a %s unit; its readings are not sent to %s",
                entity_id,
                unit,
                UNIT_FAMILIES[kind].name,
                self._entry.title,
            )
        return converted

    def _measured_time(self, mapping: TypeMapping, moment: datetime) -> tuple[datetime, str | None]:
        """The timestamp entity's value if it changed within the window, else the event."""
        if mapping.time_entity and (state := self._hass.states.get(mapping.time_entity)):
            try:
                measured_at = parse_instant(state.state)
            except ValueError:
                measured_at = None
            if measured_at is not None and abs(state.last_changed - moment) <= WINDOW:
                return measured_at, None
        return moment, TIME_SOURCE_STATE

    def _offer(
        self,
        mapping: TypeMapping,
        values: dict[str, float],
        measured_at: datetime,
        time_source: str | None,
    ) -> Reading | None:
        anchor = mapping.anchor
        reading = Reading(
            id=reading_id(anchor, measured_at),
            version=1,
            type=mapping.kind,
            entity_id=anchor,
            time=measured_at,
            values=values,
            zone_offset=_zone_offset(measured_at),
            recording_method=_recording_method(mapping.kind, anchor),
            device=self._device(anchor),
            time_source=time_source,
            extra=dict(mapping.extra),
        )
        stored = self.queue.offer(reading)
        if stored is None:
            return None
        _LOGGER.debug(
            "Queued %s from %s for %s (version %s)",
            mapping.kind,
            anchor,
            self._entry.title,
            stored.version,
        )
        self._save()
        return stored

    def _device(self, entity_id: str) -> dict[str, str] | None:
        """Manufacturer and model from the device registry; a scale when it weighs."""
        registry_entry = er.async_get(self._hass).async_get(entity_id)
        state = self._hass.states.get(entity_id)
        device_class = (state.attributes.get(ATTR_DEVICE_CLASS) if state else None) or (
            registry_entry.device_class or registry_entry.original_device_class
            if registry_entry
            else None
        )
        device_type = DEVICE_TYPE_SCALE if device_class == DEVICE_CLASS_WEIGHT else None
        device = (
            dr.async_get(self._hass).async_get(registry_entry.device_id)
            if registry_entry and registry_entry.device_id
            else None
        )
        if device is None and device_type is None:
            return None
        info: dict[str, str] = {"type": device_type or DEVICE_TYPE_UNKNOWN}
        if device is not None:
            if device.manufacturer:
                info["manufacturer"] = device.manufacturer
            if device.model:
                info["model"] = device.model
        return info

    # --- The answer -------------------------------------------------------------------

    @callback
    def async_respond(
        self, request: dict[str, Any] | None, *, in_reply_to: str, now: datetime
    ) -> dict[str, Any]:
        """Take what the phone reported and answer with what waits for it.

        Acks and failures are applied before the page is formed, so a reading the
        phone just confirmed is not offered once more. pending and more appear only
        when the request carried writeback.types, and hold only those types.
        """
        types: list[Any] | None = None
        if request is not None:
            if (protocol := request.get("protocol")) not in (None, 1):
                _LOGGER.debug(
                    "%s speaks writeback protocol %s; answering with protocol 1",
                    self._entry.title,
                    protocol,
                )
            ack = request.get("ack")
            if isinstance(ack, list) and ack:
                acked = self.queue.ack(ack, now)
                _LOGGER.debug("%s wrote %s of %s readings", self._entry.title, len(acked), len(ack))
            failed = request.get("failed")
            if isinstance(failed, list) and failed:
                self._report(self.queue.fail(failed))
            if isinstance(raw_types := request.get("types"), list):
                types = raw_types

        self.queue.prune(now)
        pending: list[dict[str, Any]] | None = None
        more: bool | None = None
        if types is not None:
            readings, more = self.queue.page(types)
            pending = [reading.to_wire() for reading in readings]
            if pending:
                _LOGGER.debug(
                    "Offering %s readings to %s%s",
                    len(pending),
                    self._entry.title,
                    ", more waiting" if more else "",
                )
        self._sync_issues()
        self._save()
        return response_body(
            version=self.version,
            in_reply_to=in_reply_to,
            issued_at=now,
            configured=self.configured,
            pending=pending,
            more=more,
        )

    def _report(self, failures: list) -> None:
        """Log what the phone refused: the entity and the code, never the value."""
        for failure in failures:
            reading = failure.reading
            where = reading.entity_id if reading else failure.id
            kind = reading.type if reading else "?"
            if not failure.permanent:
                _LOGGER.debug(
                    "%s could not write %s from %s right now (%s); offered again next time",
                    self._entry.title,
                    kind,
                    where,
                    failure.code,
                )
                continue
            hint = (
                ". Older than 30 days: turn on Accept older measurements in the app"
                if failure.code == CODE_TOO_OLD
                else ""
            )
            _LOGGER.warning(
                "%s did not write %s from %s: %s%s",
                self._entry.title,
                kind,
                where,
                failure.code,
                hint,
            )

    def _sync_issues(self) -> None:
        """One repair per type the phone refused for a reason the user can fix."""
        for kind in WRITEBACK_TYPES:
            issue_id = f"writeback_{self._entry.entry_id}_{kind}"
            code = self.queue.refused.get(kind)
            if code is None or kind not in self.mappings:
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
                continue
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=f"writeback_{code}",
                translation_placeholders={"phone": self._entry.title, "type": kind},
            )


def _zone_offset(moment: datetime) -> str:
    """The configured zone's offset at that moment, as +02:00."""
    raw = moment.astimezone(dt_util.get_default_time_zone()).strftime("%z")
    return f"{raw[:3]}:{raw[3:]}" if len(raw) == 5 else raw


def _recording_method(kind: str, entity_id: str) -> str:
    if kind == TYPE_BLOOD_PRESSURE:
        return RECORDING_ACTIVE
    if entity_id.startswith("input_number."):
        return RECORDING_MANUAL
    return RECORDING_AUTO
