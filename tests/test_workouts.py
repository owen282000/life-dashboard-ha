"""Test the workouts: which kind a record is, what is kept, and what the day sums say.

No Home Assistant here. The data is a day of an Amsterdam phone that notices its own
walks: a few of 10 to 15 minutes, a run, a long ride, and one session across midnight.
"""

import json
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.life_dashboard.history import Ledger, apply_payload
from custom_components.life_dashboard.payload import (
    APP_PACKAGE,
    KEY_EXERCISE_TODAY,
    KEY_EXERCISE_YESTERDAY,
    KEY_LAST_WORKOUT,
    SENSOR_SPECS,
)
from custom_components.life_dashboard.workout_details import PENDING_SECONDS
from custom_components.life_dashboard.workout_types import (
    HEALTH_CONNECT_TYPES,
    WORKOUT_TYPES,
    type_code,
    workout_type,
)
from custom_components.life_dashboard.workouts import (
    MAX_WAITING,
    day_sessions,
    day_updates,
    keep_sessions,
    kept_workouts,
    last_workout_update,
    parse_workout,
    prune_past,
    stored_workouts,
)

# 4 October 2026 is a Sunday; Amsterdam is on +02:00 until the 25th.
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def _amsterdam() -> ZoneInfo:
    return ZoneInfo("Europe/Amsterdam")


def _record(
    type_: object = "56",
    start: str = "2026-10-04T07:00:00Z",
    end: str = "2026-10-04T08:00:00Z",
    seconds: object = 3600,
    **extra,
) -> dict:
    """An exercise record as the app sends it, per docs/webhook.md."""
    record = {"type": type_, "start_time": start, "end_time": end, "duration_seconds": seconds}
    record.update(extra)
    return record


def _payload(*records: dict, **extra) -> dict:
    payload = {
        "timestamp": "2026-10-04T12:00:00Z",
        "app_version": "1.23.0",
        "source": "health_connect",
        "exercise": list(records),
    }
    payload.update(extra)
    return payload


def _keep(sessions: dict, *records: dict, now: datetime = NOW) -> bool:
    return keep_sessions(sessions, _payload(*records), now=now, tz=_amsterdam())


def _day(sessions: dict, day: date, **kwargs) -> list[dict]:
    return day_sessions(stored_workouts(sessions), day, tz=_amsterdam(), **kwargs)


# --- The kinds ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("56", "running"),
        ("79", "walking"),
        ("8", "biking"),
        ("9", "biking_stationary"),
        ("70", "strength_training"),
        ("81", "weightlifting"),
        ("36", "hiit"),
        ("83", "yoga"),
        ("48", "pilates"),
        ("74", "swimming_pool"),
        ("73", "swimming_open_water"),
        ("37", "hiking"),
        ("25", "elliptical"),
        ("54", "rowing_machine"),
        ("16", "dancing"),
        ("68", "stair_climbing"),
        ("13", "calisthenics"),
        ("11", "boxing"),
        ("44", "martial_arts"),
        ("64", "soccer"),
        ("0", "other"),
    ],
)
def test_health_connect_codes(raw: str, kind: str) -> None:
    """The numbers are ExerciseSessionRecord.EXERCISE_TYPE_*."""
    assert workout_type(raw) == kind


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("running", "running"),
        ("walking", "walking"),
        ("cycling", "biking"),
        ("hiit", "hiit"),
        ("traditional_strength_training", "strength_training"),
        ("functional_strength_training", "strength_training"),
        ("downhill_skiing", "skiing"),
        ("Yoga", "yoga"),
        (" pilates ", "pilates"),
        ("swimming", "swimming"),
        # HealthKit has these and Health Connect has no counterpart.
        ("archery", "other"),
        ("other", "other"),
    ],
)
def test_ios_names(raw: str, kind: str) -> None:
    """The iOS app sends HealthKit's activity as a snake_case name."""
    assert workout_type(raw) == kind


@pytest.mark.parametrize(
    "raw", ["9999", "-1", "1", "hovercraft", "", "  ", None, True, 3.5, [], {}]
)
def test_an_unknown_type_is_other(raw: object) -> None:
    assert workout_type(raw) == "other"


def test_a_number_is_taken_as_its_digits() -> None:
    """The documented form is a string; a number is the same constant."""
    assert workout_type(56) == "running"
    assert type_code(56) == "56"
    assert type_code(True) == ""
    assert type_code(None) == ""


def test_every_key_is_one_of_the_options() -> None:
    """Home Assistant refuses an enum state that is not an option."""
    assert set(HEALTH_CONNECT_TYPES.values()) <= set(WORKOUT_TYPES)
    assert len(WORKOUT_TYPES) == len(set(WORKOUT_TYPES))
    assert "other" in WORKOUT_TYPES
    assert SENSOR_SPECS[KEY_LAST_WORKOUT].options == WORKOUT_TYPES
    assert SENSOR_SPECS[KEY_LAST_WORKOUT].device_class == "enum"
    assert SENSOR_SPECS[KEY_LAST_WORKOUT].unit is None


def test_the_constants_match_androidx() -> None:
    """The numbers are not guessed: these are the ones in ExerciseSessionRecord.kt."""
    expected = {
        "0": "other",
        "8": "biking",
        "9": "biking_stationary",
        "56": "running",
        "57": "running_treadmill",
        "70": "strength_training",
        "74": "swimming_pool",
        "79": "walking",
        "81": "weightlifting",
        "83": "yoga",
    }
    for code, kind in expected.items():
        assert HEALTH_CONNECT_TYPES[code] == kind
    # 61 types, the ones in androidx.health.connect 1.1: no number is used twice.
    assert len(HEALTH_CONNECT_TYPES) == 61
    assert len(set(HEALTH_CONNECT_TYPES.values())) == 61


# --- Reading a record --------------------------------------------------------


