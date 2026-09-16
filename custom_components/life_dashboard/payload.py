"""Signature verification and payload parsing for Life Dashboard.

This module deliberately imports nothing from Home Assistant, so the whole contract
with the phone can be unit tested on its own. The webhook handler feeds it a decoded
JSON object and gets back the sensor updates that object justifies.

The payload contract is documented in the companion app repository, docs/webhook.md
and docs/webhook-schema.json. The facts this module depends on:

- One sync is one POST per source; a backlog produces several POSTs, and a failed
  delivery is re-POSTed later from the app's outbox. So the same record arrives more
  than once and every update carries measured_at for the caller to order on.
- Records are optional everywhere. A payload only carries the arrays that had data.
- Nine dense series can arrive bucketed instead of raw, under the same key, with
  "bucket_start" as the reliable discriminator.
- The iOS app sends the same field names but no daily_totals, and its blood pressure
  records can lack "diastolic".
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Final

SIGNATURE_HEADER: Final = "X-Signature"
SIGNATURE_PREFIX: Final = "sha256="

# Payload sources the app can send. healthkit_ios is the iOS companion app.
SOURCE_HEALTH_CONNECT: Final = "health_connect"
SOURCE_HEALTHKIT_IOS: Final = "healthkit_ios"
SOURCE_SCREEN_TIME: Final = "screen_time"
HEALTH_SOURCES: Final = frozenset({SOURCE_HEALTH_CONNECT, SOURCE_HEALTHKIT_IOS})

# Every array key a health payload can carry, for counting records. From
# docs/webhook.md; the app omits an array entirely when it has no records.
HEALTH_ARRAYS: Final = frozenset(
    {
        "steps",
        "sleep",
        "heart_rate",
        "distance",
        "active_calories",
        "total_calories",
        "weight",
        "height",
        "blood_pressure",
        "blood_glucose",
        "oxygen_saturation",
        "body_temperature",
        "respiratory_rate",
        "resting_heart_rate",
        "exercise",
        "hydration",
        "nutrition",
        "mindfulness",
        "body_fat",
        "lean_body_mass",
        "bone_mass",
        "body_water_mass",
        "heart_rate_variability",
        "menstruation_period",
        "menstruation_flow",
        "basal_metabolic_rate",
        "vo2_max",
        "skin_temperature",
        "basal_body_temperature",
        "intermenstrual_bleeding",
        "ovulation_test",
        "cervical_mucus",
        "sexual_activity",
    }
)

# Sensor keys.
KEY_LAST_HEALTH_SYNC: Final = "last_health_sync"
KEY_LAST_SCREEN_TIME_SYNC: Final = "last_screen_time_sync"

# State classes, as the string values Home Assistant's SensorStateClass uses.
STATE_CLASS_MEASUREMENT: Final = "measurement"
STATE_CLASS_TOTAL: Final = "total"


def signature_for(secret: str, body: bytes) -> str:
    """Build the X-Signature header value the app sends for this body."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(secret: str, body: bytes, header_value: str | None) -> bool:
    """Check the signature over the raw body, in constant time.

    The app only sends the header when a secret is configured, so a missing header
    means the user did not paste the secret and the request must be refused.
    """
    if not header_value:
        return False
    return hmac.compare_digest(signature_for(secret, body), header_value)


# Java's Instant.toString() and Swift's ISO8601DateFormatter both emit UTC with a Z,
# with zero to nine fractional digits. Python accepts at most six.
_FRACTION = re.compile(r"\.(\d{1,9})")


def parse_instant(value: Any) -> datetime:
    """Parse an ISO-8601 instant from the app into a timezone-aware datetime.

    Raises ValueError on anything that is not a usable timestamp, including a naive
    one, so a caller can skip the record.
    """
    if not isinstance(value, str):
        raise ValueError(f"not a timestamp: {value!r}")

    def _truncate(match: re.Match[str]) -> str:
        return "." + match.group(1)[:6]

    parsed = datetime.fromisoformat(_FRACTION.sub(_truncate, value))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp without a timezone: {value!r}")
    return parsed


