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
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, Final

from .payload import is_own_record, parse_instant
from .workout_details import apply_records, clean_pool, prune_pool, windows_of
from .workouts import cutoff, keep_sessions, stored_workouts

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
    MeasuredSeries("speed", "speed", "meters_per_second"),
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
    "speed": "speed",
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

# How many days before the newest one the ledger keeps. Both apps backfill 365 days at
# most, and a payload can wait about a week in the app's outbox, so everything a backfill
# or a late delivery can send again is still here to be replaced rather than added twice.
RETENTION_DAYS: Final = 400


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
    workouts: uuid -> type, start, end and length of each exercise session of yesterday
    and today, for the exercise sensors. Not the statistics' business: `exercise_minutes`
    is built from `sessions`, which keeps no type and no start.
    activity: the records a workout's details are read from (workout_details.py): those
    that overlap a kept workout, and those of the last hours that may still get one.
    kept_from: the cutoff of the last prune that dropped something ("YYYY-MM-DD"), None
    before that. Nothing older is taken in again, because its amount is already carried.
    carried: key -> the sum of the day amounts pruned, which the running sums start from.
    """

    days: dict[str, dict[str, float]] = field(default_factory=dict)
    sessions: dict[str, dict[str, list]] = field(default_factory=dict)
    hours: dict[str, dict[str, HourBucket]] = field(default_factory=dict)
    workouts: dict[str, dict[str, Any]] = field(default_factory=dict)
    activity: dict[str, Any] = field(default_factory=dict)
    kept_from: str | None = None
    carried: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "days": self.days,
            "sessions": self.sessions,
            "hours": {
                key: {hour: bucket.to_dict() for hour, bucket in buckets.items()}
                for key, buckets in self.hours.items()
            },
            "workouts": self.workouts,
        }
        # Only once there are records: a phone that sends nothing a workout reads, and a
        # test ping, leave the file as it was.
        if any(self.activity.values()):
            data["activity"] = self.activity
        # Only once something was pruned: until then the file is what it always was.
        if self.kept_from is not None:
            data["kept_from"] = self.kept_from
            data["carried"] = self.carried
        return data

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
            # Absent in a ledger written before the exercise sensors.
            workouts={u: dict(row) for u, row in (data.get("workouts") or {}).items()},
            # Absent in a ledger written before the workout details.
            activity=clean_pool(data.get("activity")),
            # Absent in a ledger that was never pruned.
            kept_from=data.get("kept_from") or None,
            carried={k: float(v) for k, v in (data.get("carried") or {}).items()},
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
    #: Whether the kept workouts or the records of their details changed. Nothing for the
    #: statistics, so it is not part of is_empty; the exercise sensors are read from them.
    workouts: bool = False

    @property
    def is_empty(self) -> bool:
        """Whether the statistics have nothing to rewrite."""
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


def apply_payload(
    ledger: Ledger, data: dict[str, Any], *, tz: tzinfo, now: datetime | None = None
) -> HistoryChange:
    """Fold one payload into the ledger, and report what it changed.

    `now` decides which workouts are recent enough to keep; it is the moment the payload
    arrives, which only a test has a reason to set.

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
    _apply_measured(ledger, data, _first_hour(ledger, tz), hour_changes)
    now = now or datetime.now(UTC)
    workouts = keep_sessions(ledger.workouts, data, now=now, tz=tz)
    records = apply_records(ledger.activity, data, now=now)
    windows = windows_of(stored_workouts(ledger.workouts))
    dropped = prune_pool(ledger.activity, windows, now=now, cutoff=cutoff(now, tz))
    workouts = workouts or records or dropped

    return HistoryChange(day_keys=day_changes, hour_keys=hour_changes, workouts=workouts)


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
        if _pruned(ledger, day):
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
        if _pruned(ledger, day):
            continue
        # The apps that are sent, when the app filtered them, as the sensors show.
        key = (
            "filtered_screen_time_minutes"
            if "filtered_screen_time_minutes" in entry
            else "total_screen_time_minutes"
        )
        minutes = _number(entry.get(key))
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
            if not isinstance(record, dict) or is_own_record(record):
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
            if _pruned(ledger, day):
                continue
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


def _pruned(ledger: Ledger, day: date) -> bool:
    """Whether a day lies before what the ledger keeps, so its amount was carried."""
    return ledger.kept_from is not None and day.isoformat() < ledger.kept_from


def _first_hour(ledger: Ledger, tz: tzinfo) -> datetime | None:
    """The first moment the ledger keeps hours for: local midnight of its first day."""
    if ledger.kept_from is None:
        return None
    return datetime.combine(date.fromisoformat(ledger.kept_from), time.min, tzinfo=tz)


