"""Test the workout details: calories, distance, pace, heart rate and steps of a workout.

No Home Assistant here. The data is synthetic: a phone with a watch app that records a run
with heart rate every second, and a fitness app that notices walks by itself and writes its
own calories, distance and steps for them.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.life_dashboard.history import Ledger, apply_payload
from custom_components.life_dashboard.payload import APP_PACKAGE, KEY_LAST_WORKOUT, SensorUpdate
from custom_components.life_dashboard.workout_details import (
    DETAIL_KEYS,
    PENDING_SECONDS,
    apply_records,
    prune_pool,
    summarize_exercise,
    windows_of,
    workout_details,
)
from custom_components.life_dashboard.workout_types import segment_type
from custom_components.life_dashboard.workouts import (
    day_updates,
    last_workout_update,
    parse_workout,
    refresh_last_workout,
    stored_workouts,
)

# Sunday 4 October 2026; Amsterdam is on +02:00.
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
WATCH = "org.example.watch"
FIT = "org.example.fitness"


def _amsterdam() -> ZoneInfo:
    return ZoneInfo("Europe/Amsterdam")


def _at(clock: str) -> str:
    return f"2026-10-04T{clock}Z"


def _run(**extra) -> dict:
    """A run of the watch app, 07:00 to 07:50 UTC."""
    record = {
        "type": "56",
        "start_time": _at("07:00:00"),
        "end_time": _at("07:50:00"),
        "duration_seconds": 3000,
        "uuid": "run-1",
        "source": WATCH,
    }
    record.update(extra)
    return record


def _interval(uuid: str, start: str, end: str, source: str = WATCH, **value) -> dict:
    return {"start_time": _at(start), "end_time": _at(end), "uuid": uuid, "source": source, **value}


def _samples(record: str, start: str, seconds: int, bpm, source: str = WATCH) -> list[dict]:
    """One sample per second, as a watch writes them during a workout."""
    first = datetime.fromisoformat(_at(start))
    samples = []
    for offset in range(seconds):
        moment = first + timedelta(seconds=offset)
        millis = int(moment.timestamp() * 1000)
        samples.append(
            {
                "bpm": bpm(offset) if callable(bpm) else bpm,
                "time": moment.isoformat().replace("+00:00", "Z"),
                "uuid": f"{record}#{millis}",
                "source": source,
            }
        )
    return samples


def _payload(sequence: int = 1, **arrays) -> dict:
    return {
        "timestamp": "2026-10-04T12:00:00Z",
        "app_version": "1.26.0",
        "source": "health_connect",
        "sequence": sequence,
        **arrays,
    }


def _details(pool: dict, record: dict | None = None) -> dict:
    return workout_details(pool, parse_workout(record or _run()))


def _pool(*payloads: dict, now: datetime = NOW) -> dict:
    pool: dict = {}
    for payload in payloads:
        apply_records(pool, payload, now=now)
    return pool


# --- Overlap ---------------------------------------------------------------------------


def test_a_record_only_partly_inside_counts_for_its_part() -> None:
    """Calories from 06:40 to 07:20, half of it during the run."""
    pool = _pool(
        _payload(
            active_calories=[
                _interval("a", "06:40:00", "07:20:00", calories=100.0),
                _interval("b", "07:20:00", "07:50:00", calories=300.0),
                _interval("c", "08:00:00", "08:30:00", calories=999.0),
            ]
        )
    )
    assert _details(pool)["active_calories"] == 350


def test_heart_rate_outside_the_workout_does_not_count() -> None:
    pool = _pool(
        _payload(
            heart_rate=[
                *_samples("before", "06:50:00", 60, 200),
                *_samples("during", "07:10:00", 120, lambda s: 120 + s % 41),
                *_samples("after", "07:55:00", 60, 40),
            ]
        )
    )
    details = _details(pool)
    assert details["heart_rate_min"] == 120
    assert details["heart_rate_max"] == 160
    assert 135 <= details["heart_rate_avg"] <= 145


def test_a_moment_on_the_boundary_is_inside() -> None:
    pool = _pool(_payload(heart_rate=_samples("edge", "07:50:00", 1, 99)))
    assert _details(pool)["heart_rate_avg"] == 99


# --- Sources ---------------------------------------------------------------------------


def test_two_apps_are_never_added_up() -> None:
    """The watch and the fitness app both wrote the run's calories, distance and steps."""
    payload = _payload(
        active_calories=[
            _interval("w-cal", "07:00:00", "07:50:00", calories=420.0),
            _interval("f-cal", "07:00:00", "07:50:00", FIT, calories=380.0),
        ],
        distance=[
            _interval("w-dist", "07:00:00", "07:50:00", meters=10000.0),
            _interval("f-dist", "07:00:00", "07:50:00", FIT, meters=9800.0),
        ],
        steps=[
            _interval("w-steps", "07:00:00", "07:50:00", count=8000),
            _interval("f-steps", "07:00:00", "07:50:00", FIT, count=8200),
        ],
    )
    details = _details(_pool(payload))
    # The watch wrote the run, so its figures count, even where another app has more.
    assert details["active_calories"] == 420
    assert details["distance_km"] == 10.0
    assert details["steps"] == 8000