def _parse_date(value: Any) -> date:
    """Parse a YYYY-MM-DD day from the app."""
    if not isinstance(value, str):
        raise ValueError(f"not a date: {value!r}")
    return date.fromisoformat(value)


def _number(value: Any) -> float | int:
    """Return value if it is a real number, else raise.

    bool is rejected on purpose: JSON true would otherwise become 1.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"not a number: {value!r}")
    return value


@dataclass(frozen=True)
class SensorUpdate:
    """One value for one sensor, with the moment it describes.

    measured_at is what the caller orders on: an update older than the value a
    sensor already holds is dropped, which makes re-delivered batches and backfill
    harmless without keeping a record of uuids.
    """

    key: str
    value: float | int | str | datetime
    measured_at: datetime
    attributes: dict[str, Any] = field(default_factory=dict)
    last_reset: datetime | None = None


@dataclass(frozen=True)
class SensorSpec:
    """How one sensor presents itself in Home Assistant.

    device_class and state_class are the string values of Home Assistant's enums, so
    this module stays free of Home Assistant imports; sensor.py maps them back.
    """

    key: str
    unit: str | None = None
    device_class: str | None = None
    #: None on every sensor: the recorder's own statistic would sit next to the
    #: correct one from history.py and, for a latest-value sensor, be wrong.
    state_class: str | None = None
    precision: int | None = None
    diagnostic: bool = False


@dataclass(frozen=True)
class _Latest:
    """A latest-value sensor: which array, which field, which time field."""

    key: str
    array: str
    value_field: str
    time_field: str = "time"
    to_int: bool = False


# The latest-value sensors, in the order they appear in the README table. Keys and
# units match the app's MQTT sensors so a user moving over sees the same names.
_LATEST: Final[tuple[_Latest, ...]] = (
    _Latest("heart_rate", "heart_rate", "bpm", to_int=True),
    _Latest("resting_heart_rate", "resting_heart_rate", "bpm", to_int=True),
    _Latest("heart_rate_variability", "heart_rate_variability", "heart_rate_variability_millis"),
    _Latest("sleep_duration", "sleep", "duration_seconds", time_field="session_end_time"),
    _Latest("weight", "weight", "kilograms"),
    _Latest("blood_pressure_systolic", "blood_pressure", "systolic"),
    _Latest("blood_pressure_diastolic", "blood_pressure", "diastolic"),
    _Latest("blood_glucose", "blood_glucose", "mmol_per_liter"),
    _Latest("oxygen_saturation", "oxygen_saturation", "percentage"),
    _Latest("body_temperature", "body_temperature", "celsius"),
    _Latest("skin_temperature_delta", "skin_temperature", "delta_celsius"),
    _Latest("basal_body_temperature", "basal_body_temperature", "celsius"),
    _Latest("respiratory_rate", "respiratory_rate", "rate"),
    _Latest("hydration", "hydration", "liters", time_field="end_time"),
    _Latest("body_fat", "body_fat", "percentage"),
    _Latest("lean_body_mass", "lean_body_mass", "kilograms"),
    _Latest("bone_mass", "bone_mass", "kilograms"),
    _Latest("body_water_mass", "body_water_mass", "kilograms"),
    _Latest("basal_metabolic_rate", "basal_metabolic_rate", "kilocalories_per_day"),
    _Latest("vo2_max", "vo2_max", "vo2_ml_per_min_per_kg"),
    _Latest("height", "height", "meters"),
)

# Series that can arrive bucketed and are averaged: the bucket's avg is the value.
# The accumulated ones (steps, distance, calories) are ignored when bucketed,
# because their day figures come from daily_totals instead.
_BUCKETED_MEASURED: Final = {
    "heart_rate": "heart_rate",
    "heart_rate_variability": "heart_rate_variability",
    "oxygen_saturation": "oxygen_saturation",
    "respiratory_rate": "respiratory_rate",
    "skin_temperature": "skin_temperature_delta",
}

# The four day totals, from daily_totals only.
_TODAY: Final[tuple[tuple[str, str], ...]] = (
    ("steps_today", "steps"),
    ("distance_today", "distance_meters"),
    ("active_calories_today", "active_calories"),
    ("total_calories_today", "total_calories"),
)

SENSOR_SPECS: Final[dict[str, SensorSpec]] = {
    spec.key: spec
    for spec in (
        # Latest value.
        SensorSpec("heart_rate", "bpm", precision=0),
        SensorSpec("resting_heart_rate", "bpm", precision=0),
        SensorSpec("heart_rate_variability", "ms", precision=1),
        SensorSpec("sleep_duration", "min", "duration", precision=0),
        SensorSpec("weight", "kg", "weight", precision=1),
        SensorSpec("blood_pressure_systolic", "mmHg", precision=0),
        SensorSpec("blood_pressure_diastolic", "mmHg", precision=0),
        SensorSpec("blood_glucose", "mmol/L", "blood_glucose_concentration", precision=2),
        SensorSpec("oxygen_saturation", "%", precision=1),
        SensorSpec("body_temperature", "°C", "temperature", precision=1),
        SensorSpec("skin_temperature_delta", "°C", "temperature", precision=2),
        SensorSpec("basal_body_temperature", "°C", "temperature", precision=1),
        SensorSpec("respiratory_rate", "breaths/min", precision=1),
        # No volume device class: Home Assistant only allows that with TOTAL, which
        # fits a tank rather than "how much the last drink was".
        SensorSpec("hydration", "L", precision=2),
        SensorSpec("body_fat", "%", precision=1),
        SensorSpec("lean_body_mass", "kg", "weight", precision=1),
        SensorSpec("bone_mass", "kg", "weight", precision=1),
        SensorSpec("body_water_mass", "kg", "weight", precision=1),
        SensorSpec("basal_metabolic_rate", "kcal/d", precision=0),
        SensorSpec("vo2_max", "mL/min/kg", precision=1),
        SensorSpec("height", "m", "distance", precision=2),
        # Day totals. TOTAL rather than TOTAL_INCREASING: Health Connect can revise a
        # day downward after deduplicating, and TOTAL_INCREASING reads that as a reset.
        SensorSpec("steps_today", "steps", precision=0),
        SensorSpec("distance_today", "m", "distance", precision=0),
        # No energy device class for calories: that would offer them to the Energy
        # dashboard, which is not what these are.
        SensorSpec("active_calories_today", "kcal", precision=0),
        SensorSpec("total_calories_today", "kcal", precision=0),
        # Screen time.
        SensorSpec("screen_time_today", "min", "duration", precision=0),
        SensorSpec("screen_time_yesterday", "min", "duration", precision=0),
        # A text sensor: no unit, no device class and no state class, or Home
        # Assistant refuses a non-numeric state.
        SensorSpec("screen_time_top_app"),
        # Diagnostics.
        SensorSpec(KEY_LAST_HEALTH_SYNC, None, "timestamp", diagnostic=True),
        SensorSpec(KEY_LAST_SCREEN_TIME_SYNC, None, "timestamp", diagnostic=True),
    )
}


def _midnight(day: date, tz: tzinfo) -> datetime:
    """The start of a local day, as an aware datetime."""
    return datetime.combine(day, time.min, tzinfo=tz)


def _record_attributes(record: dict[str, Any]) -> dict[str, Any]:
    """Attributes shared by every latest-value sensor."""
    attributes: dict[str, Any] = {}
    for name in ("source", "uuid"):
        value = record.get(name)
        if isinstance(value, str) and value:
            attributes[name] = value
    return attributes


def _latest_from_records(
    records: list[Any], spec: _Latest
) -> tuple[dict[str, Any], datetime, float | int] | None:
    """Pick the newest record that actually carries a usable value.

    Filtering on the value field, not just the time field, is what makes iOS blood
    pressure work: a systolic sample without a matching diastolic one is sent with
    "systolic" only, and the diastolic sensor has to look further back.
    """
    best: tuple[dict[str, Any], datetime, float | int] | None = None
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            measured_at = parse_instant(record.get(spec.time_field))
            value = _number(record.get(spec.value_field))
        except ValueError:
            continue
        if best is None or measured_at > best[1]:
            best = (record, measured_at, value)
    return best


def _bucketed_update(key: str, buckets: list[Any]) -> SensorUpdate | None:
    """Turn the newest closed bucket of an averaged series into an update."""
    best: tuple[datetime, dict[str, Any]] | None = None
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        try:
            bucket_end = parse_instant(bucket.get("bucket_end"))
            _number(bucket.get("avg"))
        except ValueError:
            continue
        if best is None or bucket_end > best[0]:
            best = (bucket_end, bucket)
    if best is None:
        return None

    bucket_end, bucket = best
    attributes: dict[str, Any] = {}
    for name in ("sample_count", "min", "max"):
        value = bucket.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            attributes[name] = value
    sources = bucket.get("sources")
    if isinstance(sources, list):
        named = [s for s in sources if isinstance(s, str) and s]
        if named:
            attributes["sources"] = ", ".join(named)
    return SensorUpdate(
        key=key,
        value=bucket["avg"],
        measured_at=bucket_end,
        attributes=attributes,
    )


def _is_bucketed(records: list[Any]) -> bool:
    """A bucketed series replaces its raw array under the same key."""
    return bool(records) and isinstance(records[0], dict) and "bucket_start" in records[0]


def _parse_health(data: dict[str, Any], tz: tzinfo) -> list[SensorUpdate]:
    """Sensor updates from a health payload, Android or iOS."""
    updates: list[SensorUpdate] = []

    # Day totals. The iOS app sends no daily_totals at all, so these stay absent
    # for an iPhone, exactly as with its MQTT sensors.
    totals = data.get("daily_totals")
    if isinstance(totals, list):
        newest: tuple[date, dict[str, Any]] | None = None
        for entry in totals:
            if not isinstance(entry, dict):
                continue
            try:
                day = _parse_date(entry.get("date"))
            except ValueError:
                continue
            if newest is None or day > newest[0]:
                newest = (day, entry)
        if newest is not None:
            day, entry = newest
            midnight = _midnight(day, tz)
            for key, field_name in _TODAY:
                try:
                    value = _number(entry.get(field_name))
                except ValueError:
                    continue
                updates.append(
                    SensorUpdate(
                        key=key,
                        value=int(value) if key == "steps_today" else float(value),
                        measured_at=midnight,
                        attributes={"date": day.isoformat()},
                        last_reset=midnight,
                    )
                )

    # Latest values, raw or bucketed.
    for spec in _LATEST:
        records = data.get(spec.array)
        if not isinstance(records, list) or not records:
            continue
        if _is_bucketed(records):
            key = _BUCKETED_MEASURED.get(spec.array)
            # Accumulated series (steps, distance, calories) carry no average and
            # their day figures come from daily_totals, so a bucket adds nothing.
            if key is not None and (update := _bucketed_update(key, records)) is not None:
                updates.append(update)
            continue
        found = _latest_from_records(records, spec)
        if found is None:
            continue
        record, measured_at, value = found
        if spec.key == "sleep_duration":
            value = value / 60
        updates.append(
            SensorUpdate(
                key=spec.key,
                value=int(value) if spec.to_int else float(value),
                measured_at=measured_at,
                attributes=_record_attributes(record),
            )
        )

    updates.append(_sync_update(data, KEY_LAST_HEALTH_SYNC, tz))
    return updates


def _parse_screen_time(data: dict[str, Any], tz: tzinfo) -> list[SensorUpdate]:
    """Sensor updates from a screen time payload.

    Every sync re-sends the last seven days recomputed, so the newest payload wins
    per date and the day entries are ordered on their own date, not on arrival.
    """
    updates: list[SensorUpdate] = []
    days: dict[date, dict[str, Any]] = {}
    entries = data.get("screen_time")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                days[_parse_date(entry.get("date"))] = entry
            except ValueError:
                continue

    if days:
        today = max(days)
        wanted = (
            ("screen_time_today", today),
            ("screen_time_yesterday", today - timedelta(days=1)),
        )
        for key, day in wanted:
            entry = days.get(day)
            if entry is None:
                continue
            try:
                minutes = _number(entry.get("total_screen_time_minutes"))
            except ValueError:
                continue
            midnight = _midnight(day, tz)
            updates.append(
                SensorUpdate(
                    key=key,
                    value=int(minutes),
                    measured_at=midnight,
                    attributes=_screen_time_attributes(entry, day),
                    last_reset=midnight if key == "screen_time_today" else None,
                )
            )

        if (top := _top_app(days[today])) is not None:
            name, package, minutes = top
            updates.append(
                SensorUpdate(
                    key="screen_time_top_app",
                    value=name,
                    measured_at=_midnight(today, tz),
                    attributes={
                        "package": package,
                        "minutes": minutes,
                        "date": today.isoformat(),
                    },
                )
            )

    updates.append(_sync_update(data, KEY_LAST_SCREEN_TIME_SYNC, tz))
    return updates


def _apps(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The usable app rows of a screen time day."""
    apps = entry.get("apps")
    if not isinstance(apps, list):
        return []
    usable = []
    for app in apps:
        if not isinstance(app, dict) or not isinstance(app.get("name"), str):
            continue
        try:
            _number(app.get("minutes"))
        except ValueError:
            continue
        usable.append(app)
    return usable


