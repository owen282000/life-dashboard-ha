"""Test the ledger and the arithmetic behind the statistics.

No Home Assistant here: this is the part that decides what the history says, and it is
worth pinning down on its own. The bug these tests exist for is a year of backfill
landing entirely on today, so most of them are about a payload describing the past.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from custom_components.life_dashboard.history import (
    DAY_KEYS,
    HOUR_KEYS,
    HourBucket,
    Ledger,
    apply_payload,
    day_rows,
    earliest_day,
    hour_rows,
    prune,
)


def _amsterdam() -> ZoneInfo:
    """A real zone: local midnight is not a UTC midnight, which the day rows must respect."""
    return ZoneInfo("Europe/Amsterdam")


def _payload(**extra) -> dict:
    payload = {
        "timestamp": "2026-09-15T18:00:00Z",
        "app_version": "1.16.1",
        "source": "health_connect",
    }
    payload.update(extra)
    return payload


def _backfill(**extra) -> dict:
    return _payload(backfill=True, **extra)


# --- Day sums from the aggregate ------------------------------------------


def test_daily_totals_land_on_their_own_day() -> None:
    ledger = Ledger()
    change = apply_payload(
        ledger,
        _backfill(
            daily_totals=[
                {"date": "2026-03-01", "steps": 8000, "distance_meters": 6000.0},
                {"date": "2026-03-02", "steps": 12000},
            ]
        ),
        tz=_amsterdam(),
    )

    assert ledger.days["steps"] == {"2026-03-01": 8000.0, "2026-03-02": 12000.0}
    assert ledger.days["distance"] == {"2026-03-01": 6000.0}
    # The change names March, months before the payload's own timestamp.
    assert change.day_keys["steps"] == date(2026, 3, 1)
    assert earliest_day(change) == date(2026, 3, 1)


def test_a_day_is_replaced_not_added_to() -> None:
    """Health Connect's aggregate is the authority for a day, so a re-read wins."""
    ledger = Ledger()
    apply_payload(
        ledger, _payload(daily_totals=[{"date": "2026-09-15", "steps": 4000}]), tz=_amsterdam()
    )
    apply_payload(
        ledger, _payload(daily_totals=[{"date": "2026-09-15", "steps": 9000}]), tz=_amsterdam()
    )

    assert ledger.days["steps"] == {"2026-09-15": 9000.0}


def test_resending_the_same_day_changes_nothing() -> None:
    ledger = Ledger()
    payload = _payload(daily_totals=[{"date": "2026-09-15", "steps": 4000}])
    apply_payload(ledger, payload, tz=_amsterdam())
    change = apply_payload(ledger, payload, tz=_amsterdam())

    assert change.is_empty
    assert ledger.days["steps"] == {"2026-09-15": 4000.0}


def test_a_payload_without_daily_totals_leaves_the_days_alone() -> None:
    """What a backfill from app 1.16 sends: records, no aggregate."""
    ledger = Ledger()
    apply_payload(
        ledger, _payload(daily_totals=[{"date": "2026-09-15", "steps": 4000}]), tz=_amsterdam()
    )
    apply_payload(
        ledger,
        _backfill(
            steps=[
                {
                    "count": 120,
                    "start_time": "2026-03-01T08:00:00Z",
                    "end_time": "2026-03-01T08:05:00Z",
                }
            ]
        ),
        tz=_amsterdam(),
    )

    # Raw step records are never summed: a phone and a watch both record the same walk.
    assert ledger.days["steps"] == {"2026-09-15": 4000.0}


# --- Sessions, keyed by uuid ----------------------------------------------


def test_a_sleep_session_lands_on_its_own_day() -> None:
    ledger = Ledger()
    change = apply_payload(
        ledger,
        _backfill(
            sleep=[
                {
                    "session_end_time": "2026-03-01T06:45:00Z",
                    "duration_seconds": 27300,
                    "uuid": "s1",
                }
            ]
        ),
        tz=_amsterdam(),
    )

    assert ledger.sessions["sleep_minutes"]["s1"] == ["2026-03-01", 455.0]
    assert change.day_keys["sleep_minutes"] == date(2026, 3, 1)