def test_without_figures_of_its_own_app_the_largest_other_app_counts() -> None:
    payload = _payload(
        total_calories=[
            _interval("f-1", "07:00:00", "07:25:00", FIT, calories=250.0),
            _interval("f-2", "07:25:00", "07:50:00", FIT, calories=250.0),
            _interval("o-1", "07:00:00", "07:50:00", "org.example.other", calories=450.0),
        ]
    )
    details = _details(_pool(payload))
    assert details["total_calories"] == 500
    # And a workout without a source takes the largest app too.
    assert _details(_pool(payload), _run(source=None))["total_calories"] == 500


def test_heart_rate_takes_every_app() -> None:
    pool = _pool(
        _payload(
            heart_rate=[
                *_samples("w", "07:10:00", 60, 150),
                *_samples("f", "07:20:00", 60, 170, FIT),
            ]
        )
    )
    details = _details(pool)
    assert (details["heart_rate_min"], details["heart_rate_avg"], details["heart_rate_max"]) == (
        150,
        160,
        170,
    )


def test_what_the_app_wrote_itself_is_ignored() -> None:
    pool = _pool(
        _payload(
            active_calories=[_interval("own", "07:00:00", "07:50:00", APP_PACKAGE, calories=1.0)],
            heart_rate=_samples("own-hr", "07:10:00", 10, 50, APP_PACKAGE),
        )
    )
    assert _details(pool) == {}


# --- Repeats, edits, deletions ---------------------------------------------------------


def _full_payload(sequence: int = 1) -> dict:
    return _payload(
        sequence,
        active_calories=[_interval("cal", "07:00:00", "07:50:00", calories=400.0)],
        distance=[_interval("dist", "07:00:00", "07:50:00", meters=10000.0)],
        steps=[_interval("steps", "07:00:00", "07:50:00", count=8000)],
        heart_rate=_samples("hr", "07:00:00", 600, lambda s: 130 + s % 20),
    )


def test_a_payload_that_comes_again_changes_nothing() -> None:
    pool = _pool(_full_payload())
    before = _details(pool)
    snapshot = repr(pool)
    assert apply_records(pool, _full_payload(), now=NOW + timedelta(minutes=5)) is False
    assert repr(pool) == snapshot
    assert _details(pool) == before
    # In a later backfill, under another sequence, the records still replace themselves.
    assert apply_records(pool, _full_payload(sequence=99), now=NOW) is False
    assert _details(pool) == before


def test_a_sample_twice_in_one_payload_counts_once() -> None:
    samples = _samples("hr", "07:10:00", 2, 100)
    twice = _pool(_payload(heart_rate=[*samples, samples[0], {**samples[0], "bpm": 100}]))
    assert twice == _pool(_payload(heart_rate=samples))


def test_an_edited_record_replaces_its_value() -> None:
    pool = _pool(_full_payload())
    apply_records(
        pool,
        _payload(2, active_calories=[_interval("cal", "07:00:00", "07:50:00", calories=450.0)]),
        now=NOW,
    )
    assert _details(pool)["active_calories"] == 450


def test_an_edited_heart_rate_record_replaces_its_samples() -> None:
    pool = _pool(_payload(heart_rate=_samples("hr", "07:10:00", 60, 100)))
    apply_records(pool, _payload(2, heart_rate=_samples("hr", "07:10:00", 60, 140)), now=NOW)
    assert _details(pool)["heart_rate_avg"] == 140