def test_a_record_as_documented() -> None:
    workout = parse_workout(_record(uuid="u-1", source="com.google.android.apps.fitness"))
    assert workout is not None
    assert workout.kind == "running"
    assert workout.code == "56"
    assert workout.start == datetime(2026, 10, 4, 7, 0, tzinfo=UTC)
    assert workout.end == datetime(2026, 10, 4, 8, 0, tzinfo=UTC)
    assert workout.minutes == 60
    assert workout.uuid == "u-1"
    assert workout.source == "com.google.android.apps.fitness"


def test_the_start_is_optional() -> None:
    """The contract does not promise a start: it is the end minus the length."""
    record = _record()
    del record["start_time"]
    workout = parse_workout(record)
    assert workout is not None
    assert workout.start == datetime(2026, 10, 4, 7, 0, tzinfo=UTC)


def test_a_start_after_the_end_is_not_believed() -> None:
    workout = parse_workout(_record(start="2026-10-04T09:00:00Z"))
    assert workout is not None
    assert workout.start == datetime(2026, 10, 4, 7, 0, tzinfo=UTC)


def test_the_length_is_the_span_when_it_is_missing() -> None:
    record = _record()
    del record["duration_seconds"]
    workout = parse_workout(record)
    assert workout is not None
    assert workout.seconds == 3600


def test_fractions_of_a_second_are_read() -> None:
    """Java sends up to nine digits."""
    workout = parse_workout(_record(end="2026-10-04T08:00:00.123456789Z"))
    assert workout is not None
    assert workout.end.microsecond == 123456


@pytest.mark.parametrize(
    "record",
    [
        "running",
        None,
        [],
        {},
        _record(end="yesterday"),
        _record(end=None),
        _record(end="2026-10-04T08:00:00"),
        _record(seconds=0),
        _record(seconds=-60),
        _record(seconds="3600"),
        _record(seconds=float("nan")),
        _record(seconds=float("inf")),
        _record(seconds=True),
        _record(source=APP_PACKAGE),
    ],
)
def test_a_record_that_cannot_be_read_is_skipped(record: object) -> None:
    """Never raises: one bad record must not cost a sync, and the app's own are not ours."""
    assert parse_workout(record) is None


def test_a_type_that_is_missing_is_other() -> None:
    record = _record()
    del record["type"]
    workout = parse_workout(record)
    assert workout is not None
    assert workout.kind == "other"
    assert workout.code == ""


# --- What is kept ------------------------------------------------------------


def test_sessions_are_kept_by_uuid() -> None:
    sessions: dict = {}
    assert _keep(sessions, _record(uuid="a"), _record(uuid="b", type_="79"))
    assert set(sessions) == {"a", "b"}
    assert sessions["a"] == {
        "type": "56",
        "start": "2026-10-04T07:00:00+00:00",
        "end": "2026-10-04T08:00:00+00:00",
        "seconds": 3600.0,
    }


def test_a_record_that_arrives_again_changes_nothing() -> None:
    """The outbox, a retried POST and a backfill all send the same record again."""
    sessions: dict = {}
    _keep(sessions, _record(uuid="a"))
    assert not _keep(sessions, _record(uuid="a"))
    assert len(sessions) == 1


def test_an_edited_record_replaces_itself() -> None:
    """Same uuid, new end and length, possibly another type."""
    sessions: dict = {}
    _keep(sessions, _record(uuid="a", end="2026-10-04T07:30:00Z", seconds=1800))
    assert _keep(sessions, _record(uuid="a", type_="8"))
    assert len(sessions) == 1
    [workout] = stored_workouts(sessions)
    assert workout.kind == "biking"
    assert workout.minutes == 60


def test_a_record_without_a_uuid_is_not_counted_twice() -> None:
    sessions: dict = {}
    _keep(sessions, _record())
    assert not _keep(sessions, _record())
    assert len(sessions) == 1
    # Another session at the same time of the same length is another one only with a uuid.
    _keep(sessions, _record(type_="79"))
    assert len(sessions) == 2


def test_the_app_s_own_records_are_not_kept() -> None:
    sessions: dict = {}
    assert not _keep(sessions, _record(uuid="a", source=APP_PACKAGE))
    assert sessions == {}


def test_a_payload_without_exercise_changes_nothing() -> None:
    sessions: dict = {}
    _keep(sessions, _record(uuid="a"))
    assert not keep_sessions(sessions, {"source": "health_connect"}, now=NOW, tz=_amsterdam())
    assert not keep_sessions(sessions, {"exercise": "no"}, now=NOW, tz=_amsterdam())
    assert len(sessions) == 1


def test_older_days_are_pruned() -> None:
    """Today and yesterday are kept: what ended before yesterday's midnight is not."""
    sessions: dict = {}
    _keep(
        sessions,
        # Two days ago, an hour before midnight: gone.
        _record(uuid="old", start="2026-10-02T20:00:00Z", end="2026-10-02T21:00:00Z"),
        # Yesterday in the early hours (00:30 local): kept.
        _record(uuid="yesterday", start="2026-10-02T22:00:00Z", end="2026-10-02T22:30:00Z"),
        _record(uuid="today", start="2026-10-04T07:00:00Z", end="2026-10-04T08:00:00Z"),
    )
    assert set(sessions) == {"yesterday", "today"}


def test_a_session_is_pruned_when_the_days_move_on() -> None:
    sessions: dict = {}
    _keep(sessions, _record(uuid="today"))
    later = NOW + timedelta(days=2)
    assert keep_sessions(sessions, {}, now=later, tz=_amsterdam())
    assert sessions == {}


def test_a_backfill_does_not_fill_the_store_with_the_past() -> None:
    sessions: dict = {}
    _keep(
        sessions,
        *(
            _record(
                uuid=f"u{day}",
                start=f"2026-09-{day:02d}T07:00:00Z",
                end=f"2026-09-{day:02d}T08:00:00Z",
            )
            for day in range(1, 28)
        ),
    )
    assert sessions == {}