def test_the_same_session_twice_does_not_double() -> None:
    ledger = Ledger()
    payload = _payload(
        sleep=[
            {"session_end_time": "2026-09-15T06:45:00Z", "duration_seconds": 27300, "uuid": "s1"}
        ]
    )
    apply_payload(ledger, payload, tz=_amsterdam())
    change = apply_payload(ledger, payload, tz=_amsterdam())

    assert change.is_empty
    assert ledger.day_totals("sleep_minutes") == {"2026-09-15": 455.0}


def test_an_edited_session_replaces_its_own_contribution() -> None:
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            sleep=[
                {
                    "session_end_time": "2026-09-15T06:45:00Z",
                    "duration_seconds": 27300,
                    "uuid": "s1",
                }
            ]
        ),
        tz=_amsterdam(),
    )
    apply_payload(
        ledger,
        _payload(
            sleep=[
                {
                    "session_end_time": "2026-09-15T07:15:00Z",
                    "duration_seconds": 29100,
                    "uuid": "s1",
                }
            ]
        ),
        tz=_amsterdam(),
    )

    assert ledger.day_totals("sleep_minutes") == {"2026-09-15": 485.0}


def test_a_session_that_moves_to_another_day_changes_both() -> None:
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            sleep=[
                {
                    "session_end_time": "2026-09-15T06:45:00Z",
                    "duration_seconds": 27300,
                    "uuid": "s1",
                }
            ]
        ),
        tz=_amsterdam(),
    )
    change = apply_payload(
        ledger,
        _payload(
            sleep=[
                {
                    "session_end_time": "2026-09-12T06:45:00Z",
                    "duration_seconds": 27300,
                    "uuid": "s1",
                }
            ]
        ),
        tz=_amsterdam(),
    )

    # The earlier of the two days, because every later row has to be rewritten.
    assert change.day_keys["sleep_minutes"] == date(2026, 9, 12)
    assert ledger.day_totals("sleep_minutes") == {"2026-09-12": 455.0}


def test_two_sessions_on_one_day_add_up() -> None:
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            mindfulness=[
                {"end_time": "2026-09-15T08:10:00Z", "duration_seconds": 600, "uuid": "m1"},
                {"end_time": "2026-09-15T20:30:00Z", "duration_seconds": 900, "uuid": "m2"},
            ]
        ),
        tz=_amsterdam(),
    )

    assert ledger.day_totals("mindfulness_minutes") == {"2026-09-15": 25.0}


def test_a_session_without_a_uuid_still_cannot_double() -> None:
    ledger = Ledger()
    payload = _payload(exercise=[{"end_time": "2026-09-15T17:40:00Z", "duration_seconds": 2400}])
    apply_payload(ledger, payload, tz=_amsterdam())
    apply_payload(ledger, payload, tz=_amsterdam())

    assert ledger.day_totals("exercise_minutes") == {"2026-09-15": 40.0}


def test_a_session_ending_after_local_midnight_belongs_to_the_next_day() -> None:
    ledger = Ledger()
    apply_payload(
        ledger,
        # 23:30 UTC is 01:30 the next day in Amsterdam.
        _payload(hydration=[{"end_time": "2026-09-15T23:30:00Z", "liters": 0.3, "uuid": "h1"}]),
        tz=_amsterdam(),
    )

    assert ledger.day_totals("hydration_total") == {"2026-09-16": 0.3}


# --- Measured values, hour by hour ----------------------------------------


def test_readings_become_hourly_buckets() -> None:
    ledger = Ledger()
    change = apply_payload(
        ledger,
        _backfill(
            heart_rate=[
                {"bpm": 60, "time": "2026-03-01T08:10:00Z"},
                {"bpm": 70, "time": "2026-03-01T08:50:00Z"},
                {"bpm": 90, "time": "2026-03-01T09:05:00Z"},
            ]
        ),
        tz=_amsterdam(),
    )

    buckets = ledger.hours["heart_rate"]
    assert len(buckets) == 2
    eight = buckets["2026-03-01T08:00:00+00:00"]
    assert (eight.count, eight.mean, eight.minimum, eight.maximum) == (2, 65.0, 60.0, 70.0)
    assert change.hour_keys["heart_rate"] == datetime(2026, 3, 1, 8, tzinfo=UTC)