def test_a_record_that_arrived_in_parts_adds_up() -> None:
    pool = _pool(_payload(heart_rate=_samples("hr", "07:10:00", 60, 100)))
    apply_records(pool, _payload(2, heart_rate=_samples("hr", "07:20:00", 60, 140)), now=NOW)
    assert _details(pool)["heart_rate_avg"] == 120
    # And a part that comes again replaces only itself.
    apply_records(pool, _payload(3, heart_rate=_samples("hr", "07:20:00", 60, 140)), now=NOW)
    assert _details(pool)["heart_rate_avg"] == 120


def test_deleted_records_leave_the_details() -> None:
    pool = _pool(_full_payload())
    apply_records(
        pool,
        _payload(
            2,
            deleted_records=[
                {"type": "active_calories", "uuid": "cal"},
                {"type": "heart_rate", "uuid": "hr"},
                {"type": "steps", "uuid": "nothing-like-it"},
                {"type": "nutrition", "uuid": "dist"},
            ],
        ),
        now=NOW,
    )
    details = _details(pool)
    assert "active_calories" not in details
    assert "heart_rate_avg" not in details
    assert details["distance_km"] == 10.0
    assert details["steps"] == 8000


def test_a_deleted_record_can_come_back() -> None:
    pool = _pool(_full_payload())
    apply_records(
        pool,
        {
            **_payload(2, active_calories=[_interval("cal", "07:00:00", "07:50:00", calories=1.0)]),
            "deleted_records": [{"type": "active_calories", "uuid": "cal"}],
        },
        now=NOW,
    )
    assert _details(pool)["active_calories"] == 1


# --- Buckets ---------------------------------------------------------------------------


def _bucket(start: str, end: str, **fields) -> dict:
    return {"bucket_start": _at(start), "bucket_end": _at(end), **fields}


def test_bucketed_heart_rate_is_weighted_by_its_samples() -> None:
    pool = _pool(
        _payload(
            heart_rate=[
                _bucket("07:10:00", "07:11:00", sample_count=60, avg=150.0, min=140.0, max=171.0),
                _bucket("07:11:00", "07:12:00", sample_count=20, avg=110.0, min=101.0, max=120.0),
                _bucket("09:00:00", "09:01:00", sample_count=60, avg=60.0, min=50.0, max=70.0),
            ],
            _resolutions={"heart_rate": "1m"},
        )
    )
    details = _details(pool)
    assert details["heart_rate_avg"] == 140
    assert (details["heart_rate_min"], details["heart_rate_max"]) == (101, 171)


def test_a_bucket_that_comes_again_replaces_itself_and_a_new_one_combines() -> None:
    first = _payload(1, heart_rate=[_bucket("07:10:00", "07:11:00", sample_count=30, avg=100.0)])
    pool = _pool(first, first)
    assert _details(pool)["heart_rate_avg"] == 100
    # The same window from a later sync, with samples a watch uploaded late.
    apply_records(
        pool,
        _payload(2, heart_rate=[_bucket("07:10:00", "07:11:00", sample_count=10, avg=140.0)]),
        now=NOW,
    )
    assert _details(pool)["heart_rate_avg"] == 110


def test_bucketed_calories() -> None:
    """15-minute buckets: the one across the start counts for its part."""
    pool = _pool(
        _payload(
            active_calories=[
                _bucket("06:45:00", "07:15:00", sample_count=3, total=60.0, sources=[WATCH]),
                _bucket("07:15:00", "07:50:00", sample_count=4, total=300.0, sources=[WATCH]),
            ],
            _resolutions={"active_calories": "15m"},
        )
    )
    assert _details(pool)["active_calories"] == 330


def test_a_complete_bucket_replaces_and_an_incomplete_one_combines() -> None:
    window = ("07:00:00", "07:50:00")
    pool = _pool(_payload(1, total_calories=[_bucket(*window, sample_count=2, total=100.0)]))
    # Another sync could read only part of the window: it adds to it.
    apply_records(
        pool, _payload(2, total_calories=[_bucket(*window, sample_count=1, total=50.0)]), now=NOW
    )
    assert _details(pool, _run(source=None))["total_calories"] == 150
    # The same payload again changes nothing.
    apply_records(
        pool, _payload(2, total_calories=[_bucket(*window, sample_count=1, total=50.0)]), now=NOW
    )
    assert _details(pool, _run(source=None))["total_calories"] == 150
    # A complete one holds the whole window.
    apply_records(
        pool,
        _payload(3, total_calories=[_bucket(*window, sample_count=3, total=160.0, complete=True)]),
        now=NOW,
    )
    assert _details(pool, _run(source=None))["total_calories"] == 160


