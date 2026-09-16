"""Turning payloads into history.

A sensor holds one value and Home Assistant stamps it with the moment it arrived, so a
year of backfill written to sensors lands entirely on today. Long-term statistics are the
other store: hourly rows an integration may write for any hour in the past. This module
decides what those rows should say.

It keeps a ledger rather than writing rows straight from a payload, for two reasons.
Statistics sums are cumulative, so changing a day in the past means rewriting every later
row, which needs the earlier amounts. And the same records arrive more than once, from
the app's outbox, from a re-run backfill, or after an edit in the source app, so every
contribution has to be replaceable rather than additive.

Nothing here imports from Home Assistant: the arithmetic is the part worth testing
exhaustively, and statistics.py is the thin layer that talks to the recorder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, tzinfo
from typing import Any, Final

from .payload import parse_instant

# Day sums that come from Health Connect's own aggregate. Never summed from raw records:
# a phone and a watch both record the same walk, and only the aggregate deduplicates that.
DAILY_TOTAL_KEYS: Final[dict[str, str]] = {
    "steps": "steps",
    "distance": "distance_meters",
    "active_calories": "active_calories",
    "total_calories": "total_calories",
}


@dataclass(frozen=True)
class SessionSeries:
    """A day sum built from records that each cover a stretch of time."""

    key: str
    array: str
    value_field: str
    time_field: str
    #: Seconds to minutes for the durations; 1 for a quantity like litres.
    divisor: float = 1.0


# Keyed by uuid, so a re-delivered or edited record replaces its own contribution.
SESSION_SERIES: Final[tuple[SessionSeries, ...]] = (
    SessionSeries("sleep_minutes", "sleep", "duration_seconds", "session_end_time", 60.0),
    SessionSeries("exercise_minutes", "exercise", "duration_seconds", "end_time", 60.0),
    SessionSeries("mindfulness_minutes", "mindfulness", "duration_seconds", "end_time", 60.0),
    SessionSeries("hydration_total", "hydration", "liters", "end_time"),
)


@dataclass(frozen=True)
class MeasuredSeries:
    """An hourly mean/min/max built from point-in-time readings."""

    key: str
    array: str
    value_field: str
    time_field: str = "time"


# The measured half of the sensor table, plus the bucketed series, which arrive as
# aggregates already. Two keys come out of blood_pressure, as for the sensors.
MEASURED_SERIES: Final[tuple[MeasuredSeries, ...]] = (
    MeasuredSeries("heart_rate", "heart_rate", "bpm"),
    MeasuredSeries("resting_heart_rate", "resting_heart_rate", "bpm"),
    MeasuredSeries(
        "heart_rate_variability", "heart_rate_variability", "heart_rate_variability_millis"
    ),
    MeasuredSeries("weight", "weight", "kilograms"),
    MeasuredSeries("blood_pressure_systolic", "blood_pressure", "systolic"),
    MeasuredSeries("blood_pressure_diastolic", "blood_pressure", "diastolic"),
    MeasuredSeries("blood_glucose", "blood_glucose", "mmol_per_liter"),
    MeasuredSeries("oxygen_saturation", "oxygen_saturation", "percentage"),
    MeasuredSeries("body_temperature", "body_temperature", "celsius"),
    MeasuredSeries("skin_temperature_delta", "skin_temperature", "delta_celsius"),
    MeasuredSeries("basal_body_temperature", "basal_body_temperature", "celsius"),
    MeasuredSeries("respiratory_rate", "respiratory_rate", "rate"),
    MeasuredSeries("body_fat", "body_fat", "percentage"),
    MeasuredSeries("lean_body_mass", "lean_body_mass", "kilograms"),
    MeasuredSeries("bone_mass", "bone_mass", "kilograms"),
    MeasuredSeries("body_water_mass", "body_water_mass", "kilograms"),
    MeasuredSeries("basal_metabolic_rate", "basal_metabolic_rate", "kilocalories_per_day"),
    MeasuredSeries("vo2_max", "vo2_max", "vo2_ml_per_min_per_kg"),
    MeasuredSeries("height", "height", "meters"),
)

# Which measured key a bucketed array feeds. The accumulated series (steps, distance,
# calories) are not here: their day figures come from daily_totals.
BUCKETED_KEYS: Final[dict[str, str]] = {
    "heart_rate": "heart_rate",
    "heart_rate_variability": "heart_rate_variability",
    "oxygen_saturation": "oxygen_saturation",
    "respiratory_rate": "respiratory_rate",
    "skin_temperature": "skin_temperature_delta",
}

# Minutes on the phone per local day, from the app's own per-day totals. Every screen
# time sync re-sends the last seven days, so the newest figure for a date always wins.
SCREEN_TIME_KEY: Final = "screen_time"

DAY_KEYS: Final[frozenset[str]] = frozenset(
    [*DAILY_TOTAL_KEYS, *(series.key for series in SESSION_SERIES), SCREEN_TIME_KEY]
)
HOUR_KEYS: Final[frozenset[str]] = frozenset(series.key for series in MEASURED_SERIES)


@dataclass
class HourBucket:
    """One hour of a measured quantity."""

    count: int
    mean: float
    minimum: float
    maximum: float

    def merged(self, other: HourBucket) -> HourBucket:
        """Combine two deliveries of the same hour.

        The arithmetic the app's own docs prescribe for merging buckets: add the counts,
        take the wider range, and weight the means. Merging an hour with itself therefore
        leaves mean, min and max alone and only inflates the count, which is why measured
        series need no per-record bookkeeping to survive a re-delivery.
        """
        total = self.count + other.count
        if total <= 0:
            return HourBucket(0, self.mean, self.minimum, self.maximum)
        return HourBucket(
            count=total,
            mean=(self.mean * self.count + other.mean * other.count) / total,
            minimum=min(self.minimum, other.minimum),
            maximum=max(self.maximum, other.maximum),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"n": self.count, "mean": self.mean, "min": self.minimum, "max": self.maximum}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HourBucket:
        return cls(int(data["n"]), float(data["mean"]), float(data["min"]), float(data["max"]))


@dataclass
class Ledger:
    """Everything the statistics can be rebuilt from.

    days: key -> "YYYY-MM-DD" -> amount, for the quantities Health Connect aggregates.
    sessions: key -> uuid -> (day, amount), so a record replaces its own contribution.
    hours: key -> ISO hour -> bucket.
    """

    days: dict[str, dict[str, float]] = field(default_factory=dict)
    sessions: dict[str, dict[str, list]] = field(default_factory=dict)
    hours: dict[str, dict[str, HourBucket]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "sessions": self.sessions,
            "hours": {
                key: {hour: bucket.to_dict() for hour, bucket in buckets.items()}
                for key, buckets in self.hours.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Ledger:
        if not data:
            return cls()
        return cls(
            days={k: dict(v) for k, v in (data.get("days") or {}).items()},
            sessions={
                k: {u: list(a) for u, a in v.items()}
                for k, v in (data.get("sessions") or {}).items()
            },
            hours={
                key: {hour: HourBucket.from_dict(b) for hour, b in buckets.items()}
                for key, buckets in (data.get("hours") or {}).items()
            },
        )

    def day_totals(self, key: str) -> dict[str, float]:
        """Every day this key has an amount for, sessions folded into the day sums."""
        totals = dict(self.days.get(key, {}))
        for day, amount in self.sessions.get(key, {}).values():
            totals[day] = totals.get(day, 0.0) + amount
        return totals


@dataclass(frozen=True)
class HistoryChange:
    """What one payload touched, so only the affected rows are rewritten."""

    #: key -> the earliest local day whose amount changed.
    day_keys: dict[str, date] = field(default_factory=dict)
    #: key -> the earliest hour whose bucket changed.
    hour_keys: dict[str, datetime] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.day_keys and not self.hour_keys


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _local_day(moment: datetime, tz: tzinfo) -> date:
    return moment.astimezone(tz).date()


def _hour_key(moment: datetime) -> str:
    """The UTC hour a reading belongs to. Statistics rows must start on the hour."""
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0).isoformat()


def _note_day(changes: dict[str, date], key: str, day: date) -> None:
    current = changes.get(key)
    if current is None or day < current:
        changes[key] = day


def _note_hour(changes: dict[str, datetime], key: str, hour: datetime) -> None:
    current = changes.get(key)
    if current is None or hour < current:
        changes[key] = hour


def apply_payload(ledger: Ledger, data: dict[str, Any], *, tz: tzinfo) -> HistoryChange:
    """Fold one payload into the ledger, and report what it changed.

    Never raises on a JSON object: a record it cannot read is skipped, exactly as the
    sensor parsing does, because one bad record must not cost a whole backfill window.
    """
    day_changes: dict[str, date] = {}
    hour_changes: dict[str, datetime] = {}

    if data.get("test") is True:
        return HistoryChange()

    _apply_daily_totals(ledger, data, day_changes)
    _apply_screen_time(ledger, data, day_changes)
    _apply_sessions(ledger, data, tz, day_changes)
    _apply_measured(ledger, data, hour_changes)

    return HistoryChange(day_keys=day_changes, hour_keys=hour_changes)


def _apply_daily_totals(ledger: Ledger, data: dict[str, Any], changes: dict[str, date]) -> None:
    """A day's aggregate replaces what we had: it is the authority for that day."""
    totals = data.get("daily_totals")
    if not isinstance(totals, list):
        return

    for entry in totals:
        if not isinstance(entry, dict):
            continue
        try:
            day = date.fromisoformat(entry.get("date", ""))
        except (TypeError, ValueError):
            continue
        for key, field_name in DAILY_TOTAL_KEYS.items():
            amount = _number(entry.get(field_name))
            if amount is None:
                continue
            days = ledger.days.setdefault(key, {})
            iso = day.isoformat()
            if days.get(iso) != amount:
                days[iso] = amount
                _note_day(changes, key, day)


