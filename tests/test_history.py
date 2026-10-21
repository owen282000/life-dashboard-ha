"""Test the ledger and the arithmetic behind the statistics.

No Home Assistant here: this is the part that decides what the history says, and it is
worth pinning down on its own. The bug these tests exist for is a year of backfill
landing entirely on today, so most of them are about a payload describing the past.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.life_dashboard.history import (
    DAY_KEYS,
    HOUR_KEYS,
    RETENTION_DAYS,
    HourBucket,
    Ledger,
    apply_payload,
    day_rows,
    earliest_day,
    hour_rows,
    keep_from,
    prune,
)
from custom_components.life_dashboard.payload import APP_PACKAGE


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


def test_speed_samples_become_hourly_buckets() -> None:
    """Speed arrives as one object per sample, like heart rate, in metres per second."""
    ledger = Ledger()
    apply_payload(
        ledger,
        _backfill(
            speed=[
                {"meters_per_second": 2.5, "time": "2026-03-01T08:10:00Z", "uuid": "r1#1"},
                {"meters_per_second": 3.5, "time": "2026-03-01T08:11:00Z", "uuid": "r1#2"},
            ]
        ),
        tz=_amsterdam(),
    )

    eight = ledger.hours["speed"]["2026-03-01T08:00:00+00:00"]
    assert (eight.count, eight.mean, eight.minimum, eight.maximum) == (2, 3.0, 2.5, 3.5)


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
    assert ledger.to_dict() == {"days": {}, "sessions": {}, "hours": {}, "workouts": {}}


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


# --- Pruning ---------------------------------------------------------------


def _old_and_new() -> dict:
    """A day, a sleep and an hour in 2025, and the same in September 2026."""
    return _backfill(
        daily_totals=[
            {"date": "2025-01-01", "steps": 100},
            {"date": "2026-09-15", "steps": 200},
        ],
        sleep=[
            {
                "session_end_time": "2025-01-01T06:00:00Z",
                "duration_seconds": 600,
                "uuid": "old",
            },
            {
                "session_end_time": "2026-09-15T06:00:00Z",
                "duration_seconds": 1200,
                "uuid": "new",
            },
        ],
        heart_rate=[
            {"bpm": 60, "time": "2025-01-01T08:00:00Z"},
            {"bpm": 61, "time": "2026-09-15T08:00:00Z"},
        ],
    )


def _old_only() -> dict:
    """The 2025 half of _old_and_new: a backfill or an outbox sending the pruned past again."""
    return _backfill(
        daily_totals=[{"date": "2025-01-01", "steps": 100}],
        sleep=[
            {
                "session_end_time": "2025-01-01T06:00:00Z",
                "duration_seconds": 600,
                "uuid": "old",
            }
        ],
        heart_rate=[{"bpm": 60, "time": "2025-01-01T08:00:00Z"}],
    )


def test_pruning_forgets_the_distant_past() -> None:
    ledger = Ledger()
    apply_payload(ledger, _old_and_new(), tz=_amsterdam())

    assert prune(ledger, before=date(2026, 1, 1), tz=_amsterdam()) is True

    assert ledger.days["steps"] == {"2026-09-15": 200.0}
    assert list(ledger.sessions["sleep_minutes"]) == ["new"]
    assert list(ledger.hours["heart_rate"]) == ["2026-09-15T08:00:00+00:00"]
    # What went is carried as one sum per day key; an hour needs no sum.
    assert ledger.carried == {"steps": 100.0, "sleep_minutes": 10.0}
    assert ledger.kept_from == "2026-01-01"


def test_a_rewrite_after_pruning_continues_the_sum() -> None:
    """The recorder still holds the pruned days, so a later row must count them in."""
    ledger = Ledger()
    apply_payload(ledger, _old_and_new(), tz=_amsterdam())
    before = day_rows(ledger, "steps", date(2026, 9, 15), tz=_amsterdam())
    prune(ledger, before=date(2026, 1, 1), tz=_amsterdam())

    assert day_rows(ledger, "steps", date(2026, 9, 15), tz=_amsterdam()) == before
    assert [row["sum"] for row in before] == [300.0]
    assert [
        r["sum"] for r in day_rows(ledger, "sleep_minutes", date(2026, 9, 15), tz=_amsterdam())
    ] == [30.0]

    apply_payload(
        ledger, _payload(daily_totals=[{"date": "2026-09-16", "steps": 50}]), tz=_amsterdam()
    )
    assert [r["sum"] for r in day_rows(ledger, "steps", date(2026, 9, 16), tz=_amsterdam())] == [
        350.0
    ]


def test_a_pruned_day_sent_again_counts_once() -> None:
    """A repeat of a pruned day is already in the carried sum: taking it in again would
    count it twice, and writing its hour again would overwrite the row with less."""
    ledger = Ledger()
    apply_payload(ledger, _old_and_new(), tz=_amsterdam())
    prune(ledger, before=date(2026, 1, 1), tz=_amsterdam())
    kept = ledger.to_dict()

    change = apply_payload(ledger, _old_only(), tz=_amsterdam())
    assert change.is_empty
    assert ledger.to_dict() == kept

    # Also with another figure for the day, a session under a new uuid, or another hour.
    change = apply_payload(
        ledger,
        _backfill(
            daily_totals=[{"date": "2025-01-01", "steps": 999}],
            sleep=[
                {
                    "session_end_time": "2025-12-31T06:00:00Z",
                    "duration_seconds": 600,
                    "uuid": "other",
                }
            ],
            heart_rate=[
                {
                    "bucket_start": "2025-12-31T22:45:00Z",
                    "bucket_end": "2025-12-31T23:00:00Z",
                    "sample_count": 3,
                    "avg": 70.0,
                }
            ],
        ),
        tz=_amsterdam(),
    )
    assert change.is_empty
    assert ledger.to_dict() == kept
    assert [r["sum"] for r in day_rows(ledger, "steps", date(2026, 9, 15), tz=_amsterdam())] == [
        300.0
    ]


def test_the_first_day_kept_takes_its_first_hour_in_local_time() -> None:
    """Local midnight of 1 January is 23:00 UTC the evening before."""
    ledger = Ledger()
    apply_payload(ledger, _old_and_new(), tz=_amsterdam())
    prune(ledger, before=date(2026, 1, 1), tz=_amsterdam())

    apply_payload(
        ledger,
        _payload(
            heart_rate=[
                {"bpm": 50, "time": "2025-12-31T22:30:00Z"},
                {"bpm": 52, "time": "2025-12-31T23:10:00Z"},
            ]
        ),
        tz=_amsterdam(),
    )
    assert "2025-12-31T22:00:00+00:00" not in ledger.hours["heart_rate"]
    assert ledger.hours["heart_rate"]["2025-12-31T23:00:00+00:00"].mean == 52.0


def test_pruning_again_at_the_same_day_changes_nothing() -> None:
    ledger = Ledger()
    apply_payload(ledger, _old_and_new(), tz=_amsterdam())
    assert prune(ledger, before=date(2026, 1, 1), tz=_amsterdam()) is True
    kept = ledger.to_dict()

    assert prune(ledger, before=date(2026, 1, 1), tz=_amsterdam()) is False
    assert prune(ledger, before=date(2025, 6, 1), tz=_amsterdam()) is False
    assert ledger.to_dict() == kept


def test_pruning_with_nothing_to_drop_leaves_the_file_as_it_was() -> None:
    """Nothing went, so nothing is carried and the ledger has no reason to be written."""
    ledger = Ledger()
    apply_payload(
        ledger, _payload(daily_totals=[{"date": "2026-09-15", "steps": 200}]), tz=_amsterdam()
    )
    kept = ledger.to_dict()

    assert prune(ledger, before=date(2026, 1, 1), tz=_amsterdam()) is False
    assert ledger.kept_from is None
    assert ledger.to_dict() == kept
    assert "kept_from" not in kept and "carried" not in kept


def test_a_day_between_two_prunes_that_was_never_kept_counts_once() -> None:
    """kept_from moves only when something goes, so a day older than the next cutoff that
    arrives for the first time is taken in, written, and carried at the next prune."""
    ledger = Ledger()
    apply_payload(ledger, _old_and_new(), tz=_amsterdam())
    prune(ledger, before=date(2025, 6, 1), tz=_amsterdam())
    assert ledger.kept_from == "2025-06-01"

    change = apply_payload(
        ledger, _backfill(daily_totals=[{"date": "2025-08-01", "steps": 40}]), tz=_amsterdam()
    )
    assert change.day_keys == {"steps": date(2025, 8, 1)}
    assert [r["sum"] for r in day_rows(ledger, "steps", date(2025, 8, 1), tz=_amsterdam())] == [
        140.0,
        340.0,
    ]

    assert prune(ledger, before=date(2026, 1, 1), tz=_amsterdam()) is True
    assert ledger.carried["steps"] == 140.0
    assert [r["sum"] for r in day_rows(ledger, "steps", date(2026, 9, 15), tz=_amsterdam())] == [
        340.0
    ]


def test_the_ledger_round_trips_what_pruning_carries() -> None:
    ledger = Ledger()
    apply_payload(ledger, _old_and_new(), tz=_amsterdam())
    prune(ledger, before=date(2026, 1, 1), tz=_amsterdam())

    restored = Ledger.from_dict(ledger.to_dict())
    assert restored.kept_from == "2026-01-01"
    assert restored.carried == {"steps": 100.0, "sleep_minutes": 10.0}
    assert apply_payload(restored, _old_only(), tz=_amsterdam()).is_empty


def test_the_ledger_keeps_400_days_before_its_newest() -> None:
    ledger = Ledger()
    assert keep_from(ledger, today=date(2026, 10, 14), tz=_amsterdam()) is None

    apply_payload(ledger, _old_and_new(), tz=_amsterdam())
    assert RETENTION_DAYS == 400
    assert keep_from(ledger, today=date(2026, 10, 14), tz=_amsterdam()) == date(2025, 8, 11)

    # A newer hour counts too, on its local day: 22:30 UTC is already the 17th here.
    apply_payload(
        ledger, _payload(heart_rate=[{"bpm": 60, "time": "2026-09-16T22:30:00Z"}]), tz=_amsterdam()
    )
    assert keep_from(ledger, today=date(2026, 10, 14), tz=_amsterdam()) == date(2025, 8, 13)


def test_a_date_in_the_future_cannot_push_the_rest_out() -> None:
    ledger = Ledger()
    apply_payload(ledger, _old_and_new(), tz=_amsterdam())
    apply_payload(
        ledger, _payload(daily_totals=[{"date": "2099-01-01", "steps": 1}]), tz=_amsterdam()
    )

    assert keep_from(ledger, today=date(2026, 9, 16), tz=_amsterdam()) == date(2025, 8, 12)


def test_a_year_of_backfill_is_kept_whole_and_sent_again_counts_once() -> None:
    """Both apps backfill 365 days at most: every day of it must still be in the ledger,
    so a second run of the same backfill replaces itself instead of adding up."""
    today = date(2026, 10, 14)
    days = [today - timedelta(days=n) for n in range(366)]
    backfill = _backfill(
        daily_totals=[{"date": day.isoformat(), "steps": 1000} for day in days],
        sleep=[
            {
                "session_end_time": f"{day.isoformat()}T05:00:00Z",
                "duration_seconds": 3600,
                "uuid": f"sleep-{day.isoformat()}",
            }
            for day in days
        ],
        heart_rate=[{"bpm": 60, "time": f"{day.isoformat()}T12:00:00Z"} for day in days],
    )
    ledger = Ledger()
    apply_payload(ledger, backfill, tz=_amsterdam())

    assert (
        prune(ledger, before=keep_from(ledger, today=today, tz=_amsterdam()), tz=_amsterdam())
        is False
    )
    assert len(ledger.days["steps"]) == 366
    assert len(ledger.sessions["sleep_minutes"]) == 366

    # A week later, the syncs of that week in, and the backfill re-sent from the app's
    # outbox: its oldest day is still kept, so nothing doubles.
    later = today + timedelta(days=7)
    apply_payload(
        ledger,
        _payload(
            daily_totals=[
                {"date": (today + timedelta(days=n)).isoformat(), "steps": 1000}
                for n in range(1, 8)
            ]
        ),
        tz=_amsterdam(),
    )
    assert (
        prune(ledger, before=keep_from(ledger, today=later, tz=_amsterdam()), tz=_amsterdam())
        is False
    )
    change = apply_payload(ledger, backfill, tz=_amsterdam())
    assert not change.day_keys
    first = days[-1]
    assert day_rows(ledger, "steps", first, tz=_amsterdam())[-1]["sum"] == 373_000.0
    assert day_rows(ledger, "sleep_minutes", first, tz=_amsterdam())[-1]["sum"] == 366 * 60.0


def test_pruning_leaves_the_workouts_of_today_and_yesterday() -> None:
    """The exercise sensors read the kept workouts; a prune only touches the past."""
    now = datetime(2026, 10, 14, 12, tzinfo=UTC)
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            daily_totals=[{"date": "2025-01-01", "steps": 100}],
            exercise=[
                {
                    "type": "56",
                    "start_time": "2026-10-13T07:00:00Z",
                    "end_time": "2026-10-13T08:00:00Z",
                    "duration_seconds": 3600,
                    "uuid": "yesterday",
                },
                {
                    "type": "79",
                    "start_time": "2026-10-14T07:00:00Z",
                    "end_time": "2026-10-14T07:30:00Z",
                    "duration_seconds": 1800,
                    "uuid": "today",
                },
            ],
        ),
        tz=_amsterdam(),
        now=now,
    )
    workouts = {uuid: dict(row) for uuid, row in ledger.workouts.items()}
    assert set(workouts) == {"yesterday", "today"}

    before = keep_from(ledger, today=now.date(), tz=_amsterdam())
    assert prune(ledger, before=before, tz=_amsterdam()) is True

    assert ledger.workouts == workouts
    assert set(ledger.sessions["exercise_minutes"]) == {"yesterday", "today"}
    assert ledger.carried == {"steps": 100.0}


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


def test_screen_time_lands_on_its_own_day_and_the_newest_figure_wins() -> None:
    """Every screen time sync re-sends the last seven days, so a date is replaced."""
    ledger = Ledger()
    first = apply_payload(
        ledger,
        _payload(
            source="screen_time",
            screen_time=[
                {"date": "2026-09-15", "total_screen_time_minutes": 180, "apps": []},
                {"date": "2026-09-16", "total_screen_time_minutes": 25},
            ],
        ),
        tz=_amsterdam(),
    )
    assert ledger.days["screen_time"] == {"2026-09-15": 180.0, "2026-09-16": 25.0}
    assert first.day_keys["screen_time"] == date(2026, 9, 15)

    # Later in the day the total has grown; the same yesterday changes nothing.
    second = apply_payload(
        ledger,
        _payload(
            source="screen_time",
            screen_time=[
                {"date": "2026-09-15", "total_screen_time_minutes": 180},
                {"date": "2026-09-16", "total_screen_time_minutes": 61},
            ],
        ),
        tz=_amsterdam(),
    )
    assert ledger.days["screen_time"]["2026-09-16"] == 61.0
    assert second.day_keys == {"screen_time": date(2026, 9, 16)}

    rows = day_rows(ledger, "screen_time", date(2026, 9, 15), tz=_amsterdam())
    assert [(row["state"], row["sum"]) for row in rows] == [(180.0, 180.0), (61.0, 241.0)]
    assert "screen_time" in DAY_KEYS


def test_screen_time_statistics_follow_the_app_filter() -> None:
    """With an app filter the day figure is the apps that are sent, as on the sensors."""
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            source="screen_time",
            app_filter="allowlist",
            screen_time=[
                {
                    "date": "2026-09-16",
                    "total_screen_time_minutes": 180,
                    "filtered_screen_time_minutes": 40,
                }
            ],
        ),
        tz=_amsterdam(),
    )
    assert ledger.days["screen_time"] == {"2026-09-16": 40.0}


def test_a_screen_time_entry_without_a_total_is_skipped() -> None:
    ledger = Ledger()
    change = apply_payload(
        ledger,
        _payload(source="screen_time", screen_time=[{"date": "2026-09-16"}, {"date": "x"}, 3]),
        tz=_amsterdam(),
    )
    assert change.is_empty and "screen_time" not in ledger.days


# --- What the app wrote for us -------------------------------------------


def test_own_records_stay_out_of_the_ledger() -> None:
    """A weight received from Home Assistant must not come back as a second reading."""
    ledger = Ledger()
    change = apply_payload(
        ledger,
        _payload(
            weight=[
                {"kilograms": 81.35, "time": "2026-09-27T06:30:00Z", "source": APP_PACKAGE},
                {"kilograms": 80.0, "time": "2026-09-27T07:30:00Z", "source": "Zepp"},
            ],
            hydration=[
                {"liters": 0.3, "end_time": "2026-09-27T08:00:00Z", "source": APP_PACKAGE},
            ],
        ),
        tz=_amsterdam(),
    )
    assert ledger.hours["weight"]["2026-09-27T07:00:00+00:00"].count == 1
    assert "2026-09-27T06:00:00+00:00" not in ledger.hours["weight"]
    assert "hydration_total" not in ledger.sessions
    assert set(change.hour_keys) == {"weight"}