def test_records_and_buckets_of_one_app_do_not_add_up() -> None:
    """The resolution was changed meanwhile: the records count, not both."""
    pool = _pool(
        _payload(1, steps=[_interval("s", "07:00:00", "07:50:00", count=8000)]),
        _payload(
            2,
            steps=[_bucket("07:00:00", "07:50:00", sample_count=1, total=8000.0, sources=[WATCH])],
        ),
    )
    assert _details(pool)["steps"] == 8000


def test_a_bucket_of_several_apps_is_an_app_of_its_own() -> None:
    pool = _pool(
        _payload(
            distance=[
                _bucket("07:00:00", "07:50:00", sample_count=2, total=19000.0, sources=[WATCH, FIT])
            ]
        )
    )
    # Not the watch's own: only taken because there is nothing else.
    assert _details(pool)["distance_km"] == 19.0
    apply_records(
        pool, _payload(2, distance=[_interval("d", "07:00:00", "07:50:00", meters=9500.0)]), now=NOW
    )
    assert _details(pool)["distance_km"] == 9.5


# --- Laps and sets ---------------------------------------------------------------------


def test_laps_give_the_distance_when_no_record_does() -> None:
    swim = _run(
        type="74",
        laps=[
            {"start_time": _at("07:00:00"), "end_time": _at("07:02:00"), "meters": 50.0},
            {"start_time": _at("07:02:00"), "end_time": _at("07:04:00"), "meters": 50.0},
            {"start_time": _at("07:04:00"), "end_time": _at("07:06:00")},
        ],
    )
    details = workout_details({}, parse_workout(swim))
    assert details == {"distance_km": 0.1, "laps": 3}
    # A distance record wins over the laps.
    pool = _pool(_payload(distance=[_interval("d", "07:00:00", "07:50:00", meters=1500.0)]))
    assert workout_details(pool, parse_workout(swim))["distance_km"] == 1.5


def test_sets_and_repetitions() -> None:
    strength = _run(
        type="70",
        segments=[
            {
                "type": "51",
                "start_time": _at("07:00:00"),
                "end_time": _at("07:01:00"),
                "repetitions": 5,
            },
            {"type": "44", "start_time": _at("07:01:00"), "end_time": _at("07:03:00")},
            {
                "type": "51",
                "start_time": _at("07:03:00"),
                "end_time": _at("07:04:00"),
                "repetitions": 5,
            },
            {
                "type": "5",
                "start_time": _at("07:05:00"),
                "end_time": _at("07:06:00"),
                "repetitions": 8,
            },
            {"type": "5", "start_time": _at("07:07:00"), "end_time": _at("07:08:00")},
            "not a segment",
        ],
    )
    details = workout_details({}, parse_workout(strength))
    assert details == {
        "sets": 4,
        "repetitions": 18,
        "exercises": [
            {"type": "squat", "type_code": "51", "sets": 2, "repetitions": 10},
            {"type": "bench_press", "type_code": "5", "sets": 2, "repetitions": 8},
        ],
    }


def test_pauses_are_no_sets_and_unknown_segments_read_as_other() -> None:
    session = _run(
        type="70",
        segments=[
            {"type": "39", "start_time": _at("07:00:00"), "end_time": _at("07:01:00")},
            {
                "type": "9999",
                "start_time": _at("07:01:00"),
                "end_time": _at("07:02:00"),
                "repetitions": 5,
            },
            {
                "type": 42,
                "start_time": _at("07:02:00"),
                "end_time": _at("07:03:00"),
                "repetitions": 6,
            },
        ],
    )
    details = workout_details({}, parse_workout(session))
    assert details["sets"] == 2
    assert details["exercises"] == [
        {"type": "other", "type_code": "9999", "sets": 1, "repetitions": 5},
        {"type": "pull_up", "type_code": "42", "sets": 1, "repetitions": 6},
    ]