def test_a_row_that_cannot_be_read_is_dropped_not_fatal() -> None:
    sessions = {"bad": {"type": "56"}, "worse": "x"}
    _keep(sessions, _record(uuid="a"))
    assert set(sessions) == {"a"}
    assert [w.kind for w in stored_workouts({"bad": {"type": "56"}, "a": sessions["a"]})] == [
        "running"
    ]


# --- The day sums ------------------------------------------------------------


def test_a_day_of_short_and_long_sessions() -> None:
    """Auto-detected walks of 10 to 15 minutes, a run and a long ride on one day."""
    sessions: dict = {}
    _keep(
        sessions,
        _record("79", "2026-10-04T05:10:00Z", "2026-10-04T05:22:00Z", 720, uuid="walk-1"),
        _record("79", "2026-10-04T06:00:00Z", "2026-10-04T06:15:00Z", 900, uuid="walk-2"),
        _record("56", "2026-10-04T07:00:00Z", "2026-10-04T08:00:00Z", 3600, uuid="run"),
        _record("8", "2026-10-04T09:00:00Z", "2026-10-04T10:50:00Z", 6600, uuid="ride"),
    )
    rows = _day(sessions, date(2026, 10, 4))
    assert [(row["type"], row["minutes"]) for row in rows] == [
        ("walking", 12),
        ("walking", 15),
        ("running", 60),
        ("biking", 110),
    ]
    assert rows[0] == {
        "type": "walking",
        "start": "2026-10-04T07:10:00+02:00",
        "end": "2026-10-04T07:22:00+02:00",
        "minutes": 12,
    }
    [today, yesterday] = day_updates(stored_workouts(sessions), now=NOW, tz=_amsterdam())
    assert (today.key, today.value) == (KEY_EXERCISE_TODAY, 197)
    assert today.attributes["date"] == "2026-10-04"
    assert [row["type"] for row in today.attributes["sessions"]] == [
        "walking",
        "walking",
        "running",
        "biking",
    ]
    assert (yesterday.key, yesterday.value) == (KEY_EXERCISE_YESTERDAY, 0)
    assert yesterday.attributes == {"date": "2026-10-03", "sessions": []}


def test_the_state_rounds_the_day_once_and_the_list_each_session() -> None:
    """Two sessions of 10.5 minutes: the list shows each rounded, the day is 21 minutes."""
    sessions: dict = {}
    _keep(
        sessions,
        _record("79", "2026-10-04T05:00:00Z", "2026-10-04T05:10:30Z", 630, uuid="a"),
        _record("79", "2026-10-04T06:00:00Z", "2026-10-04T06:10:30Z", 630, uuid="b"),
    )
    [today, _] = day_updates(stored_workouts(sessions), now=NOW, tz=_amsterdam())
    assert [row["minutes"] for row in today.attributes["sessions"]] == [10, 10]
    assert today.value == 21


def test_short_sessions_add_up_before_rounding() -> None:
    """Ten sessions of 40 seconds are 7 minutes, not ten rounded-up minutes."""
    sessions: dict = {}
    _keep(
        sessions,
        *(
            _record(
                "79", f"2026-10-04T05:{m:02d}:00Z", f"2026-10-04T05:{m:02d}:40Z", 40, uuid=f"s{m}"
            )
            for m in range(0, 50, 5)
        ),
    )
    [today, _] = day_updates(stored_workouts(sessions), now=NOW, tz=_amsterdam())
    assert today.value == 7


def test_one_run_written_by_two_apps_counts_once() -> None:
    """The phone and the watch both write the same hour: the day has one hour."""
    sessions: dict = {}
    _keep(
        sessions,
        _record(
            "56",
            "2026-10-04T07:00:00Z",
            "2026-10-04T08:00:00Z",
            3600,
            uuid="phone",
            source="com.sec.android.app.shealth",
        ),
        _record(
            "56",
            "2026-10-04T07:00:00Z",
            "2026-10-04T08:00:00Z",
            3600,
            uuid="watch",
            source="com.strava",
        ),
    )
    [today, _] = day_updates(stored_workouts(sessions), now=NOW, tz=_amsterdam())
    assert len(today.attributes["sessions"]) == 2
    assert today.value == 60


def test_overlapping_sessions_count_their_union() -> None:
    """07:00 to 08:00 and 07:30 to 08:30 cover 90 minutes, not 120."""
    sessions: dict = {}
    _keep(
        sessions,
        _record("56", "2026-10-04T07:00:00Z", "2026-10-04T08:00:00Z", 3600, uuid="a"),
        _record("79", "2026-10-04T07:30:00Z", "2026-10-04T08:30:00Z", 3600, uuid="b"),
    )
    [today, _] = day_updates(stored_workouts(sessions), now=NOW, tz=_amsterdam())
    assert today.value == 90


def _deleted(*uuids: str, type_: str = "exercise") -> list[dict]:
    return [{"type": type_, "uuid": uuid} for uuid in uuids]


def test_a_deleted_workout_no_longer_counts() -> None:
    sessions: dict = {}
    _keep(sessions, _record(uuid="run"))
    payload = {"deleted_records": _deleted("run")}
    assert keep_sessions(sessions, payload, now=NOW, tz=_amsterdam()) is True
    assert sessions == {}
    [today, _] = day_updates(stored_workouts(sessions), now=NOW, tz=_amsterdam())
    assert today.value == 0