def _apply_screen_time(ledger: Ledger, data: dict[str, Any], changes: dict[str, date]) -> None:
    """A day's screen time replaces what we had, like a daily total."""
    entries = data.get("screen_time")
    if not isinstance(entries, list):
        return

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            day = date.fromisoformat(entry.get("date", ""))
        except (TypeError, ValueError):
            continue
        minutes = _number(entry.get("total_screen_time_minutes"))
        if minutes is None:
            continue
        days = ledger.days.setdefault(SCREEN_TIME_KEY, {})
        iso = day.isoformat()
        if days.get(iso) != minutes:
            days[iso] = minutes
            _note_day(changes, SCREEN_TIME_KEY, day)


def _apply_sessions(
    ledger: Ledger, data: dict[str, Any], tz: tzinfo, changes: dict[str, date]
) -> None:
    """Sessions are summed per day and keyed by uuid, so a re-delivery replaces itself."""
    for series in SESSION_SERIES:
        records = data.get(series.array)
        if not isinstance(records, list):
            continue

        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                moment = parse_instant(record.get(series.time_field))
            except ValueError:
                continue
            raw = _number(record.get(series.value_field))
            if raw is None:
                continue

            amount = raw / series.divisor
            day = _local_day(moment, tz)
            # A record without a uuid still cannot be counted twice: its moment and value
            # identify it well enough for the only case that matters, the same batch
            # arriving again.
            uuid = record.get("uuid")
            identity = uuid if isinstance(uuid, str) and uuid else f"{moment.isoformat()}:{raw}"

            known = ledger.sessions.setdefault(series.key, {})
            previous = known.get(identity)
            if previous == [day.isoformat(), amount]:
                continue
            if previous is not None:
                # An edited record can move to another day; both days change.
                _note_day(changes, series.key, date.fromisoformat(previous[0]))
            known[identity] = [day.isoformat(), amount]
            _note_day(changes, series.key, day)