def test_the_same_hour_twice_keeps_its_shape() -> None:
    """Merging an hour with itself may inflate the count, never the mean or the range."""
    ledger = Ledger()
    payload = _payload(
        heart_rate=[
            {"bpm": 60, "time": "2026-09-15T08:10:00Z"},
            {"bpm": 70, "time": "2026-09-15T08:50:00Z"},
        ]
    )
    apply_payload(ledger, payload, tz=_amsterdam())
    apply_payload(ledger, payload, tz=_amsterdam())

    bucket = ledger.hours["heart_rate"]["2026-09-15T08:00:00+00:00"]
    assert (bucket.mean, bucket.minimum, bucket.maximum) == (65.0, 60.0, 70.0)
    assert bucket.count == 4


def test_a_bucketed_series_merges_by_weighted_mean() -> None:
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            heart_rate=[
                {
                    "bucket_start": "2026-09-15T08:00:00Z",
                    "bucket_end": "2026-09-15T08:15:00Z",
                    "sample_count": 30,
                    "avg": 60.0,
                    "min": 55,
                    "max": 66,
                },
                {
                    "bucket_start": "2026-09-15T08:15:00Z",
                    "bucket_end": "2026-09-15T08:30:00Z",
                    "sample_count": 10,
                    "avg": 80.0,
                    "min": 70,
                    "max": 95,
                },
            ],
            _resolutions={"heart_rate": "15m"},
        ),
        tz=_amsterdam(),
    )

    # Both quarters belong to the same hour: (60*30 + 80*10) / 40 = 65.
    bucket = ledger.hours["heart_rate"]["2026-09-15T08:00:00+00:00"]
    assert bucket.count == 40
    assert bucket.mean == 65.0
    assert (bucket.minimum, bucket.maximum) == (55.0, 95.0)


def test_a_bucketed_accumulated_series_is_ignored() -> None:
    """Bucketed steps carry no average; the day figure comes from daily_totals."""
    ledger = Ledger()
    change = apply_payload(
        ledger,
        _payload(
            steps=[
                {
                    "bucket_start": "2026-09-15T08:00:00Z",
                    "bucket_end": "2026-09-15T09:00:00Z",
                    "sample_count": 12,
                    "total": 1840,
                }
            ]
        ),
        tz=_amsterdam(),
    )

    assert change.is_empty
    assert "steps" not in ledger.hours


def test_blood_pressure_yields_two_series() -> None:
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            blood_pressure=[{"systolic": 118.0, "diastolic": 76.0, "time": "2026-09-15T07:15:00Z"}]
        ),
        tz=_amsterdam(),
    )

    assert ledger.hours["blood_pressure_systolic"]["2026-09-15T07:00:00+00:00"].mean == 118.0
    assert ledger.hours["blood_pressure_diastolic"]["2026-09-15T07:00:00+00:00"].mean == 76.0


# --- Rows ------------------------------------------------------------------


def test_day_rows_carry_a_cumulative_sum() -> None:
    ledger = Ledger()
    apply_payload(
        ledger,
        _backfill(
            daily_totals=[
                {"date": "2026-03-01", "steps": 100},
                {"date": "2026-03-02", "steps": 200},
                {"date": "2026-03-03", "steps": 300},
            ]
        ),
        tz=_amsterdam(),
    )

    rows = day_rows(ledger, "steps", date(2026, 3, 1), tz=_amsterdam())
    assert [row["state"] for row in rows] == [100.0, 200.0, 300.0]
    assert [row["sum"] for row in rows] == [100.0, 300.0, 600.0]


def test_rewriting_from_a_later_day_continues_the_sum() -> None:
    """Only the changed days are rewritten, so their sums must carry the earlier ones."""
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            daily_totals=[
                {"date": "2026-03-01", "steps": 100},
                {"date": "2026-03-02", "steps": 200},
                {"date": "2026-03-03", "steps": 300},
            ]
        ),
        tz=_amsterdam(),
    )

    rows = day_rows(ledger, "steps", date(2026, 3, 2), tz=_amsterdam())
    assert [row["start"].date() for row in rows] == [date(2026, 3, 2), date(2026, 3, 3)]
    assert [row["sum"] for row in rows] == [300.0, 600.0]