def test_an_ios_edit_replaces_the_session() -> None:
    """HealthKit edits by deleting the old uuid and writing a new one: 60 to 45 minutes is 45."""
    sessions: dict = {}
    _keep(sessions, _record(uuid="old"))
    payload = _payload(
        _record("56", "2026-10-04T07:00:00Z", "2026-10-04T07:45:00Z", 2700, uuid="new")
    )
    payload["deleted_records"] = _deleted("old")
    keep_sessions(sessions, payload, now=NOW, tz=_amsterdam())
    assert set(sessions) == {"new"}
    [today, _] = day_updates(stored_workouts(sessions), now=NOW, tz=_amsterdam())
    assert today.value == 45


def test_a_deleted_uuid_that_comes_back_is_kept() -> None:
    """Deletions apply before the records, so a uuid written again stays (Fitbit does this)."""
    sessions: dict = {}
    _keep(sessions, _record(uuid="same"))
    payload = _payload(_record(uuid="same"))
    payload["deleted_records"] = _deleted("same")
    keep_sessions(sessions, payload, now=NOW, tz=_amsterdam())
    assert set(sessions) == {"same"}


def test_deletions_of_other_types_leave_workouts_alone() -> None:
    sessions: dict = {}
    _keep(sessions, _record(uuid="run"))
    payload = {"deleted_records": _deleted("run", type_="steps") + [{"type": "exercise"}, "junk"]}
    assert keep_sessions(sessions, payload, now=NOW, tz=_amsterdam()) is False
    assert set(sessions) == {"run"}


def test_an_edit_that_moves_a_session_before_yesterday_drops_it() -> None:
    sessions: dict = {}
    _keep(sessions, _record(uuid="moved"))
    moved = _record("56", "2026-10-01T07:00:00Z", "2026-10-01T08:00:00Z", 3600, uuid="moved")
    assert _keep(sessions, moved) is True
    assert sessions == {}


def test_a_session_across_midnight_is_split() -> None:
    """23:30 to 00:30 in Amsterdam: half an hour on each day, listed on both."""
    sessions: dict = {}
    _keep(
        sessions,
        _record("56", "2026-10-03T21:30:00Z", "2026-10-03T22:30:00Z", 3600, uuid="late"),
        now=datetime(2026, 10, 4, 6, 0, tzinfo=UTC),
    )
    saturday = _day(sessions, date(2026, 10, 3))
    sunday = _day(sessions, date(2026, 10, 4))
    assert [row["minutes"] for row in saturday] == [30]
    assert [row["minutes"] for row in sunday] == [30]
    # Each entry still says when the session really was.
    assert saturday[0]["start"] == sunday[0]["start"] == "2026-10-03T23:30:00+02:00"
    assert saturday[0]["end"] == sunday[0]["end"] == "2026-10-04T00:30:00+02:00"


def test_the_day_is_the_home_assistant_day_not_the_utc_one() -> None:
    """00:30 to 01:00 in Amsterdam is 22:30 to 23:00 UTC of the day before."""
    sessions: dict = {}
    _keep(sessions, _record("79", "2026-10-03T22:30:00Z", "2026-10-03T23:00:00Z", 1800, uuid="a"))
    workouts = stored_workouts(sessions)
    assert [r["minutes"] for r in day_sessions(workouts, date(2026, 10, 4), tz=_amsterdam())] == [
        30
    ]
    assert day_sessions(workouts, date(2026, 10, 3), tz=_amsterdam()) == []
    # For a Home Assistant set to UTC it is the other way round.
    assert [r["minutes"] for r in day_sessions(workouts, date(2026, 10, 3), tz=UTC)] == [30]
    assert day_sessions(workouts, date(2026, 10, 4), tz=UTC) == []


def test_the_day_the_clocks_go_back_has_25_hours() -> None:
    """Midnight to midnight is not 24 hours twice a year."""
    sessions: dict = {}
    # 23:30 CET on the 25th to 00:30 on the 26th; the offset changed at 03:00 that day.
    _keep(
        sessions,
        _record("56", "2026-10-25T22:30:00Z", "2026-10-25T23:30:00Z", 3600, uuid="a"),
        now=datetime(2026, 10, 26, 6, 0, tzinfo=UTC),
    )
    assert [r["minutes"] for r in _day(sessions, date(2026, 10, 25))] == [30]
    assert [r["minutes"] for r in _day(sessions, date(2026, 10, 26))] == [30]


def test_yesterday_rolls_over_at_midnight() -> None:
    """Today's sessions are yesterday's after midnight, with no new sync."""
    sessions: dict = {}
    _keep(sessions, _record(uuid="run"))
    workouts = stored_workouts(sessions)
    evening = datetime(2026, 10, 4, 21, 0, tzinfo=UTC)
    [today, yesterday] = day_updates(workouts, now=evening, tz=_amsterdam())
    assert (today.value, yesterday.value) == (60, 0)

    # 00:00:05 local on the 5th is 22:00:05 UTC on the 4th.
    after_midnight = datetime(2026, 10, 4, 22, 0, 5, tzinfo=UTC)
    [today, yesterday] = day_updates(workouts, now=after_midnight, tz=_amsterdam())
    assert (today.value, yesterday.value) == (0, 60)
    assert today.attributes["date"] == "2026-10-05"
    assert yesterday.attributes["date"] == "2026-10-04"
    assert yesterday.attributes["sessions"][0]["type"] == "running"
    # What the ordering rule goes by: each day's own midnight.
    assert today.measured_at == datetime(2026, 10, 5, tzinfo=_amsterdam())
    assert yesterday.measured_at == datetime(2026, 10, 4, tzinfo=_amsterdam())


def test_a_session_of_two_days_ago_counts_for_neither() -> None:
    workouts = [parse_workout(_record(end="2026-10-02T08:00:00Z", start="2026-10-02T07:00:00Z"))]
    [today, yesterday] = day_updates(workouts, now=NOW, tz=_amsterdam())
    assert (today.value, yesterday.value) == (0, 0)


# --- The shortest workout ----------------------------------------------------