def test_segment_names_follow_health_connect() -> None:
    assert segment_type("51") == "squat"
    assert segment_type("5") == "bench_press"
    assert segment_type(42) == "pull_up"
    assert segment_type("11") == "deadlift"
    assert segment_type("0") == "other"
    assert segment_type("") == "other"
    assert segment_type(True) == "other"


def test_only_rests_are_no_sets() -> None:
    record = {
        "segments": [{"type": "44", "start_time": _at("07:00:00"), "end_time": _at("07:01:00")}]
    }
    assert summarize_exercise(record) == {}
    assert summarize_exercise({"laps": [], "segments": "x"}) == {}


# --- Pace, speed, cadence --------------------------------------------------------------


def _moving(type_: str, meters: float = 10000.0, steps: int = 8000) -> dict:
    pool = _pool(
        _payload(
            distance=[_interval("d", "07:00:00", "07:50:00", meters=meters)],
            steps=[_interval("s", "07:00:00", "07:50:00", count=steps)],
        )
    )
    return workout_details(pool, parse_workout(_run(type=type_)))


@pytest.mark.parametrize("type_", ["56", "57", "79", "37", "running", "walking", "hiking"])
def test_pace_and_cadence_on_foot(type_: str) -> None:
    details = _moving(type_)
    assert details["pace_min_per_km"] == 5.0
    assert details["pace"] == "5:00"
    assert details["cadence_spm"] == 160
    assert "speed_kmh" not in details


def test_a_pace_with_seconds() -> None:
    assert _moving("56", meters=9615.0)["pace"] == "5:12"


@pytest.mark.parametrize("type_", ["8", "9", "cycling"])
def test_speed_on_a_bike(type_: str) -> None:
    details = _moving(type_)
    assert details["speed_kmh"] == 12.0
    assert "pace" not in details
    assert "cadence_spm" not in details
    assert details["steps"] == 8000


def test_no_pace_for_strength_and_none_without_distance() -> None:
    details = _moving("70")
    assert "pace" not in details and "speed_kmh" not in details
    assert details["distance_km"] == 10.0
    assert "pace" not in _moving("56", meters=0.0)


def test_a_workout_without_records_has_no_details() -> None:
    assert workout_details({}, parse_workout(_run())) == {}
    assert workout_details(None, parse_workout(_run())) == {}
    update = last_workout_update([_run()], tz=_amsterdam(), pool={})
    assert set(update.attributes) == {
        "start",
        "end",
        "duration_minutes",
        "type_code",
        "source",
        "uuid",
    }


def test_last_workout_carries_its_details() -> None:
    update = last_workout_update([_run()], tz=_amsterdam(), pool=_pool(_full_payload()))
    assert update.attributes == {
        "start": "2026-10-04T09:00:00+02:00",
        "end": "2026-10-04T09:50:00+02:00",
        "duration_minutes": 50,
        "type_code": "56",
        "source": WATCH,
        "uuid": "run-1",
        "active_calories": 400,
        "distance_km": 10.0,
        "pace_min_per_km": 5.0,
        "pace": "5:00",
        "heart_rate_avg": 140,
        "heart_rate_min": 130,
        "heart_rate_max": 149,
        "steps": 8000,
        "cadence_spm": 160,
    }
    base = {"start", "end", "duration_minutes", "type_code", "source", "uuid"}
    assert set(update.attributes) - base <= set(DETAIL_KEYS)


# --- Through the ledger: order, lateness, retention ------------------------------------


def _apply(ledger: Ledger, payload: dict, now: datetime = NOW):
    return apply_payload(ledger, payload, tz=_amsterdam(), now=now)


def _session_rows(ledger: Ledger, now: datetime = NOW) -> list[dict]:
    updates = day_updates(
        stored_workouts(ledger.workouts), now=now, tz=_amsterdam(), pool=ledger.activity
    )
    return updates[0].attributes["sessions"]


def test_records_that_come_after_the_workout_fill_it_in() -> None:
    """The watch uploads its heart rate and calories an hour after the run was synced."""
    ledger = Ledger()
    _apply(ledger, _payload(exercise=[_run()]))
    assert set(_session_rows(ledger)[0]) == {"type", "start", "end", "minutes"}

    change = _apply(ledger, _full_payload(2), now=NOW + timedelta(hours=1))
    assert change.workouts
    row = _session_rows(ledger, NOW + timedelta(hours=1))[0]
    assert row["active_calories"] == 400
    assert row["heart_rate_avg"] == 140
    assert row["minutes"] == 50