def test_a_day_row_starts_at_local_midnight() -> None:
    ledger = Ledger()
    apply_payload(
        ledger, _payload(daily_totals=[{"date": "2026-09-15", "steps": 100}]), tz=_amsterdam()
    )

    start = day_rows(ledger, "steps", date(2026, 9, 15), tz=_amsterdam())[0]["start"]
    assert start.tzinfo is not None
    assert (start.hour, start.minute) == (0, 0)
    # Amsterdam is two hours ahead in September, so local midnight is 22:00 UTC.
    assert start.astimezone(UTC) == datetime(2026, 9, 14, 22, tzinfo=UTC)


def test_a_missing_day_produces_no_row() -> None:
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            daily_totals=[
                {"date": "2026-03-01", "steps": 100},
                {"date": "2026-03-03", "steps": 300},
            ]
        ),
        tz=_amsterdam(),
    )

    rows = day_rows(ledger, "steps", date(2026, 3, 1), tz=_amsterdam())
    assert [row["start"].date() for row in rows] == [date(2026, 3, 1), date(2026, 3, 3)]


def test_day_rows_fold_sessions_into_the_days() -> None:
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            sleep=[
                {
                    "session_end_time": "2026-09-14T06:00:00Z",
                    "duration_seconds": 28800,
                    "uuid": "a",
                },
                {
                    "session_end_time": "2026-09-15T06:00:00Z",
                    "duration_seconds": 25200,
                    "uuid": "b",
                },
            ]
        ),
        tz=_amsterdam(),
    )

    rows = day_rows(ledger, "sleep_minutes", date(2026, 9, 14), tz=_amsterdam())
    assert [row["state"] for row in rows] == [480.0, 420.0]
    assert [row["sum"] for row in rows] == [480.0, 900.0]


def test_hour_rows_carry_mean_min_and_max() -> None:
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            heart_rate=[
                {"bpm": 60, "time": "2026-09-15T08:10:00Z"},
                {"bpm": 70, "time": "2026-09-15T08:50:00Z"},
                {"bpm": 90, "time": "2026-09-15T09:05:00Z"},
            ]
        ),
        tz=_amsterdam(),
    )

    rows = hour_rows(ledger, "heart_rate", datetime(2026, 9, 15, 8, tzinfo=UTC))
    assert len(rows) == 2
    assert rows[0]["mean"] == 65.0
    assert (rows[0]["min"], rows[0]["max"]) == (60.0, 70.0)
    assert rows[0]["start"] == datetime(2026, 9, 15, 8, tzinfo=UTC)
    # Rows before `since` are left to the rows already written.
    assert hour_rows(ledger, "heart_rate", datetime(2026, 9, 15, 9, tzinfo=UTC))[0]["mean"] == 90.0


def test_rows_for_an_unknown_key_are_empty() -> None:
    ledger = Ledger()
    assert day_rows(ledger, "steps", date(2026, 1, 1), tz=_amsterdam()) == []
    assert hour_rows(ledger, "heart_rate", datetime(2026, 1, 1, tzinfo=UTC)) == []


# --- The ledger itself -----------------------------------------------------


def test_the_ledger_round_trips() -> None:
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            daily_totals=[{"date": "2026-09-15", "steps": 4212}],
            sleep=[
                {
                    "session_end_time": "2026-09-15T06:45:00Z",
                    "duration_seconds": 27300,
                    "uuid": "s1",
                }
            ],
            heart_rate=[{"bpm": 61, "time": "2026-09-15T08:10:00Z"}],
        ),
        tz=_amsterdam(),
    )

    restored = Ledger.from_dict(ledger.to_dict())
    assert restored.days == ledger.days
    assert restored.sessions == ledger.sessions
    assert restored.hours["heart_rate"]["2026-09-15T08:00:00+00:00"].mean == 61.0
    # And it keeps working after a restart.
    apply_payload(
        restored, _payload(daily_totals=[{"date": "2026-09-16", "steps": 100}]), tz=_amsterdam()
    )
    assert restored.day_totals("steps") == {"2026-09-15": 4212.0, "2026-09-16": 100.0}