def _short_and_long() -> dict:
    sessions: dict = {}
    _keep(
        sessions,
        _record("79", "2026-10-04T05:00:00Z", "2026-10-04T05:10:00Z", 600, uuid="walk"),
        _record("79", "2026-10-04T06:00:00Z", "2026-10-04T06:15:00Z", 900, uuid="longer-walk"),
        _record("56", "2026-10-04T07:00:00Z", "2026-10-04T08:00:00Z", 3600, uuid="run"),
    )
    return sessions


@pytest.mark.parametrize(
    ("minimum", "kept"),
    [(0, 3), (10, 3), (11, 2), (15, 2), (16, 1), (60, 1), (61, 0)],
)
def test_the_shortest_workout(minimum: int, kept: int) -> None:
    """A session as long as the minimum counts; a shorter one does not."""
    rows = _day(_short_and_long(), date(2026, 10, 4), min_minutes=minimum)
    assert len(rows) == kept


def test_the_minimum_changes_what_is_read_and_loses_nothing() -> None:
    sessions = _short_and_long()
    workouts = stored_workouts(sessions)
    [today, _] = day_updates(workouts, now=NOW, tz=_amsterdam(), min_minutes=30)
    assert today.value == 60
    [today, _] = day_updates(workouts, now=NOW, tz=_amsterdam(), min_minutes=0)
    assert today.value == 85


def test_the_minimum_is_judged_on_the_whole_session() -> None:
    """A 40 minute session across midnight is 20 and 20 on its days; it is not 20 short."""
    sessions: dict = {}
    _keep(
        sessions,
        _record("56", "2026-10-03T21:40:00Z", "2026-10-03T22:20:00Z", 2400, uuid="late"),
        now=datetime(2026, 10, 4, 6, 0, tzinfo=UTC),
    )
    assert [r["minutes"] for r in _day(sessions, date(2026, 10, 3), min_minutes=30)] == [20]
    assert [r["minutes"] for r in _day(sessions, date(2026, 10, 4), min_minutes=30)] == [20]
    assert _day(sessions, date(2026, 10, 4), min_minutes=41) == []


# --- The past workouts -------------------------------------------------------


def _fold(sessions: dict, past: dict, *records: dict, now: datetime = NOW, **extra) -> bool:
    """As the ledger folds a payload: with the past workouts next to the sessions."""
    payload = _payload(*records, **extra)
    return keep_sessions(sessions, payload, now=now, tz=_amsterdam(), past=past)


def _old(uuid: str | None = "old", day: int = 20, **extra) -> dict:
    """A run in September, an hour from 09:00 in Amsterdam, as a backfill brings it."""
    record = _record(
        start=f"2026-09-{day:02d}T07:00:00Z", end=f"2026-09-{day:02d}T08:00:00Z", **extra
    )
    if uuid is not None:
        record["uuid"] = uuid
    return record


OLD_ROW = {
    "type": "56",
    "start": "2026-09-20T07:00:00+00:00",
    "end": "2026-09-20T08:00:00+00:00",
    "seconds": 3600.0,
}
LATER = NOW + timedelta(seconds=PENDING_SECONDS)


def test_a_session_that_leaves_the_two_days_stays_as_a_past_workout() -> None:
    sessions: dict = {}
    past: dict = {}
    _fold(sessions, past, _record(uuid="run"))
    assert past == {}
    row = dict(sessions["run"])

    assert _fold(sessions, past, now=NOW + timedelta(days=2))
    assert sessions == {}
    # The session as it was, and its details: none, with no records to read them from.
    assert past == {"run": {**row, "details": {}}}


def test_a_session_that_arrives_older_waits_for_its_records_and_then_stays() -> None:
    """A backfill brings a run of two weeks ago: its records may still be on their way."""
    sessions: dict = {}
    past: dict = {}
    assert _fold(sessions, past, _old())
    assert sessions == {}
    assert past == {"old": {**OLD_ROW, "received": int(NOW.timestamp())}}

    assert not _fold(sessions, past, now=LATER - timedelta(seconds=1))
    assert "received" in past["old"]
    assert _fold(sessions, past, now=LATER)
    assert past == {"old": {**OLD_ROW, "details": {}}}
    assert not _fold(sessions, past, now=LATER + timedelta(days=30))


def test_a_backfill_fills_the_past_workouts_and_not_the_two_days() -> None:
    sessions: dict = {}
    past: dict = {}
    _fold(sessions, past, *(_old(f"u{day}", day) for day in range(1, 28)), backfill=True)
    assert sessions == {}
    assert len(past) == 27
    assert len(kept_workouts(sessions, past, tz=_amsterdam())) == 27


def test_a_past_workout_that_arrives_again_changes_nothing() -> None:
    """A backfill sent a second time, while it waits and after."""
    sessions: dict = {}
    past: dict = {}
    _fold(sessions, past, _old())
    assert not _fold(sessions, past, _old(), now=NOW + timedelta(hours=1))
    assert past["old"]["received"] == int(NOW.timestamp())

    _fold(sessions, past, now=LATER)
    finished = dict(past["old"])
    assert not _fold(sessions, past, _old(), now=LATER + timedelta(days=1))
    assert past == {"old": finished}


def test_an_edited_past_workout_replaces_itself_and_waits_again() -> None:
    """Same uuid, another kind and half the length: one row, with its hours from now."""
    sessions: dict = {}
    past: dict = {}
    _fold(sessions, past, _old())
    _fold(sessions, past, now=LATER)
    edited = _old(type_="79", seconds=1800)
    assert _fold(sessions, past, edited, now=LATER + timedelta(days=1))
    assert past == {
        "old": {
            **OLD_ROW,
            "type": "79",
            "seconds": 1800.0,
            "received": int((LATER + timedelta(days=1)).timestamp()),
        }
    }