def _apply_measured(
    ledger: Ledger,
    data: dict[str, Any],
    first_hour: datetime | None,
    changes: dict[str, datetime],
) -> None:
    """Point-in-time readings become hourly buckets; bucketed series merge straight in."""
    for series in MEASURED_SERIES:
        records = data.get(series.array)
        if not isinstance(records, list) or not records:
            continue

        first = records[0]
        if isinstance(first, dict) and "bucket_start" in first:
            key = BUCKETED_KEYS.get(series.array)
            if key == series.key:
                _apply_buckets(ledger, key, records, first_hour, changes)
            continue

        for record in records:
            # What the app wrote for us is already in Home Assistant; counting it
            # again would put every received weight in the hourly history twice.
            if not isinstance(record, dict) or is_own_record(record):
                continue
            try:
                moment = parse_instant(record.get(series.time_field))
            except ValueError:
                continue
            value = _number(record.get(series.value_field))
            if value is None:
                continue
            _merge_hour(
                ledger,
                series.key,
                _hour_key(moment),
                HourBucket(1, value, value, value),
                first_hour,
                changes,
            )


def _apply_buckets(
    ledger: Ledger,
    key: str,
    buckets: list[Any],
    first_hour: datetime | None,
    changes: dict[str, datetime],
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
            first_hour,
            changes,
        )


def _merge_hour(
    ledger: Ledger,
    key: str,
    hour: str,
    bucket: HourBucket,
    first_hour: datetime | None,
    changes: dict[str, datetime],
) -> None:
    start = datetime.fromisoformat(hour)
    if first_hour is not None and start < first_hour:
        # Pruned already; merging it into a fresh bucket would overwrite the hour's row.
        return
    buckets = ledger.hours.setdefault(key, {})
    existing = buckets.get(hour)
    buckets[hour] = bucket if existing is None else existing.merged(bucket)
    _note_hour(changes, key, start)


def day_rows(ledger: Ledger, key: str, since: date, *, tz: tzinfo) -> list[dict[str, Any]]:
    """Statistics rows for a day sum, from `since` onward.

    Sums are cumulative in Home Assistant, so the running total starts at everything
    before `since`, the pruned days included, and every later row carries it forward. Days
    with no amount produce no row: Home Assistant interpolates nothing, and a missing day is
    more honest than a zero.
    """
    totals = ledger.day_totals(key)
    if not totals:
        return []

    running = ledger.carried.get(key, 0.0) + sum(
        amount for iso, amount in totals.items() if date.fromisoformat(iso) < since
    )
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


def keep_from(ledger: Ledger, *, today: date, tz: tzinfo) -> date | None:
    """The first day the ledger should keep: RETENTION_DAYS before its newest day.

    Counted from the newest day it holds rather than from the clock alone, so a phone
    that was away for a while keeps what it had; never from a day after `today`, so a
    reading with a wrong date in the future cannot push everything else out.
    """
    newest = [max(days) for days in ledger.days.values() if days]
    newest += [
        max(day for day, _ in records.values()) for records in ledger.sessions.values() if records
    ]
    newest += [
        datetime.fromisoformat(max(buckets)).astimezone(tz).date().isoformat()
        for buckets in ledger.hours.values()
        if buckets
    ]
    if not newest:
        return None
    return min(date.fromisoformat(max(newest)), today) - timedelta(days=RETENTION_DAYS)


def prune(ledger: Ledger, *, before: date, tz: tzinfo) -> bool:
    """Drop everything older than `before`, so the ledger does not grow without end.

    Statistics already written stay in the recorder; this only forgets what would be
    needed to rewrite them, which is acceptable for days that will not change again. The
    amounts of the pruned days are carried, so a day rewritten later still continues the
    sum it had, and nothing before `before` is taken in again: a repeat of a pruned day
    would otherwise count once in the carried sum and once more as a day of its own.

    The kept workouts are left alone; they hold yesterday and today only. Returns whether
    anything was dropped, so the ledger is written only then.
    """
    cutoff = before.isoformat()
    if ledger.kept_from is not None and cutoff <= ledger.kept_from:
        # Nothing older than kept_from is taken in, so there is nothing to drop.
        return False

    dropped = False
    for key, days in ledger.days.items():
        for iso in [iso for iso in days if iso < cutoff]:
            ledger.carried[key] = ledger.carried.get(key, 0.0) + days.pop(iso)
            dropped = True
    for key, sessions in ledger.sessions.items():
        for identity in [i for i, (day, _) in sessions.items() if day < cutoff]:
            _, amount = sessions.pop(identity)
            ledger.carried[key] = ledger.carried.get(key, 0.0) + amount
            dropped = True
    boundary = datetime.combine(before, time.min, tzinfo=tz).astimezone(UTC)
    for buckets in ledger.hours.values():
        for iso in [iso for iso in buckets if datetime.fromisoformat(iso) < boundary]:
            del buckets[iso]
            dropped = True
    # Moved only when something went: a day between the old and the new cutoff was then
    # never kept, so taking it in later counts it once.
    if dropped:
        ledger.kept_from = cutoff
    return dropped


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
    "RETENTION_DAYS",
    "SESSION_SERIES",
    "apply_payload",
    "day_rows",
    "earliest_day",
    "hour_rows",
    "keep_from",
    "prune",
]