def test_an_empty_ledger_survives_from_dict() -> None:
    assert Ledger.from_dict(None).days == {}
    assert Ledger.from_dict({}).hours == {}


def test_a_test_ping_changes_nothing() -> None:
    ledger = Ledger()
    change = apply_payload(
        ledger,
        {"test": True, "timestamp": "2026-09-15T18:00:00Z", "source": "health_connect"},
        tz=_amsterdam(),
    )
    assert change.is_empty
    assert ledger.to_dict() == {"days": {}, "sessions": {}, "hours": {}}


def test_bad_records_are_skipped_not_fatal() -> None:
    ledger = Ledger()
    change = apply_payload(
        ledger,
        _payload(
            daily_totals=[{"date": "nonsense", "steps": 1}, {"date": "2026-09-15", "steps": 4000}],
            sleep=["not an object", {"duration_seconds": 100}, {"session_end_time": "bad"}],
            heart_rate=[
                {"bpm": "many", "time": "2026-09-15T08:00:00Z"},
                {"bpm": 61, "time": "2026-09-15T08:10:00Z"},
            ],
        ),
        tz=_amsterdam(),
    )

    assert ledger.days["steps"] == {"2026-09-15": 4000.0}
    assert "sleep_minutes" not in change.day_keys
    assert ledger.hours["heart_rate"]["2026-09-15T08:00:00+00:00"].count == 1


def test_pruning_forgets_the_distant_past() -> None:
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            daily_totals=[
                {"date": "2025-01-01", "steps": 100},
                {"date": "2026-09-15", "steps": 200},
            ],
            sleep=[
                {
                    "session_end_time": "2025-01-01T06:00:00Z",
                    "duration_seconds": 100,
                    "uuid": "old",
                },
                {
                    "session_end_time": "2026-09-15T06:00:00Z",
                    "duration_seconds": 200,
                    "uuid": "new",
                },
            ],
            heart_rate=[
                {"bpm": 60, "time": "2025-01-01T08:00:00Z"},
                {"bpm": 61, "time": "2026-09-15T08:00:00Z"},
            ],
        ),
        tz=_amsterdam(),
    )

    prune(ledger, before=date(2026, 1, 1), tz=_amsterdam())

    assert ledger.days["steps"] == {"2026-09-15": 200.0}
    assert list(ledger.sessions["sleep_minutes"]) == ["new"]
    assert list(ledger.hours["heart_rate"]) == ["2026-09-15T08:00:00+00:00"]


def test_the_key_sets_cover_what_apply_produces() -> None:
    """Whatever the ledger can hold, statistics.py must have metadata for."""
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            daily_totals=[
                {
                    "date": "2026-09-15",
                    "steps": 1,
                    "distance_meters": 1.0,
                    "active_calories": 1.0,
                    "total_calories": 1.0,
                }
            ],
            sleep=[
                {"session_end_time": "2026-09-15T06:00:00Z", "duration_seconds": 60, "uuid": "a"}
            ],
            exercise=[{"end_time": "2026-09-15T17:00:00Z", "duration_seconds": 60, "uuid": "b"}],
            mindfulness=[{"end_time": "2026-09-15T18:00:00Z", "duration_seconds": 60, "uuid": "c"}],
            hydration=[{"end_time": "2026-09-15T19:00:00Z", "liters": 0.3, "uuid": "d"}],
            heart_rate=[{"bpm": 61, "time": "2026-09-15T08:00:00Z"}],
        ),
        tz=_amsterdam(),
    )

    assert set(ledger.days) | set(ledger.sessions) <= DAY_KEYS
    assert set(ledger.hours) <= HOUR_KEYS


def test_an_hour_bucket_merges_symmetrically() -> None:
    a = HourBucket(2, 60.0, 55.0, 65.0)
    b = HourBucket(3, 80.0, 70.0, 90.0)
    assert a.merged(b) == b.merged(a)
    assert a.merged(b).count == 5
    assert a.merged(b).mean == (60.0 * 2 + 80.0 * 3) / 5