def test_a_past_workout_without_a_uuid_is_not_kept_twice() -> None:
    sessions: dict = {}
    past: dict = {}
    _fold(sessions, past, _old(uuid=None))
    assert not _fold(sessions, past, _old(uuid=None))
    assert list(past) == ["2026-09-20T08:00:00+00:00:3600.0:56"]


def test_a_deleted_past_workout_goes() -> None:
    """One that still waits and one that is finished, both by their uuid."""
    sessions: dict = {}
    past: dict = {}
    _fold(sessions, past, _old("first", 20))
    _fold(sessions, past, _old("second", 21), now=LATER)
    assert "received" not in past["first"] and "received" in past["second"]

    payload = {"deleted_records": _deleted("first", "second") + _deleted("first", type_="steps")}
    assert keep_sessions(sessions, payload, now=LATER, tz=_amsterdam(), past=past) is True
    assert past == {}
    assert keep_sessions(sessions, payload, now=LATER, tz=_amsterdam(), past=past) is False


def test_a_past_workout_deleted_and_written_again_as_it_was_stays_as_it_is() -> None:
    """An older app names a record it wrote again as deleted too (Fitbit does this). The
    records of its details are long gone, so the row must not start again without them."""
    sessions: dict = {}
    past: dict = {}
    _fold(sessions, past, _old())
    _fold(sessions, past, now=LATER)
    past["old"]["details"] = {"steps": 4500}
    finished = dict(past["old"])

    _fold(sessions, past, _old(), now=LATER + timedelta(days=1), deleted_records=_deleted("old"))
    assert past == {"old": finished}
    # Written again as something else, it is an edit like any other.
    _fold(sessions, past, _old(seconds=1800), now=LATER, deleted_records=_deleted("old"))
    assert "details" not in past["old"] and past["old"]["seconds"] == 1800.0


def test_an_ios_edit_of_a_past_workout_leaves_one_row() -> None:
    """HealthKit deletes the old uuid and writes a new one, here for a run of two weeks ago."""
    sessions: dict = {}
    past: dict = {}
    _fold(sessions, past, _old("before"))
    _fold(sessions, past, now=LATER)

    shorter = _record("56", "2026-09-20T07:00:00Z", "2026-09-20T07:45:00Z", 2700, uuid="after")
    _fold(sessions, past, shorter, now=LATER, deleted_records=_deleted("before"))
    assert set(past) == {"after"}
    [row] = kept_workouts(sessions, past, tz=_amsterdam())
    assert row["minutes"] == 45


def test_an_edit_that_moves_a_session_before_yesterday_makes_it_a_past_workout() -> None:
    sessions: dict = {}
    past: dict = {}
    _fold(sessions, past, _record(uuid="moved"))
    moved = _record("56", "2026-10-01T07:00:00Z", "2026-10-01T08:00:00Z", 3600, uuid="moved")
    assert _fold(sessions, past, moved) is True
    assert sessions == {}
    assert past["moved"]["end"] == "2026-10-01T08:00:00+00:00"


def test_an_edit_that_moves_a_past_workout_into_the_two_days_makes_it_a_session() -> None:
    sessions: dict = {}
    past: dict = {}
    _fold(sessions, past, _old("moved"))
    assert _fold(sessions, past, _record(uuid="moved")) is True
    assert past == {}
    assert set(sessions) == {"moved"}
    assert len(kept_workouts(sessions, past, tz=_amsterdam())) == 1