def test_records_that_come_before_the_workout_wait_for_it() -> None:
    """A sync every 15 minutes sends the run's heart rate while it is still going."""
    ledger = Ledger()
    _apply(ledger, _full_payload(1), now=datetime(2026, 10, 4, 7, 30, tzinfo=UTC))
    assert ledger.activity
    _apply(ledger, _payload(2, exercise=[_run()]), now=datetime(2026, 10, 4, 8, 0, tzinfo=UTC))
    assert _session_rows(ledger)[0]["heart_rate_avg"] == 140


def test_records_without_a_workout_are_let_go_after_a_while() -> None:
    ledger = Ledger()
    _apply(ledger, _full_payload(1), now=datetime(2026, 10, 4, 7, 30, tzinfo=UTC))
    later = datetime(2026, 10, 4, 7, 30, tzinfo=UTC) + timedelta(seconds=PENDING_SECONDS + 60)
    change = _apply(
        ledger, _payload(2, steps=[_interval("x", "13:00:00", "13:05:00", count=5)]), now=later
    )
    assert change.workouts
    assert set(ledger.activity["add"]) == {"steps"}
    assert set(ledger.activity["add"]["steps"]) == {"x"}
    assert ledger.activity["heart_rate"] == {}


def test_a_kept_workout_keeps_its_records_and_only_its_minutes() -> None:
    ledger = Ledger()
    # A heart rate record of a whole morning, of which only the run matters.
    _apply(
        ledger,
        _payload(1, exercise=[_run()], heart_rate=_samples("day", "06:00:00", 3 * 3600, 70)),
        now=datetime(2026, 10, 4, 9, 0, tzinfo=UTC),
    )
    much_later = datetime(2026, 10, 4, 21, 30, tzinfo=UTC)
    _apply(
        ledger, _payload(2, steps=[_interval("x", "21:00:00", "21:05:00", count=5)]), now=much_later
    )
    rows = ledger.activity["heart_rate"]["day"][2]
    # The 50 minutes of the run, nothing of the hours around it.
    assert len(rows) == 50
    assert _session_rows(ledger, much_later)[0]["heart_rate_avg"] == 70


def test_the_records_go_with_the_workout_after_two_days() -> None:
    ledger = Ledger()
    _apply(ledger, _full_payload(1))
    _apply(ledger, _payload(2, exercise=[_run()]))
    two_days_later = NOW + timedelta(days=2)
    _apply(
        ledger,
        _payload(3, steps=[_interval("x", "13:00:00", "13:05:00", count=5)]),
        now=two_days_later,
    )
    assert ledger.workouts == {}
    # Recently received, but of a day that is no longer kept: nothing waits for a workout.
    assert not any(ledger.activity.values())
    assert "activity" not in ledger.to_dict()


def test_a_deleted_workout_takes_its_details_along() -> None:
    ledger = Ledger()
    _apply(ledger, _payload(1, exercise=[_run()], heart_rate=_full_payload()["heart_rate"]))
    assert _session_rows(ledger)[0]["heart_rate_avg"] == 140
    _apply(ledger, _payload(2, deleted_records=[{"type": "exercise", "uuid": "run-1"}]))
    assert _session_rows(ledger) == []


def test_the_shortest_workout_option_still_applies() -> None:
    ledger = Ledger()
    _apply(ledger, _full_payload(1))
    _apply(ledger, _payload(2, exercise=[_run()]))
    workouts = stored_workouts(ledger.workouts)
    updates = day_updates(workouts, now=NOW, tz=_amsterdam(), min_minutes=60, pool=ledger.activity)
    assert updates[0].attributes["sessions"] == []
    assert updates[0].value == 0