def _apply_measured(ledger: Ledger, data: dict[str, Any], changes: dict[str, datetime]) -> None:
    """Point-in-time readings become hourly buckets; bucketed series merge straight in."""
    for series in MEASURED_SERIES:
        records = data.get(series.array)
        if not isinstance(records, list) or not records:
            continue

        first = records[0]
        if isinstance(first, dict) and "bucket_start" in first:
            key = BUCKETED_KEYS.get(series.array)
            if key == series.key:
                _apply_buckets(ledger, key, records, changes)
            continue

        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                moment = parse_instant(record.get(series.time_field))
            except ValueError:
                continue
            value = _number(record.get(series.value_field))
            if value is None:
                continue
            _merge_hour(
                ledger, series.key, _hour_key(moment), HourBucket(1, value, value, value), changes
            )


def _apply_buckets(
    ledger: Ledger, key: str, buckets: list[Any], changes: dict[str, datetime]
) -> None:
    """A bucketed series is already aggregated; its window may be finer than an hour."""
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        try:
            start = parse_instant(bucket.get("bucket_start"))
        except ValueError:
            continue
        mean = _number(bucket.get("avg"))
        if mean is None:
            continue
        count = _number(bucket.get("sample_count")) or 1.0
        minimum = _number(bucket.get("min"))
        maximum = _number(bucket.get("max"))
        _merge_hour(
            ledger,
            key,
            _hour_key(start),
            HourBucket(
                count=int(count),
                mean=mean,
                minimum=minimum if minimum is not None else mean,
                maximum=maximum if maximum is not None else mean,
            ),
            changes,
        )


