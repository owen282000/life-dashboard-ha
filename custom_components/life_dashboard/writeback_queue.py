"""The queue of measurements waiting for a phone.

Home Assistant offers a reading; the phone writes it to Health Connect and confirms
it in its next request. Everything between those two moments is decided here: what
counts as one measurement, what counts as a correction, what the phone is told when
it refuses one, and how much is kept for a phone that does not come back.

The facts the design rests on:

- An id is "{entity_id}@{measured_at_ms}" and becomes Health Connect's clientRecordId.
  Inserting the same id again is an upsert there, and a higher version wins, so a
  re-delivery is harmless and a correction is the same id with version + 1. That is
  what lets this queue offer a reading again after every kind of loss (an outbox
  dropped on the phone, a crash while writing, a reinstall) without an exchange of
  uuids.
- A scale advertises the same reading for seconds, and Home Assistant reports an
  unchanged state as an event of its own. The same value within ten minutes of the
  previous measurement of an entity is therefore that measurement, not a new one.
- The phone answers with ids and codes only, never with a value, and the codes form
  a closed set: some mean "do not offer this again", the rest "try again next time".

Nothing here imports from Home Assistant, so the whole of it is tested on its own;
writeback.py is the thin layer that listens to states and talks to the store.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from .payload import format_instant, parse_instant

# The types of phase 1, in the order the phone shows them. Each is a Health Connect
# record the app can write; BMI, muscle mass and visceral fat are not offered because
# Health Connect has no record for them.
TYPE_WEIGHT: Final = "weight"
TYPE_HEIGHT: Final = "height"
TYPE_BODY_FAT: Final = "body_fat"
TYPE_LEAN_BODY_MASS: Final = "lean_body_mass"
TYPE_BONE_MASS: Final = "bone_mass"
TYPE_BODY_WATER_MASS: Final = "body_water_mass"
TYPE_BLOOD_PRESSURE: Final = "blood_pressure"
WRITEBACK_TYPES: Final[tuple[str, ...]] = (
    TYPE_WEIGHT,
    TYPE_HEIGHT,
    TYPE_BODY_FAT,
    TYPE_LEAN_BODY_MASS,
    TYPE_BONE_MASS,
    TYPE_BODY_WATER_MASS,
    TYPE_BLOOD_PRESSURE,
)

# The value field per single-value type, spelled as docs/webhook.md spells it, in the
# unit Health Connect wants. Blood pressure carries systolic and diastolic instead.
VALUE_FIELDS: Final[dict[str, str]] = {
    TYPE_WEIGHT: "kilograms",
    TYPE_HEIGHT: "meters",
    TYPE_BODY_FAT: "percentage",
    TYPE_LEAN_BODY_MASS: "kilograms",
    TYPE_BONE_MASS: "kilograms",
    TYPE_BODY_WATER_MASS: "kilograms",
}
FIELD_SYSTOLIC: Final = "systolic"
FIELD_DIASTOLIC: Final = "diastolic"

# The closed set of codes the phone answers a refused reading with.
CODE_PERMISSION_DENIED: Final = "permission_denied"
CODE_UNSUPPORTED_TYPE: Final = "unsupported_type"
CODE_OUT_OF_RANGE: Final = "out_of_range"
CODE_TOO_OLD: Final = "too_old"
CODE_INVALID: Final = "invalid"
CODE_RATE_LIMITED: Final = "rate_limited"
CODE_HC_UNAVAILABLE: Final = "hc_unavailable"
#: The reading leaves the queue: offering it again would only fail again.
PERMANENT_CODES: Final = frozenset(
    {
        CODE_PERMISSION_DENIED,
        CODE_UNSUPPORTED_TYPE,
        CODE_OUT_OF_RANGE,
        CODE_TOO_OLD,
        CODE_INVALID,
    }
)
#: The reading stays: the phone could not write anything at that moment.
TRANSIENT_CODES: Final = frozenset({CODE_RATE_LIMITED, CODE_HC_UNAVAILABLE})
FAILURE_CODES: Final = PERMANENT_CODES | TRANSIENT_CODES
#: The two codes the user can do something about, so they get a repair issue.
REPAIR_CODES: Final = frozenset({CODE_PERMISSION_DENIED, CODE_UNSUPPORTED_TYPE})

# The enum strings Health Connect knows; anything else becomes unknown on the phone.
DEVICE_TYPES: Final = frozenset(
    {
        "unknown",
        "watch",
        "phone",
        "scale",
        "ring",
        "head_mounted",
        "fitness_band",
        "chest_strap",
        "smart_display",
    }
)
BODY_POSITIONS: Final[tuple[str, ...]] = (
    "unknown",
    "standing_up",
    "sitting_down",
    "lying_down",
    "reclining",
)
MEASUREMENT_LOCATIONS: Final[tuple[str, ...]] = (
    "unknown",
    "left_wrist",
    "right_wrist",
    "left_upper_arm",
    "right_upper_arm",
)
RECORDING_AUTO: Final = "auto"
RECORDING_ACTIVE: Final = "active"
RECORDING_MANUAL: Final = "manual"

#: The measured time came from last_changed, for want of a timestamp entity.
TIME_SOURCE_STATE: Final = "state"

DEBOUNCE: Final = timedelta(minutes=10)
MAX_AGE: Final = timedelta(days=90)
MAX_PER_ENTITY: Final = 500
PAGE_SIZE: Final = 200
DELIVERED_MAX: Final = 2000

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def epoch_ms(moment: datetime) -> int:
    """Milliseconds since the epoch, in integer arithmetic so no float rounds it."""
    return (moment.astimezone(UTC) - _EPOCH) // timedelta(milliseconds=1)


def reading_id(entity_id: str, measured_at: datetime) -> str:
    """The id rule: one entity, one measured moment, one id, however often it is seen."""
    return f"{entity_id}@{epoch_ms(measured_at)}"


def configured_types(mapping: dict[str, Any] | None) -> list[str]:
    """The types the options map to an entity, in the order of WRITEBACK_TYPES.

    Blood pressure needs both halves; a single-value type needs its one slot. Anything
    else in the options is not a mapping and is ignored.
    """
    if not isinstance(mapping, dict):
        return []
    found: list[str] = []
    for kind in WRITEBACK_TYPES:
        slots = mapping.get(kind)
        if not isinstance(slots, dict):
            continue
        if kind == TYPE_BLOOD_PRESSURE:
            if slots.get(FIELD_SYSTOLIC) and slots.get(FIELD_DIASTOLIC):
                found.append(kind)
        elif slots.get("entity"):
            found.append(kind)
    return found


def _values_hash(values: dict[str, float]) -> str:
    """A short fingerprint of the values, so delivered readings need not keep them."""
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Reading:
    """One measurement as it will be offered to the phone.

    values holds the type's fields (kilograms, or systolic and diastolic) in the units
    Health Connect wants. extra holds the string fields of blood pressure.
    """

    id: str
    version: int
    type: str
    entity_id: str
    time: datetime
    values: dict[str, float]
    zone_offset: str | None = None
    recording_method: str = RECORDING_AUTO
    device: dict[str, str] | None = None
    time_source: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        """The reading as the phone reads it (section 4.3 of the protocol)."""
        wire: dict[str, Any] = {
            "id": self.id,
            "version": self.version,
            "type": self.type,
            **self.values,
            "time": format_instant(self.time),
        }
        if self.zone_offset:
            wire["zone_offset"] = self.zone_offset
        wire["recording_method"] = self.recording_method
        if self.device:
            wire["device"] = dict(self.device)
        if self.time_source:
            wire["time_source"] = self.time_source
        wire.update(self.extra)
        return wire

    def to_dict(self) -> dict[str, Any]:
        """The reading as the store keeps it: the wire form plus what the queue needs."""
        return {
            **self.to_wire(),
            "entity_id": self.entity_id,
            "time": self.time.astimezone(UTC).isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Reading:
        kind = data["type"]
        if kind == TYPE_BLOOD_PRESSURE:
            values = {
                FIELD_SYSTOLIC: float(data[FIELD_SYSTOLIC]),
                FIELD_DIASTOLIC: float(data[FIELD_DIASTOLIC]),
            }
            extra = {
                name: data[name]
                for name in ("body_position", "measurement_location")
                if isinstance(data.get(name), str)
            }
        else:
            field_name = VALUE_FIELDS[kind]
            values = {field_name: float(data[field_name])}
            extra = {}
        device = data.get("device")
        return cls(
            id=data["id"],
            version=int(data["version"]),
            type=kind,
            entity_id=data["entity_id"],
            time=parse_instant(data["time"]),
            values=values,
            zone_offset=data.get("zone_offset"),
            recording_method=data.get("recording_method", RECORDING_AUTO),
            device=dict(device) if isinstance(device, dict) else None,
            time_source=data.get("time_source"),
            extra=extra,
        )


@dataclass(frozen=True)
class Failure:
    """What the phone said about one reading it did not write."""

    id: str
    code: str
    reading: Reading | None
    #: True when the reading left the queue because of it.
    permanent: bool


@dataclass
class _Seen:
    """The last measurement accepted for an entity, which the debounce compares to."""

    values: dict[str, float]
    time: datetime


@dataclass
class WritebackQueue:
    """Everything one phone's queue remembers.

    pending: id -> the reading, until the phone confirms it or refuses it for good.
    delivered: id -> version, type, moment and value fingerprint, so a re-offer of a
    confirmed reading is recognised without keeping its value around.
    seen: entity -> the last accepted measurement, for the debounce.
    refused: type -> the code that earned it a repair issue, until an ack clears it.
    """

    pending: dict[str, Reading] = field(default_factory=dict)
    delivered: dict[str, dict[str, Any]] = field(default_factory=dict)
    seen: dict[str, _Seen] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)
    acked_total: int = 0
    failed_total: dict[str, int] = field(default_factory=dict)
    last_ack_at: datetime | None = None

    # --- Offering ------------------------------------------------------------

    def offer(self, reading: Reading) -> Reading | None:
        """Take a measurement, and say what it became.

        None when it is the measurement the queue already knows: the same value within
        the debounce of the previous one for that entity, or an id already pending or
        delivered with the same value. A different value on an id that is known is a
        correction and comes back with the next version. Anything else is version 1.
        """
        last = self.seen.get(reading.entity_id)
        if (
            last is not None
            and last.values == reading.values
            and timedelta(0) <= reading.time - last.time <= DEBOUNCE
        ):
            return None

        if (existing := self.pending.get(reading.id)) is not None:
            if existing.values == reading.values:
                self._note_seen(reading)
                return None
            version = existing.version + 1
        elif (done := self.delivered.get(reading.id)) is not None:
            if done.get("h") == _values_hash(reading.values):
                self._note_seen(reading)
                return None
            version = int(done["v"]) + 1
        else:
            version = 1

        stored = replace(reading, version=version)
        self.pending[reading.id] = stored
        self._note_seen(stored)
        return stored

    def rekey(self, old_id: str, measured_at: datetime) -> Reading | None:
        """Move a pending reading to another measured moment, as version 1 of that id.

        For the timestamp entity that updates just after the value it belongs to: the
        reading was queued on last_changed, and the better moment arrived seconds
        later. Only a pending reading moves; a delivered one is the phone's already.
        """
        reading = self.pending.pop(old_id, None)
        if reading is None:
            return None
        moved = replace(
            reading,
            id=reading_id(reading.entity_id, measured_at),
            version=1,
            time=measured_at,
            time_source=None,
        )
        self.pending[moved.id] = moved
        seen = self.seen.get(moved.entity_id)
        if seen is not None and seen.time == reading.time:
            seen.time = measured_at
        return moved

    def _note_seen(self, reading: Reading) -> None:
        """Remember the newest measurement per entity; a backfill never moves it back."""
        last = self.seen.get(reading.entity_id)
        if last is None or reading.time >= last.time:
            self.seen[reading.entity_id] = _Seen(dict(reading.values), reading.time)

    # --- What the phone said -----------------------------------------------------

    def ack(self, ids: list[Any], now: datetime) -> list[Reading]:
        """Take the ids the phone wrote; they leave pending for delivered."""
        acked: list[Reading] = []
        for raw in ids:
            if not isinstance(raw, str):
                continue
            reading = self.pending.pop(raw, None)
            if reading is None:
                continue
            self.delivered[raw] = {
                "v": reading.version,
                "t": reading.type,
                "at": now.astimezone(UTC).isoformat(),
                "h": _values_hash(reading.values),
            }
            self.refused.pop(reading.type, None)
            acked.append(reading)
        if acked:
            self.acked_total += len(acked)
            self.last_ack_at = now
        return acked

    def fail(self, entries: list[Any]) -> list[Failure]:
        """Take the ids the phone refused, with their codes.

        A permanent code takes the reading out of pending; a transient one leaves it
        for the next round. A code outside the set is treated as transient: an app
        newer than this integration may have a reason we do not know, and offering
        the reading again is bounded by the age limit, whereas dropping it is not.
        """
        failures: list[Failure] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            code = entry.get("code")
            if not isinstance(code, str) or not code:
                code = CODE_INVALID
            self.failed_total[code] = self.failed_total.get(code, 0) + 1
            reading = self.pending.get(entry["id"])
            if reading is None:
                # Not ours, or already gone: counted, nothing to report.
                continue
            permanent = code in PERMANENT_CODES
            if permanent:
                del self.pending[entry["id"]]
                if code in REPAIR_CODES:
                    self.refused[reading.type] = code
            failures.append(Failure(entry["id"], code, reading, permanent))
        return failures

    # --- Housekeeping ------------------------------------------------------------

    def prune(self, now: datetime) -> None:
        """Forget what no phone should get any more.

        Pending readings older than MAX_AGE are not offered again, and an entity
        keeps at most MAX_PER_ENTITY of them, oldest out first: a phone that never
        comes back must not grow the store without end. Delivered ids are a ring of
        DELIVERED_MAX or MAX_AGE, whichever is smaller.
        """
        cutoff = now - MAX_AGE
        for reading_key in [key for key, r in self.pending.items() if r.time < cutoff]:
            del self.pending[reading_key]

        per_entity: dict[str, list[Reading]] = {}
        for reading in self.pending.values():
            per_entity.setdefault(reading.entity_id, []).append(reading)
        for readings in per_entity.values():
            if len(readings) <= MAX_PER_ENTITY:
                continue
            readings.sort(key=lambda r: (r.time, r.id))
            for reading in readings[: len(readings) - MAX_PER_ENTITY]:
                del self.pending[reading.id]

        iso_cutoff = cutoff.astimezone(UTC).isoformat()
        for key in [key for key, done in self.delivered.items() if done.get("at", "") < iso_cutoff]:
            del self.delivered[key]
        if len(self.delivered) > DELIVERED_MAX:
            ordered = sorted(self.delivered, key=lambda key: self.delivered[key].get("at", ""))
            for key in ordered[: len(self.delivered) - DELIVERED_MAX]:
                del self.delivered[key]

    def page(self, types: list[Any], *, limit: int = PAGE_SIZE) -> tuple[list[Reading], bool]:
        """The readings for one answer: the oldest first, only the types asked for.

        The second value says whether more are waiting, so the phone asks again.
        """
        wanted = {kind for kind in types if isinstance(kind, str)}
        matching = sorted(
            (r for r in self.pending.values() if r.type in wanted),
            key=lambda r: (r.time, r.id),
        )
        return matching[:limit], len(matching) > limit

    # --- Persistence and diagnostics ------------------------------------------------

    def counts(self) -> dict[str, Any]:
        """Numbers for diagnostics: how many, of what, never which value or when."""
        pending: dict[str, int] = {}
        for reading in self.pending.values():
            pending[reading.type] = pending.get(reading.type, 0) + 1
        delivered: dict[str, int] = {}
        for done in self.delivered.values():
            kind = str(done.get("t", "?"))
            delivered[kind] = delivered.get(kind, 0) + 1
        return {
            "pending": dict(sorted(pending.items())),
            "delivered": dict(sorted(delivered.items())),
            "acked_total": self.acked_total,
            "failed": dict(sorted(self.failed_total.items())),
            "last_ack_at": self.last_ack_at.isoformat() if self.last_ack_at else None,
            "refused": dict(sorted(self.refused.items())),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending": {key: reading.to_dict() for key, reading in self.pending.items()},
            "delivered": dict(self.delivered),
            "seen": {
                entity_id: {"values": seen.values, "time": seen.time.astimezone(UTC).isoformat()}
                for entity_id, seen in self.seen.items()
            },
            "refused": dict(self.refused),
            "acked_total": self.acked_total,
            "failed_total": dict(self.failed_total),
            "last_ack_at": self.last_ack_at.isoformat() if self.last_ack_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> WritebackQueue:
        if not data:
            return cls()
        queue = cls()
        for key, raw in (data.get("pending") or {}).items():
            try:
                queue.pending[key] = Reading.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                continue
        queue.delivered = {
            key: dict(value)
            for key, value in (data.get("delivered") or {}).items()
            if isinstance(value, dict)
        }
        for entity_id, raw in (data.get("seen") or {}).items():
            try:
                queue.seen[entity_id] = _Seen(
                    {name: float(v) for name, v in raw["values"].items()},
                    parse_instant(raw["time"]),
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        queue.refused = {
            kind: code
            for kind, code in (data.get("refused") or {}).items()
            if isinstance(code, str)
        }
        queue.acked_total = int(data.get("acked_total") or 0)
        queue.failed_total = {code: int(n) for code, n in (data.get("failed_total") or {}).items()}
        last = data.get("last_ack_at")
        if isinstance(last, str):
            try:
                queue.last_ack_at = parse_instant(last)
            except ValueError:
                queue.last_ack_at = None
        return queue


__all__ = [
    "BODY_POSITIONS",
    "DEBOUNCE",
    "DELIVERED_MAX",
    "DEVICE_TYPES",
    "FAILURE_CODES",
    "FIELD_DIASTOLIC",
    "FIELD_SYSTOLIC",
    "MAX_AGE",
    "MAX_PER_ENTITY",
    "MEASUREMENT_LOCATIONS",
    "PAGE_SIZE",
    "PERMANENT_CODES",
    "REPAIR_CODES",
    "TRANSIENT_CODES",
    "TYPE_BLOOD_PRESSURE",
    "VALUE_FIELDS",
    "WRITEBACK_TYPES",
    "Failure",
    "Reading",
    "WritebackQueue",
    "configured_types",
    "epoch_ms",
    "reading_id",
]