def test_only_so_many_wait_at_once() -> None:
    """A year of backfill in a few minutes: the ones that waited longest are finished."""
    sessions: dict = {}
    past: dict = {}
    for day in range(1, 4):
        _fold(sessions, past, _old(f"first-{day}", day), now=NOW + timedelta(minutes=day))
    _fold(
        sessions,
        past,
        *(_old(f"u{n}", 5 + n // 10, seconds=600 + n) for n in range(MAX_WAITING)),
        now=NOW + timedelta(minutes=10),
    )
    waiting = {key for key, row in past.items() if "received" in row}
    assert len(waiting) == MAX_WAITING
    assert set(past) - waiting == {"first-1", "first-2", "first-3"}


def test_the_kept_workouts_as_data_have_the_format_of_the_sessions() -> None:
    sessions: dict = {}
    past: dict = {}
    _fold(
        sessions,
        past,
        _record(uuid="run"),
        _record("8", "2026-10-03T09:00:00Z", "2026-10-03T10:30:00Z", 5400, uuid="ride"),
        _old("walk", type_="79"),
    )
    workouts = kept_workouts(sessions, past, tz=_amsterdam())
    assert workouts == [
        {
            "type": "walking",
            "start": "2026-09-20T09:00:00+02:00",
            "end": "2026-09-20T10:00:00+02:00",
            "minutes": 60,
        },
        {
            "type": "biking",
            "start": "2026-10-03T11:00:00+02:00",
            "end": "2026-10-03T12:30:00+02:00",
            "minutes": 90,
        },
        {
            "type": "running",
            "start": "2026-10-04T09:00:00+02:00",
            "end": "2026-10-04T10:00:00+02:00",
            "minutes": 60,
        },
    ]
    # Those of the two days are word for word what the sensors list.
    assert workouts[1:] == _day(sessions, date(2026, 10, 3)) + _day(sessions, date(2026, 10, 4))
    # Plain data: it goes out as the response of an action.
    assert json.loads(json.dumps(workouts)) == workouts


def test_the_kept_workouts_as_data_in_a_window_of_the_days_they_ended() -> None:
    sessions: dict = {}
    past: dict = {}
    _fold(sessions, past, *(_old(f"u{day}", day) for day in (18, 19, 20, 21)), _record(uuid="run"))

    def ended(**window) -> list[str]:
        rows = kept_workouts(sessions, past, tz=_amsterdam(), **window)
        return [row["end"][:10] for row in rows]

    assert ended() == ["2026-09-18", "2026-09-19", "2026-09-20", "2026-09-21", "2026-10-04"]
    assert ended(start=date(2026, 9, 20)) == ["2026-09-20", "2026-09-21", "2026-10-04"]
    assert ended(end=date(2026, 9, 19)) == ["2026-09-18", "2026-09-19"]
    assert ended(start=date(2026, 9, 19), end=date(2026, 9, 20)) == ["2026-09-19", "2026-09-20"]
    assert ended(start=date(2026, 10, 5)) == []


def test_a_kept_workout_across_midnight_is_data_once_and_whole() -> None:
    """23:30 to 00:30 in Amsterdam: two entries of 30 minutes on the sensors, one of 60 here,
    on the day it ended."""
    sessions: dict = {}
    past: dict = {}
    late = _record("56", "2026-10-03T21:30:00Z", "2026-10-03T22:30:00Z", 3600, uuid="late")
    _fold(sessions, past, late, now=datetime(2026, 10, 4, 6, 0, tzinfo=UTC))
    [row] = kept_workouts(sessions, past, tz=_amsterdam())
    assert row["minutes"] == 60
    assert kept_workouts(sessions, past, tz=_amsterdam(), end=date(2026, 10, 3)) == []
    assert kept_workouts(sessions, past, tz=_amsterdam(), start=date(2026, 10, 4)) == [row]
    # For a Home Assistant set to UTC it ended on the 3rd.
    assert len(kept_workouts(sessions, past, tz=UTC, end=date(2026, 10, 3))) == 1


def test_the_kept_workouts_as_data_hold_the_short_ones_too() -> None:
    """The shortest workout is an option of the sensors; whoever reads the list picks."""
    sessions = _short_and_long()
    assert len(_day(sessions, date(2026, 10, 4), min_minutes=30)) == 1
    assert [row["minutes"] for row in kept_workouts(sessions, {}, tz=_amsterdam())] == [10, 15, 60]


def test_no_workouts_as_data_without_workouts() -> None:
    assert kept_workouts({}, {}, tz=_amsterdam()) == []
    # A row that cannot be read is left out, not fatal.
    assert kept_workouts({"worse": "x"}, {"bad": {"type": "56"}}, tz=_amsterdam()) == []


def test_past_workouts_go_by_the_day_they_ended() -> None:
    """As the ledger lets its sessions go: by the local day, one that still waits included."""
    sessions: dict = {}
    past: dict = {}
    _fold(sessions, past, _old("a", 19), _old("b", 20))
    _fold(sessions, past, _old("c", 21), now=LATER)
    assert prune_past(past, before=date(2026, 9, 19), tz=_amsterdam()) is False
    assert prune_past(past, before=date(2026, 9, 21), tz=_amsterdam()) is True
    assert set(past) == {"c"}
    assert prune_past(past, before=date(2026, 9, 22), tz=_amsterdam()) is True
    assert past == {}


# --- Last workout ------------------------------------------------------------


def test_the_last_workout_is_the_newest_session_of_the_payload() -> None:
    update = last_workout_update(
        [
            _record("79", "2026-10-04T05:00:00Z", "2026-10-04T05:12:00Z", 720, uuid="walk"),
            _record(
                "56",
                "2026-10-04T07:00:00Z",
                "2026-10-04T08:00:00Z",
                3600,
                uuid="run",
                source="com.x",
            ),
            _record("8", "2026-10-04T06:00:00Z", "2026-10-04T06:30:00Z", 1800, uuid="ride"),
        ],
        tz=_amsterdam(),
    )
    assert update is not None
    assert update.key == KEY_LAST_WORKOUT
    assert update.value == "running"
    assert update.measured_at == datetime(2026, 10, 4, 8, 0, tzinfo=UTC)
    assert update.attributes == {
        "start": "2026-10-04T09:00:00+02:00",
        "end": "2026-10-04T10:00:00+02:00",
        "duration_minutes": 60,
        "type_code": "56",
        "source": "com.x",
        "uuid": "run",
    }


def test_the_last_workout_needs_no_source_and_keeps_the_raw_type() -> None:
    update = last_workout_update([_record("9999")], tz=_amsterdam())
    assert update is not None
    assert update.value == "other"
    assert update.attributes["type_code"] == "9999"
    assert "source" not in update.attributes
    assert "uuid" not in update.attributes


def test_the_last_workout_of_an_iphone() -> None:
    update = last_workout_update([_record("hiit")], tz=_amsterdam())
    assert update is not None
    assert update.value == "hiit"
    assert update.attributes["type_code"] == "hiit"


def test_the_last_workout_skips_what_is_too_short() -> None:
    records = [
        _record("56", "2026-10-04T07:00:00Z", "2026-10-04T08:00:00Z", 3600, uuid="run"),
        _record("79", "2026-10-04T09:00:00Z", "2026-10-04T09:12:00Z", 720, uuid="walk"),
    ]
    assert last_workout_update(records, tz=_amsterdam()).value == "walking"
    assert last_workout_update(records, tz=_amsterdam(), min_minutes=30).value == "running"
    assert last_workout_update(records, tz=_amsterdam(), min_minutes=61) is None


@pytest.mark.parametrize("records", [None, [], "running", {}, [None, "x", {}]])
def test_no_workout_no_update(records: object) -> None:
    assert last_workout_update(records, tz=_amsterdam()) is None


# --- The ledger --------------------------------------------------------------


def test_apply_payload_keeps_the_workouts_and_the_statistics_as_they_were() -> None:
    """The statistic books a session on the day it ended and knows no type: unchanged."""
    ledger = Ledger()
    change = apply_payload(
        ledger,
        _payload(_record(uuid="run"), _record("79", seconds=1800, uuid="walk")),
        tz=_amsterdam(),
        now=NOW,
    )
    assert change.workouts
    assert not change.is_empty
    assert ledger.sessions["exercise_minutes"] == {
        "run": ["2026-10-04", 60.0],
        "walk": ["2026-10-04", 30.0],
    }
    assert set(ledger.workouts) == {"run", "walk"}
    assert ledger.day_totals("exercise_minutes") == {"2026-10-04": 90.0}


def test_a_workout_of_two_days_ago_is_a_past_workout_and_the_statistic_takes_it() -> None:
    ledger = Ledger()
    # Two days ago: not one of the two days, and the statistic still takes it.
    change = apply_payload(
        ledger,
        _payload(_record(uuid="old", start="2026-10-02T07:00:00Z", end="2026-10-02T08:00:00Z")),
        tz=_amsterdam(),
        now=NOW,
    )
    assert change.workouts
    assert ledger.workouts == {}
    assert set(ledger.past_workouts) == {"old"}
    assert not change.is_empty
    assert ledger.sessions["exercise_minutes"] == {"old": ["2026-10-02", 60.0]}


def test_a_test_ping_keeps_nothing() -> None:
    ledger = Ledger()
    change = apply_payload(
        ledger, {"test": True, "exercise": [_record(uuid="a")]}, tz=_amsterdam(), now=NOW
    )
    assert not change.workouts
    assert ledger.workouts == {}
    assert ledger.past_workouts == {}


def test_the_ledger_survives_a_round_trip() -> None:
    ledger = Ledger()
    apply_payload(ledger, _payload(_record(uuid="run")), tz=_amsterdam(), now=NOW)
    restored = Ledger.from_dict(json.loads(json.dumps(ledger.to_dict())))
    assert restored.workouts == ledger.workouts
    assert restored.sessions == ledger.sessions
    # No workout is older than yesterday, so the file is what it was.
    assert "past_workouts" not in ledger.to_dict()


def test_the_past_workouts_survive_a_round_trip() -> None:
    """One that is finished and one that still waits, and the wait carries on after it."""
    ledger = Ledger()
    apply_payload(ledger, _payload(_old("first", 20)), tz=_amsterdam(), now=NOW)
    apply_payload(ledger, _payload(_old("second", 21)), tz=_amsterdam(), now=LATER)
    restored = Ledger.from_dict(json.loads(json.dumps(ledger.to_dict())))
    assert restored.past_workouts == ledger.past_workouts
    assert restored.past_workouts["first"] == {**OLD_ROW, "details": {}}
    assert "received" in restored.past_workouts["second"]

    apply_payload(restored, _payload(), tz=_amsterdam(), now=LATER + timedelta(days=1))
    assert "received" not in restored.past_workouts["second"]


def test_a_past_workout_that_cannot_be_read_is_dropped_not_fatal() -> None:
    stored = {
        "past_workouts": {
            "ok": {**OLD_ROW, "details": {"steps": 4500}},
            "waiting": {**OLD_ROW, "received": 1790000000},
            "no end": {"type": "56", "details": {}},
            "neither": dict(OLD_ROW),
            "since when": {**OLD_ROW, "received": "yesterday"},
            "no details": {**OLD_ROW, "details": "none"},
            "worse": "x",
        }
    }
    assert set(Ledger.from_dict(stored).past_workouts) == {"ok", "waiting"}
    assert Ledger.from_dict({"past_workouts": "x"}).past_workouts == {}


def test_a_ledger_from_before_the_workouts_loads() -> None:
    """What 0.9.0 wrote: no workouts key. Everything in it must come through."""
    stored = {
        "days": {"steps": {"2026-10-03": 5000.0}},
        "sessions": {"exercise_minutes": {"u": ["2026-10-03", 45.0]}},
        "hours": {
            "heart_rate": {"2026-10-03T10:00:00+00:00": {"n": 2, "mean": 60, "min": 58, "max": 62}}
        },
    }
    ledger = Ledger.from_dict(stored)
    assert ledger.workouts == {}
    assert ledger.past_workouts == {}
    assert ledger.days == stored["days"]
    assert ledger.sessions == stored["sessions"]
    assert ledger.hours["heart_rate"]["2026-10-03T10:00:00+00:00"].count == 2
    assert Ledger.from_dict(None).workouts == {}


# --- The texts ---------------------------------------------------------------


def _load(name: str) -> dict:
    with open(f"custom_components/life_dashboard/{name}") as handle:
        return json.load(handle)


def test_every_kind_has_an_english_text() -> None:
    """A state without a translation shows as its raw key in the interface."""
    states = _load("strings.json")["entity"]["sensor"]["last_workout"]["state"]
    assert set(states) == set(WORKOUT_TYPES)
    assert all(text.strip() for text in states.values())
    assert len(set(states.values())) == len(states), "two kinds read the same"


def test_every_kind_has_a_german_text() -> None:
    german = _load("translations/de.json")["entity"]["sensor"]
    english = _load("strings.json")["entity"]["sensor"]
    assert set(german["last_workout"]["state"]) == set(WORKOUT_TYPES)
    assert all(text.strip() for text in german["last_workout"]["state"].values())
    for key in ("last_workout", "exercise_today", "exercise_yesterday"):
        assert german[key]["name"].strip()
        assert german[key]["name"] != english[key]["name"]


def test_a_translation_has_no_key_the_english_one_lacks() -> None:
    """hassfest takes any subset of strings.json; a key that is not there is a typo."""

    def keys(node: object, path: str = "") -> set[str]:
        if not isinstance(node, dict):
            return {path}
        return {leaf for key, value in node.items() for leaf in keys(value, f"{path}.{key}")}

    assert keys(_load("translations/de.json")) <= keys(_load("strings.json"))


def test_the_texts_use_the_same_placeholders() -> None:
    german = json.dumps(_load("translations/de.json"))
    english = json.dumps(_load("strings.json"))
    assert set(re.findall(r"\{\w+\}", german)) <= set(re.findall(r"\{\w+\}", english))