def _merge_hour(
    ledger: Ledger, key: str, hour: str, bucket: HourBucket, changes: dict[str, datetime]
) -> None:
    buckets = ledger.hours.setdefault(key, {})
    existing = buckets.get(hour)
    buckets[hour] = bucket if existing is None else existing.merged(bucket)
    _note_hour(changes, key, datetime.fromisoformat(hour))


def day_rows(ledger: Ledger, key: str, since: date, *, tz: tzinfo) -> list[dict[str, Any]]:
    """Statistics rows for a day sum, from `since` onward.

    Sums are cumulative in Home Assistant, so the running total starts at everything
    before `since` and every later row carries it forward. Days with no amount produce no
    row: Home Assistant interpolates nothing, and a missing day is more honest than a zero.
    """
    totals = ledger.day_totals(key)
    if not totals:
        return []

    running = sum(amount for iso, amount in totals.items() if date.fromisoformat(iso) < since)
    rows: list[dict[str, Any]] = []
    for iso in sorted(totals):
        day = date.fromisoformat(iso)
        if day < since:
            continue
        amount = totals[iso]
        running += amount
        rows.append(
            {
                # Local midnight, floored to the hour: the recorder requires a row to start
                # on the hour, and a half-hour time zone's midnight is not.
                "start": datetime.combine(day, time.min, tzinfo=tz).replace(
                    minute=0, second=0, microsecond=0
                ),
                "state": amount,
                "sum": running,
            }
        )
    return rows


def hour_rows(ledger: Ledger, key: str, since: datetime) -> list[dict[str, Any]]:
    """Statistics rows for a measured quantity, from `since` onward."""
    buckets = ledger.hours.get(key)
    if not buckets:
        return []

    rows: list[dict[str, Any]] = []
    for iso in sorted(buckets):
        start = datetime.fromisoformat(iso)
        if start < since:
            continue
        bucket = buckets[iso]
        rows.append(
            {
                "start": start,
                "mean": bucket.mean,
                "min": bucket.minimum,
                "max": bucket.maximum,
            }
        )
    return rows


def prune(ledger: Ledger, *, before: date, tz: tzinfo) -> None:
    """Drop everything older than `before`, so the ledger does not grow without end.

    Statistics already written stay in the recorder; this only forgets what would be
    needed to rewrite them, which is acceptable for days that will not change again.
    """
    cutoff = before.isoformat()
    for days in ledger.days.values():
        for iso in [iso for iso in days if iso < cutoff]:
            del days[iso]
    for sessions in ledger.sessions.values():
        for identity in [i for i, (day, _) in sessions.items() if day < cutoff]:
            del sessions[identity]
    boundary = datetime.combine(before, time.min, tzinfo=tz).astimezone(UTC)
    for buckets in ledger.hours.values():
        for iso in [iso for iso in buckets if datetime.fromisoformat(iso) < boundary]:
            del buckets[iso]


def earliest_day(change: HistoryChange) -> date | None:
    """The oldest day any key changed, for logging a backfill window."""
    days = list(change.day_keys.values())
    hours = [moment.date() for moment in change.hour_keys.values()]
    both = days + hours
    return min(both) if both else None


__all__ = [
    "DAILY_TOTAL_KEYS",
    "DAY_KEYS",
    "SCREEN_TIME_KEY",
    "HOUR_KEYS",
    "HistoryChange",
    "HourBucket",
    "Ledger",
    "MEASURED_SERIES",
    "SESSION_SERIES",
    "apply_payload",
    "day_rows",
    "earliest_day",
    "hour_rows",
    "prune",
]