def test_a_ledger_with_details_is_written_and_read_back() -> None:
    ledger = Ledger()
    _apply(ledger, _full_payload(1))
    _apply(
        ledger,
        _payload(
            2,
            exercise=[
                _run(
                    laps=[
                        {
                            "start_time": _at("07:00:00"),
                            "end_time": _at("07:10:00"),
                            "meters": 2000.0,
                        }
                    ]
                )
            ],
        ),
    )
    data = ledger.to_dict()
    assert data["workouts"]["run-1"]["source"] == WATCH
    assert data["workouts"]["run-1"]["laps"] == 1
    assert data["workouts"]["run-1"]["lap_meters"] == 2000.0
    again = Ledger.from_dict(data)
    assert again.activity == ledger.activity
    assert _session_rows(again) == _session_rows(ledger)


def test_an_unreadable_pool_reads_as_empty() -> None:
    assert Ledger.from_dict({"activity": "x"}).activity == {}
    assert Ledger.from_dict({"activity": {"add": "x", "heart_rate": []}}).activity == {
        "add": {},
        "heart_rate": {},
    }
    # One bad row is dropped, not fatal for every payload after it.
    good = ["org.example.watch", 1, 2, 3.0, 4]
    pool = Ledger.from_dict(
        {
            "activity": {
                "add": {"steps": {"ok": good, "short": [1, 2], "text": [None, "a", 2, 3, 4]}},
                "heart_rate": {"ok": [None, 1, [[1, 2, 1, 60, 60, 60]]], "bad": [None, 1, [[1]]]},
                "nonsense": 1,
            }
        }
    ).activity
    assert pool == {
        "add": {"steps": {"ok": good}},
        "heart_rate": {"ok": [None, 1, [[1, 2, 1, 60, 60, 60]]]},
    }
    # A ledger without any records writes no key for them.
    assert "activity" not in Ledger().to_dict()


# --- Last workout, once its records came later ----------------------------------------


def test_last_workout_is_refreshed_when_its_records_come_later() -> None:
    ledger = Ledger()
    _apply(ledger, _payload(1, exercise=[_run()]))
    current = last_workout_update([_run()], tz=_amsterdam(), pool=ledger.activity)
    workouts = stored_workouts(ledger.workouts)
    assert refresh_last_workout(current, workouts, ledger.activity) is None

    _apply(ledger, _full_payload(2))
    refreshed = refresh_last_workout(current, stored_workouts(ledger.workouts), ledger.activity)
    assert refreshed.key == KEY_LAST_WORKOUT
    assert refreshed.measured_at == current.measured_at
    assert refreshed.attributes["heart_rate_avg"] == 140
    assert refreshed.attributes["uuid"] == "run-1"
    # Once more with the same records: nothing to send.
    assert (
        refresh_last_workout(refreshed, stored_workouts(ledger.workouts), ledger.activity) is None
    )


def test_last_workout_without_a_uuid_is_found_by_its_times() -> None:
    ledger = Ledger()
    run = _run(uuid=None)
    _apply(ledger, _payload(1, exercise=[run], heart_rate=_samples("hr", "07:10:00", 60, 120)))
    current = last_workout_update([run], tz=_amsterdam())
    refreshed = refresh_last_workout(current, stored_workouts(ledger.workouts), ledger.activity)
    assert refreshed.attributes["heart_rate_avg"] == 120


def test_a_last_workout_that_is_no_longer_kept_is_left_alone() -> None:
    current = SensorUpdate(
        key=KEY_LAST_WORKOUT,
        value="running",
        measured_at=NOW,
        attributes={"uuid": "gone", "heart_rate_avg": 150},
    )
    assert refresh_last_workout(current, [], {}) is None
    assert refresh_last_workout(None, [], {}) is None
    broken = SensorUpdate(key=KEY_LAST_WORKOUT, value="running", measured_at=NOW, attributes={})
    kept = {"a": {"type": "56", "start": NOW.isoformat(), "end": NOW.isoformat(), "seconds": 60}}
    assert refresh_last_workout(broken, stored_workouts(kept), {}) is None


def test_prune_keeps_what_is_recent_or_belongs_to_a_workout() -> None:
    pool = _pool(_full_payload())
    workout = parse_workout(_run())
    later = NOW + timedelta(seconds=PENDING_SECONDS + 1)
    cutoff = datetime(2026, 10, 2, 22, 0, tzinfo=UTC)
    assert prune_pool(pool, windows_of([workout]), now=later, cutoff=cutoff) is False
    assert prune_pool(pool, [], now=later, cutoff=cutoff) is True
    assert pool == {"add": {}, "heart_rate": {}}