def _screen_time_attributes(entry: dict[str, Any], day: date) -> dict[str, Any]:
    """Attributes for a screen time day sensor."""
    apps = _apps(entry)
    top = sorted(apps, key=lambda app: app["minutes"], reverse=True)[:5]
    return {
        "date": day.isoformat(),
        "app_count": len(apps),
        "top_apps": ", ".join(f"{app['name']} ({int(app['minutes'])} min)" for app in top),
    }


def _top_app(entry: dict[str, Any]) -> tuple[str, str, int] | None:
    """The most used app of a day, as (name, package, minutes)."""
    apps = _apps(entry)
    if not apps:
        return None
    app = max(apps, key=lambda app: app["minutes"])
    package = app.get("package")
    return (
        app["name"],
        package if isinstance(package, str) else "",
        int(app["minutes"]),
    )


def _sync_update(data: dict[str, Any], key: str, tz: tzinfo) -> SensorUpdate:
    """The diagnostic timestamp sensor for a payload.

    Test pings update this too. The user knows exactly when they pressed Test, so a
    moving timestamp is feedback rather than a surprise.
    """
    try:
        measured_at = parse_instant(data.get("timestamp"))
    except ValueError:
        measured_at = datetime.now(tz)

    attributes: dict[str, Any] = {}
    if isinstance(version := data.get("app_version"), str) and version:
        attributes["app_version"] = version
    if isinstance(source := data.get("source"), str) and source:
        attributes["source"] = source

    if key == KEY_LAST_HEALTH_SYNC:
        attributes["backfill"] = data.get("backfill") is True
        attributes["record_count"] = sum(
            len(value)
            for name, value in data.items()
            if name in HEALTH_ARRAYS and isinstance(value, list)
        )
    elif isinstance(device := data.get("device"), str) and device:
        attributes["device"] = device

    return SensorUpdate(key=key, value=measured_at, measured_at=measured_at, attributes=attributes)


def parse_payload(data: dict[str, Any], *, tz: tzinfo) -> list[SensorUpdate]:
    """Turn one decoded payload into the sensor updates it justifies.

    Never raises on a JSON object: a record it cannot read is skipped, because one
    unreadable record must not cost the user the rest of a sync.
    """
    source = data.get("source")

    # A test ping carries no data, only proof that URL and secret are right.
    if data.get("test") is True:
        key = KEY_LAST_SCREEN_TIME_SYNC if source == SOURCE_SCREEN_TIME else KEY_LAST_HEALTH_SYNC
        return [_sync_update(data, key, tz)]

    if source == SOURCE_SCREEN_TIME:
        return _parse_screen_time(data, tz)
    return _parse_health(data, tz)
