"""Sleep stages: what a session counts for, its main night, and how it can change.

Plain tests on history.py and sleep.py, the arithmetic; the rows and entities they lead to
are in test_sleep_statistics.py and test_sleep_entities.py.
"""

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.life_dashboard.history import (
    Ledger,
    apply_payload,
    closing_rows,
    day_rows,
    prune,
)
from custom_components.life_dashboard.payload import parse_payload, tile_hours
from custom_components.life_dashboard.sleep import (
    KEPT_NIGHTS,
    NIGHT_STATISTICS,
    Sample,
    Segment,
    clean_store,
    last_night,
    main_session,
    night_rows,
    night_values,
    noon,
    parse_samples,
    sleep_updates,
    summarize,
    timeline,
)

AMS = ZoneInfo("Europe/Amsterdam")
NIGHT = "2026-11-08"


def _stage(name, start: str, end: str, **extra) -> dict:
    return {"stage": name, "start_time": start, "end_time": end, **extra}


def _session(
    uuid: str | None,
    stages,
    *,
    end: str = "2026-11-08T06:00:00Z",
    seconds: int = 27000,
    source: str | None = "com.watch",
    **extra,
) -> dict:
    record = {"session_end_time": end, "duration_seconds": seconds, "stages": stages, **extra}
    if uuid is not None:
        record["uuid"] = uuid
    if source is not None:
        record["source"] = source
    return record


def _payload(*sleep, timestamp: str = "2026-11-08T12:00:00Z", **extra) -> dict:
    return {"timestamp": timestamp, "source": "health_connect", "sleep": list(sleep), **extra}


# A realistic Android night: one record, one app, the stages in a row.
ANDROID_STAGES = [
    _stage("light", "2026-11-07T22:30:00Z", "2026-11-07T23:30:00Z"),
    _stage("deep", "2026-11-07T23:30:00Z", "2026-11-08T00:30:00Z"),
    _stage("rem", "2026-11-08T00:30:00Z", "2026-11-08T01:00:00Z"),
    _stage("awake", "2026-11-08T01:00:00Z", "2026-11-08T01:06:00Z"),
    _stage("awake_in_bed", "2026-11-08T01:06:00Z", "2026-11-08T01:10:00Z"),
    _stage("light", "2026-11-08T01:10:00Z", "2026-11-08T06:00:00Z"),
]


def _android(uuid: str = "night-1", **extra) -> dict:
    return _session(uuid, ANDROID_STAGES, **extra)


# A realistic iPhone night: the phone's in_bed over everything, the watch's stages inside
# it, one overlap between two watch samples, and a second app with its own guess.
IPHONE_STAGES = [
    _stage(
        "in_bed",
        "2026-11-07T22:00:00Z",
        "2026-11-08T06:30:00Z",
        uuid="ib-1",
        source="iPhone",
    ),
    _stage(
        "light",
        "2026-11-07T22:30:00Z",
        "2026-11-07T23:30:00Z",
        uuid="w-1",
        source="com.watch",
    ),
    _stage(
        "deep",
        "2026-11-07T23:15:00Z",
        "2026-11-08T00:15:00Z",
        uuid="w-2",
        source="com.watch",
    ),
    _stage(
        "awake",
        "2026-11-08T00:15:00Z",
        "2026-11-08T00:20:00Z",
        uuid="w-3",
        source="com.watch",
    ),
    _stage(
        "rem",
        "2026-11-08T00:20:00Z",
        "2026-11-08T06:00:00Z",
        uuid="w-4",
        source="com.watch",
    ),
    _stage(
        "deep",
        "2026-11-08T02:00:00Z",
        "2026-11-08T04:00:00Z",
        uuid="o-1",
        source="other.app",
    ),
]


def _iphone(uuid: str = "iphone-night", stages=None, **extra) -> dict:
    return _session(uuid, IPHONE_STAGES if stages is None else stages, **extra)


def _ledger(*payloads: dict) -> Ledger:
    ledger = Ledger()
    for payload in payloads:
        apply_payload(ledger, payload, tz=AMS)
    return ledger


def _hours(ledger: Ledger, day: str = NIGHT) -> dict:
    return {key: round(value, 4) for key, value in night_values(ledger.sleep, day).items()}


def _minutes(ledger: Ledger, identity: str) -> dict:
    row = ledger.sleep["nights"][identity]
    return {name: row[name] for name in NIGHT_STATISTICS.values() if name in row}


def test_an_android_night_counts_each_stage() -> None:
    ledger = _ledger(_payload(_android()))
    assert _minutes(ledger, "night-1") == {
        "deep": 60.0,
        "light": 350.0,
        "rem": 30.0,
        "awake": 10.0,
        "asleep": 440.0,
    }
    assert _hours(ledger) == {
        "sleep_deep": 1.0,
        "sleep_light": round(350 / 60, 4),
        "sleep_rem": 0.5,
        "sleep_awake": round(10 / 60, 4),
        "sleep_asleep": round(440 / 60, 4),
    }


def test_an_iphone_night_counts_every_moment_once() -> None:
    ledger = _ledger(_payload(_iphone()))
    # light 22:30-23:15 once the deep sample covers the rest; the other app and in_bed out.
    assert _minutes(ledger, "iphone-night") == {
        "deep": 60.0,
        "light": 45.0,
        "rem": 340.0,
        "awake": 5.0,
        "asleep": 445.0,
    }


def test_the_stored_segments_are_the_clean_timeline() -> None:
    ledger = _ledger(_payload(_iphone()))
    night = last_night(ledger.sleep)
    assert [(s.stage) for s in night.segments] == ["light", "deep", "awake", "rem"]
    for left, right in zip(night.segments, night.segments[1:], strict=False):
        assert left.end <= right.start


def test_a_session_without_a_source_keeps_every_stage_but_never_counts_in_bed_or_twice() -> None:
    """iOS before 1.5.0 sends no source for the session, so no filter can apply."""
    ledger = _ledger(_payload(_session("old", IPHONE_STAGES, source=None)))
    minutes = _minutes(ledger, "old")
    # Now the other app's two hours of deep are in, on top of REM, and in_bed still is not.
    assert minutes["deep"] == 180.0
    assert minutes["rem"] == 220.0
    assert minutes["asleep"] == 445.0


def test_several_sources_do_not_add_up() -> None:
    ledger = _ledger(_payload(_iphone()))
    night = ledger.sleep["nights"]["iphone-night"]
    assert night["source"] == "com.watch"
    assert night["asleep"] == 445.0


@pytest.mark.parametrize(
    "stages",
    [
        None,
        [],
        "not a list",
        ["not an object", 7],
        [{"stage": "deep"}],
        [_stage("deep", "garbage", "2026-11-08T01:00:00Z")],
        [_stage("in_bed", "2026-11-07T22:00:00Z", "2026-11-08T06:00:00Z")],
        [_stage("unknown", "2026-11-07T22:00:00Z", "2026-11-08T06:00:00Z")],
        [_stage("garbage-name", "2026-11-07T22:00:00Z", "2026-11-08T06:00:00Z")],
        [_stage("awake", "2026-11-07T22:00:00Z", "2026-11-08T06:00:00Z")],
    ],
)
def test_no_counted_stages_means_no_row_and_no_main(stages) -> None:
    record = _session("n", stages)
    ledger = _ledger(_payload(record))
    assert main_session(ledger.sleep, NIGHT) is None
    assert _hours(ledger) == {}
    assert ledger.sessions["sleep_minutes"]["n"] == [NIGHT, 450.0], "the sum takes it as before"
    assert sleep_updates(ledger.sleep, tz=AMS) == []


def test_a_stage_list_that_is_missing_altogether() -> None:
    record = {"session_end_time": "2026-11-08T06:00:00Z", "duration_seconds": 100, "uuid": "n"}
    ledger = _ledger(_payload(record))
    assert _hours(ledger) == {}
    assert "stages" not in ledger.sleep or "n" not in ledger.sleep["stages"]


def test_zero_of_one_stage_is_zero_with_a_breakdown() -> None:
    stages = [
        _stage("light", "2026-11-07T22:00:00Z", "2026-11-08T02:00:00Z"),
        _stage("deep", "2026-11-08T02:00:00Z", "2026-11-08T04:00:00Z"),
    ]
    ledger = _ledger(_payload(_session("n", stages)))
    assert _hours(ledger)["sleep_rem"] == 0.0
    assert _hours(ledger)["sleep_awake"] == 0.0
    assert _hours(ledger)["sleep_light"] == 4.0


def test_only_sleeping_reports_asleep_and_no_stages() -> None:
    stages = [_stage("sleeping", "2026-11-07T22:00:00Z", "2026-11-08T06:00:00Z")]
    ledger = _ledger(_payload(_session("n", stages)))
    assert _hours(ledger) == {"sleep_asleep": 8.0}


def test_only_sleeping_with_awake_reports_awake() -> None:
    stages = [
        _stage("sleeping", "2026-11-07T22:00:00Z", "2026-11-08T06:00:00Z"),
        _stage("awake", "2026-11-08T06:00:00Z", "2026-11-08T06:10:00Z"),
    ]
    ledger = _ledger(_payload(_session("n", stages, end="2026-11-08T06:10:00Z")))
    assert _hours(ledger) == {"sleep_asleep": 8.0, "sleep_awake": round(10 / 60, 4)}


def test_the_sum_stays_the_session_while_the_stages_say_what_they_say() -> None:
    """The stage statistics do not have to add up to the session's duration."""
    ledger = _ledger(_payload(_android(seconds=99999)))
    assert ledger.sessions["sleep_minutes"]["night-1"] == [NIGHT, 99999 / 60]
    assert ledger.sleep["nights"]["night-1"]["asleep"] == 440.0


def test_a_stage_without_a_duration_counts_from_its_times() -> None:
    entry = _stage("deep", "2026-11-07T22:00:00Z", "2026-11-07T23:00:00Z")
    assert "duration_seconds" not in entry
    assert parse_samples([entry])[0].end - parse_samples([entry])[0].start == 3600


def test_a_stage_that_lacks_an_end_takes_it_from_the_duration() -> None:
    entry = {"stage": "deep", "start_time": "2026-11-07T22:00:00Z", "duration_seconds": 600}
    samples = parse_samples([entry])
    assert samples[0].end - samples[0].start == 600


def test_a_stage_that_ends_before_it_starts_is_skipped() -> None:
    assert parse_samples([_stage("deep", "2026-11-08T02:00:00Z", "2026-11-08T01:00:00Z")]) == []
    assert parse_samples([_stage("deep", "2026-11-08T02:00:00Z", "2026-11-08T02:00:00Z")]) == []


@pytest.mark.parametrize(
    "fraction", ["", ".1", ".123", ".123456", ".123456789", ".000000000"], ids=str
)
def test_fractional_seconds_of_every_length(fraction: str) -> None:
    entry = _stage("deep", f"2026-11-07T22:00:00{fraction}Z", f"2026-11-07T23:00:00{fraction}Z")
    assert parse_samples([entry])[0].end - parse_samples([entry])[0].start == pytest.approx(3600)


@pytest.mark.parametrize(
    ("wire", "name"),
    [
        ("STAGE_TYPE_DEEP", "deep"),
        ("STAGE_TYPE_AWAKE_IN_BED", "awake"),
        ("STAGE_TYPE_OUT_OF_BED", "out_of_bed"),
        ("deep", "deep"),
        ("REM", "rem"),
        ("awake_in_bed", "awake"),
        ("in_bed", "in_bed"),
        ("sleeping", "sleeping"),
        ("an unknown value", "unknown"),
        (17, "unknown"),
        (None, "unknown"),
    ],
)
def test_wire_stage_names(wire, name: str) -> None:
    sample = parse_samples([_stage(wire, "2026-11-07T22:00:00Z", "2026-11-07T23:00:00Z")])[0]
    assert sample.stage == name


def test_out_of_bed_counts_as_awake_and_keeps_its_name_on_the_timeline() -> None:
    stages = [
        _stage("light", "2026-11-07T22:00:00Z", "2026-11-08T02:00:00Z"),
        _stage("out_of_bed", "2026-11-08T02:00:00Z", "2026-11-08T02:30:00Z"),
    ]
    ledger = _ledger(_payload(_session("n", stages)))
    assert _minutes(ledger, "n")["awake"] == 30.0
    assert [s.stage for s in last_night(ledger.sleep).segments] == ["light", "out_of_bed"]


def test_the_sweep_takes_the_most_specific_stage_of_an_overlap() -> None:
    samples = [
        Sample("unknown", 0, 100),
        Sample("awake", 0, 100),
        Sample("sleeping", 10, 90),
        Sample("light", 20, 80),
        Sample("rem", 30, 70),
        Sample("deep", 40, 60),
    ]
    assert timeline(samples, None) == [
        Segment("awake", 0, 10),
        Segment("sleeping", 10, 20),
        Segment("light", 20, 30),
        Segment("rem", 30, 40),
        Segment("deep", 40, 60),
        Segment("rem", 60, 70),
        Segment("light", 70, 80),
        Segment("sleeping", 80, 90),
        Segment("awake", 90, 100),
    ]


def test_touching_segments_of_one_stage_are_merged_and_gaps_stay() -> None:
    samples = [
        Sample("deep", 0, 10),
        Sample("deep", 10, 20),
        Sample("light", 30, 40),
    ]
    assert timeline(samples, None) == [Segment("deep", 0, 20), Segment("light", 30, 40)]


def test_a_summary_counts_nothing_without_sleep() -> None:
    assert summarize([Segment("awake", 0, 600)]) == {}
    assert summarize([]) == {}


def _nap(uuid: str, source: str = "com.watch", asleep_minutes: int = 40) -> dict:
    return _session(
        uuid,
        [
            _stage(
                "light",
                "2026-11-08T13:00:00Z",
                (datetime(2026, 11, 8, 13, 0) + timedelta(minutes=asleep_minutes)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            )
        ],
        end="2026-11-08T14:00:00Z",
        seconds=asleep_minutes * 60,
        source=source,
    )


def test_a_nap_does_not_add_up_with_the_night() -> None:
    ledger = _ledger(_payload(_android(), _nap("nap-1")))
    assert main_session(ledger.sleep, NIGHT)[0] == "night-1"
    assert _hours(ledger)["sleep_asleep"] == round(440 / 60, 4)
    # The running sum counts both, as it always did.
    assert sum(a for _, a in ledger.sessions["sleep_minutes"].values()) == 450 + 40


def test_a_nap_alone_is_the_main_session_of_its_date() -> None:
    ledger = _ledger(_payload(_nap("nap-1")))
    assert main_session(ledger.sleep, NIGHT)[0] == "nap-1"


def test_two_apps_the_same_night_count_once_and_the_most_asleep_wins() -> None:
    worse = _session(
        "aaa",
        [_stage("light", "2026-11-07T23:00:00Z", "2026-11-08T05:00:00Z")],
        source="other.app",
    )
    best = _android("bbb", source="com.watch")
    ledger = _ledger(_payload(worse, best))
    assert main_session(ledger.sleep, NIGHT)[0] == "bbb"
    assert _hours(ledger)["sleep_asleep"] == round(440 / 60, 4)


def test_a_tie_is_broken_on_the_span_and_then_the_uuid() -> None:
    stage = [_stage("light", "2026-11-07T23:00:00Z", "2026-11-08T05:00:00Z")]
    longer = _session("a-longer", stage, seconds=30000)
    shorter = _session("z-shorter", stage, seconds=25000)
    assert main_session(_ledger(_payload(longer, shorter)).sleep, NIGHT)[0] == "a-longer"
    a = _session("uuid-a", stage, seconds=30000)
    b = _session("uuid-b", stage, seconds=30000)
    for order in ((a, b), (b, a)):
        assert main_session(_ledger(_payload(*order)).sleep, NIGHT)[0] == "uuid-b"


def test_a_night_is_filed_under_the_date_it_ended_in_the_local_zone() -> None:
    before = _session(
        "x",
        [_stage("light", "2026-11-07T20:00:00Z", "2026-11-07T22:30:00Z")],
        end="2026-11-07T22:30:00Z",
        seconds=9000,
    )
    # 22:30 UTC is 23:30 in Amsterdam: still the 7th.
    assert _ledger(_payload(before)).sleep["nights"]["x"]["day"] == "2026-11-07"
    after = _session(
        "y",
        [_stage("light", "2026-11-07T22:00:00Z", "2026-11-07T23:30:00Z")],
        end="2026-11-07T23:30:00Z",
        seconds=5400,
    )
    # 23:30 UTC is 00:30 in Amsterdam: the 8th.
    assert _ledger(_payload(after)).sleep["nights"]["y"]["day"] == "2026-11-08"


@pytest.mark.parametrize(
    ("tz", "day", "hour_utc"),
    [
        ("Europe/Amsterdam", date(2026, 11, 8), 11),
        ("Europe/Amsterdam", date(2026, 7, 8), 10),
        ("Asia/Kolkata", date(2026, 11, 8), 6),
        ("America/St_Johns", date(2026, 11, 8), 15),
    ],
)
def test_the_row_of_a_night_starts_at_local_noon_on_the_utc_hour(tz, day, hour_utc) -> None:
    start = noon(day, ZoneInfo(tz))
    assert start.tzinfo == UTC
    assert start.hour == hour_utc
    assert (start.minute, start.second, start.microsecond) == (0, 0, 0)


@pytest.mark.parametrize(
    ("end", "expected"),
    [
        # The last night of summer time, the clock back an hour in the small hours.
        ("2026-10-25T05:00:00Z", "2026-10-25"),
        # The night the clock goes forward.
        ("2026-03-29T05:00:00Z", "2026-03-29"),
    ],
)
def test_a_night_over_a_clock_change(end: str, expected: str) -> None:
    moment = datetime.fromisoformat(end.replace("Z", "+00:00"))
    ledger = _ledger(
        _payload(
            _session(
                "x",
                [
                    _stage(
                        "light",
                        (moment - timedelta(hours=8)).isoformat(),
                        end,
                    )
                ],
                end=end,
                seconds=8 * 3600,
            )
        )
    )
    assert ledger.sleep["nights"]["x"]["day"] == expected
    assert night_values(ledger.sleep, expected)["sleep_asleep"] == 8.0
    assert noon(date.fromisoformat(expected), AMS).hour in (10, 11)


def test_a_growing_night_replaces_itself() -> None:
    first = _iphone(stages=IPHONE_STAGES[:3], end="2026-11-08T03:00:00Z", seconds=3600 * 5)
    ledger = _ledger(_payload(first))
    grown = _iphone(end="2026-11-08T06:30:00Z", seconds=int(8.5 * 3600))
    apply_payload(ledger, _payload(grown), tz=AMS)
    assert len(ledger.sleep["nights"]) == 1
    assert _minutes(ledger, "iphone-night")["asleep"] == 445.0
    assert list(ledger.sessions["sleep_minutes"]) == ["iphone-night"]


def test_a_revised_night_replaces_the_stages() -> None:
    ledger = _ledger(_payload(_android()))
    revised = [_stage("deep", "2026-11-07T22:30:00Z", "2026-11-08T00:30:00Z")]
    change = apply_payload(
        ledger,
        _payload(_session("night-1", revised, seconds=7200), timestamp="2026-11-08T13:00:00Z"),
        tz=AMS,
    )
    assert _minutes(ledger, "night-1") == {
        "deep": 120.0,
        "light": 0.0,
        "rem": 0.0,
        "awake": 0.0,
        "asleep": 120.0,
    }
    assert change.day_keys["sleep_minutes"] == date(2026, 11, 8)
    assert ledger.sessions["sleep_minutes"]["night-1"] == [NIGHT, 120.0]


def test_a_night_sent_again_changes_nothing() -> None:
    ledger = _ledger(_payload(_android()))
    change = apply_payload(ledger, _payload(_android()), tz=AMS)
    assert change.is_empty
    assert not change.nights
    assert change.night_rows == {}


def test_a_backfill_of_a_known_night_changes_nothing_and_doubles_nothing() -> None:
    ledger = _ledger(_payload(_android()))
    change = apply_payload(
        ledger,
        _payload(_android(), timestamp="2026-11-08T20:00:00Z", backfill=True),
        tz=AMS,
    )
    assert change.is_empty
    assert list(ledger.sessions["sleep_minutes"]) == ["night-1"]
    assert ledger.sessions["sleep_minutes"]["night-1"] == [NIGHT, 450.0]


def test_a_backfill_of_old_nights_lands_on_their_dates() -> None:
    old = _session(
        "old",
        [_stage("deep", "2026-06-01T22:00:00Z", "2026-06-02T01:00:00Z")],
        end="2026-06-02T01:00:00Z",
        seconds=10800,
    )
    ledger = _ledger(_payload(_android()), _payload(old, backfill=True))
    assert _hours(ledger, "2026-06-02")["sleep_deep"] == 3.0
    assert main_session(ledger.sleep, NIGHT)[0] == "night-1"
    # The newest night is still the newest.
    assert last_night(ledger.sleep).day == NIGHT


def test_a_night_older_than_the_ledger_is_not_taken_in() -> None:
    ledger = Ledger()
    ledger.sleep["kept_from"] = "2026-01-01"
    old = _session(
        "old",
        [_stage("deep", "2025-06-01T22:00:00Z", "2025-06-02T01:00:00Z")],
        end="2025-06-02T01:00:00Z",
    )
    change = apply_payload(ledger, _payload(old), tz=AMS)
    assert "old" not in ledger.sleep.get("nights", {})
    assert change.night_rows == {}


def test_only_the_last_fourteen_nights_keep_their_stages() -> None:
    payloads = []
    for days in range(20):
        day = date(2026, 11, 8) - timedelta(days=days)
        payloads.append(
            _payload(
                _session(
                    f"s{days}",
                    [_stage("light", f"{day}T00:00:00Z", f"{day}T05:00:00Z")],
                    end=f"{day}T05:00:00Z",
                    seconds=18000,
                )
            )
        )
    ledger = _ledger(*payloads)
    assert len(ledger.sleep["stages"]) == KEPT_NIGHTS
    assert "s13" in ledger.sleep["stages"] and "s14" not in ledger.sleep["stages"]
    assert len(ledger.sleep["nights"]) == 20, "the summary of every night stays"


def test_a_night_older_than_the_held_ones_loses_nothing() -> None:
    ledger = _ledger(
        *[
            _payload(
                _session(
                    f"s{days}",
                    [
                        _stage(
                            "light",
                            f"{date(2026, 11, 8) - timedelta(days=days)}T00:00:00Z",
                            f"{date(2026, 11, 8) - timedelta(days=days)}T05:00:00Z",
                        )
                    ],
                    end=f"{date(2026, 11, 8) - timedelta(days=days)}T05:00:00Z",
                )
            )
            for days in range(15)
        ]
    )
    assert _hours(ledger, "2026-10-25")["sleep_light"] == 5.0


def test_a_payload_that_is_a_test_or_a_heartbeat_changes_nothing() -> None:
    ledger = _ledger(_payload(_android()))
    before = json.dumps(ledger.sleep, sort_keys=True)
    apply_payload(
        ledger,
        {"timestamp": "2026-11-08T20:00:00Z", "test": True, "sleep": [_android()]},
        tz=AMS,
    )
    assert json.dumps(ledger.sleep, sort_keys=True) == before


def test_a_record_the_app_wrote_itself_is_not_a_night() -> None:
    ledger = _ledger(_payload(_android(source="com.owen282000.lifedashboard")))
    assert not ledger.sleep.get("nights")
    assert not ledger.sessions.get("sleep_minutes")


def test_records_without_a_uuid_replace_themselves() -> None:
    ledger = _ledger(
        _payload(_session(None, ANDROID_STAGES)), _payload(_session(None, ANDROID_STAGES))
    )
    assert len(ledger.sleep["nights"]) == 1
    identity = next(iter(ledger.sleep["nights"]))
    assert list(ledger.sessions["sleep_minutes"]) == [identity]


def test_a_file_round_trip_keeps_everything() -> None:
    ledger = _ledger(_payload(_iphone()))
    restored = Ledger.from_dict(json.loads(json.dumps(ledger.to_dict())))
    assert restored.sleep == ledger.sleep
    assert last_night(restored.sleep).segments == last_night(ledger.sleep).segments


def test_a_ledger_without_sleep_leaves_the_file_as_it_was() -> None:
    ledger = _ledger({"timestamp": "2026-11-08T12:00:00Z", "source": "health_connect"})
    assert "sleep" not in ledger.to_dict()


def test_clean_store_drops_what_cannot_be_read() -> None:
    store = clean_store(
        {
            "nights": {
                "ok": {"day": "2026-11-08", "start": 1.0, "end": 2.0, "asleep": 5.0},
                "bad": "x",
                "no-day": {"start": 1.0, "end": 2.0},
            },
            "stages": {
                "ok": [["deep", 1.0, 2.0]],
                "bad": [["nonsense", 1.0, 2.0]],
                "not-list": "x",
            },
            "gone": {"a": 5.0, "b": "x"},
            "kept_from": "2026-01-01",
        }
    )
    assert list(store["nights"]) == ["ok"]
    assert list(store["stages"]) == ["ok"]
    assert store["gone"] == {"a": 5.0}
    assert store["kept_from"] == "2026-01-01"
    assert clean_store(None) == {}
    assert clean_store("nonsense") == {}


# Corrections.


def _delete(uuid: str, **extra) -> dict:
    return {
        "timestamp": "2026-11-08T14:00:00Z",
        "source": "health_connect",
        "deleted_records": [{"type": "sleep", "uuid": uuid}],
        **extra,
    }


def test_an_android_session_deletion_takes_the_night_and_the_sum_away() -> None:
    ledger = _ledger(_payload(_android()))
    change = apply_payload(ledger, _delete("night-1"), tz=AMS)
    assert "night-1" not in ledger.sleep["nights"]
    assert "night-1" not in ledger.sessions["sleep_minutes"]
    assert change.day_keys["sleep_minutes"] == date(2026, 11, 8)
    assert set(change.night_rows[NIGHT]) == set(NIGHT_STATISTICS)
    assert all(value is None for value in change.night_rows[NIGHT].values())
    assert change.nights is True


def test_a_deletion_that_empties_a_day_keeps_its_rows_moving_with_the_sum() -> None:
    before = _session(
        "a",
        [_stage("light", "2026-11-06T22:00:00Z", "2026-11-07T05:00:00Z")],
        end="2026-11-07T05:00:00Z",
        seconds=25200,
    )
    middle = _android("b")
    after = _session(
        "c",
        [_stage("light", "2026-11-08T22:00:00Z", "2026-11-09T05:00:00Z")],
        end="2026-11-09T05:00:00Z",
        seconds=25200,
    )
    ledger = _ledger(_payload(before, middle, after))
    apply_payload(ledger, _delete("b"), tz=AMS)

    # The rows of the day a correction emptied are written over, not left behind.
    assert ledger.day_totals("sleep_minutes")[NIGHT] == 0.0
    rows = day_rows(ledger, "sleep_minutes", date(2026, 11, 8), tz=AMS)
    assert [(r["state"], r["sum"]) for r in rows] == [(0.0, 420.0), (420.0, 840.0)]
    closing = closing_rows(ledger, "sleep_minutes", date(2026, 11, 8), tz=AMS)
    assert [r["sum"] for r in closing] == [420.0, 420.0]


def test_a_move_that_empties_a_day_keeps_it_for_the_same_reason() -> None:
    ledger = _ledger(
        _payload(
            _session("x", [_stage("deep", "2026-11-07T22:00:00Z", "2026-11-08T05:00:00Z")]),
        ),
        _payload(
            _session(
                "x",
                [_stage("deep", "2026-11-06T22:00:00Z", "2026-11-07T05:00:00Z")],
                end="2026-11-07T05:00:00Z",
            ),
            timestamp="2026-11-08T15:00:00Z",
        ),
    )
    assert ledger.day_totals("sleep_minutes") == {NIGHT: 0.0, "2026-11-07": 450.0}


def test_a_deletion_can_fall_back_to_the_night_before() -> None:
    earlier = _session(
        "a",
        [_stage("deep", "2026-11-06T22:00:00Z", "2026-11-07T05:00:00Z")],
        end="2026-11-07T05:00:00Z",
    )
    ledger = _ledger(_payload(earlier, _android("b")))
    assert last_night(ledger.sleep).day == NIGHT
    apply_payload(ledger, _delete("b"), tz=AMS)
    assert last_night(ledger.sleep).day == "2026-11-07"


def test_a_deletion_of_the_main_session_leaves_the_nap_as_main() -> None:
    ledger = _ledger(_payload(_android(), _nap("nap-1")))
    change = apply_payload(ledger, _delete("night-1"), tz=AMS)
    assert main_session(ledger.sleep, NIGHT)[0] == "nap-1"
    assert change.night_rows[NIGHT]["sleep_asleep"] == 40 / 60


def test_a_deletion_of_an_iphone_stage_takes_it_out_of_the_night() -> None:
    ledger = _ledger(_payload(_iphone()))
    apply_payload(ledger, _delete("w-2"), tz=AMS)
    minutes = _minutes(ledger, "iphone-night")
    assert minutes["deep"] == 0.0
    assert minutes["light"] == 60.0
    assert len(ledger.sleep["stages"]["iphone-night"]) == 5


def test_a_deletion_of_the_samples_at_the_edges_shrinks_the_session() -> None:
    ledger = _ledger(_payload(_iphone()))
    apply_payload(ledger, _delete("ib-1"), tz=AMS)
    row = ledger.sleep["nights"]["iphone-night"]
    samples = ledger.sleep["stages"]["iphone-night"]
    assert row["end"] == max(s[2] for s in samples)
    assert ledger.sessions["sleep_minutes"]["iphone-night"][1] == (row["end"] - row["start"]) / 60


def test_the_last_stage_deleted_takes_the_night_with_it() -> None:
    only = [
        _stage("light", "2026-11-07T22:00:00Z", "2026-11-08T05:00:00Z", uuid="only", source="w")
    ]
    ledger = _ledger(_payload(_session("n", only, source="w")))
    apply_payload(ledger, _delete("only"), tz=AMS)
    assert "n" not in ledger.sleep["nights"]
    assert "n" not in ledger.sessions["sleep_minutes"]
    assert ledger.day_totals("sleep_minutes") == {NIGHT: 0.0}


def test_a_night_older_than_the_held_stages_ignores_a_stage_deletion() -> None:
    ledger = _ledger(_payload(_iphone()))
    ledger.sleep["stages"].clear()
    before = dict(ledger.sleep["nights"]["iphone-night"])
    apply_payload(ledger, _delete("w-2"), tz=AMS)
    assert ledger.sleep["nights"]["iphone-night"] == before


def test_a_deletion_and_a_record_in_one_payload_keeps_the_record() -> None:
    """An older Android app sends one uuid as both."""
    ledger = _ledger(_payload(_android()))
    apply_payload(
        ledger,
        {**_delete("night-1"), "sleep": [_android(seconds=28000)]},
        tz=AMS,
    )
    assert "night-1" in ledger.sleep["nights"]
    assert ledger.sessions["sleep_minutes"]["night-1"] == [NIGHT, 28000 / 60]


def test_delete_and_reinsert_the_same_uuid() -> None:
    ledger = _ledger(_payload(_android()))
    apply_payload(ledger, _delete("night-1"), tz=AMS)
    apply_payload(ledger, _payload(_android(), timestamp="2026-11-08T15:00:00Z"), tz=AMS)
    assert ledger.sessions["sleep_minutes"]["night-1"] == [NIGHT, 450.0]
    assert "night-1" not in ledger.sleep.get("gone", {})


def test_an_older_payload_does_not_bring_a_deleted_night_back() -> None:
    ledger = _ledger(_payload(_android()))
    apply_payload(ledger, _delete("night-1"), tz=AMS)
    apply_payload(ledger, _payload(_android(), timestamp="2026-11-08T13:00:00Z"), tz=AMS)
    assert "night-1" not in ledger.sleep["nights"]
    assert "night-1" not in ledger.sessions["sleep_minutes"]


def test_an_older_deletion_does_not_take_a_newer_record() -> None:
    ledger = _ledger(_payload(_android(), timestamp="2026-11-08T15:00:00Z"))
    apply_payload(ledger, _delete("night-1"), tz=AMS)
    assert "night-1" in ledger.sleep["nights"]


def test_an_older_payload_does_not_replace_a_newer_version() -> None:
    revised = [_stage("deep", "2026-11-07T22:30:00Z", "2026-11-08T00:30:00Z")]
    ledger = _ledger(
        _payload(_session("night-1", revised, seconds=7200), timestamp="2026-11-08T15:00:00Z")
    )
    change = apply_payload(ledger, _payload(_android()), tz=AMS)
    assert change.night_rows == {}
    assert _minutes(ledger, "night-1")["deep"] == 120.0
    assert ledger.sessions["sleep_minutes"]["night-1"] == [NIGHT, 120.0]


def test_a_night_under_a_new_uuid_retires_the_old_one() -> None:
    """An iPhone session's uuid follows its earliest sample; there is no deletion of the
    old uuid, but the two share their stages."""
    first = _iphone("old-hash", stages=IPHONE_STAGES[1:])
    grown = _iphone("new-hash")
    ledger = _ledger(_payload(first), _payload(grown, timestamp="2026-11-08T15:00:00Z"))
    assert list(ledger.sleep["nights"]) == ["new-hash"]
    assert list(ledger.sessions["sleep_minutes"]) == ["new-hash"]


def test_a_late_copy_of_an_earlier_state_of_the_same_night_is_ignored() -> None:
    first = _iphone("old-hash", stages=IPHONE_STAGES[1:])
    grown = _iphone("new-hash")
    ledger = _ledger(_payload(grown, timestamp="2026-11-08T15:00:00Z"), _payload(first))
    assert list(ledger.sleep["nights"]) == ["new-hash"]
    assert list(ledger.sessions["sleep_minutes"]) == ["new-hash"]


def test_two_sessions_merged_retire_the_session_that_was_merged() -> None:
    a = _iphone("a", stages=IPHONE_STAGES[1:3])
    b = _iphone("b", stages=IPHONE_STAGES[3:])
    merged = _iphone("a")
    ledger = _ledger(_payload(a, b), _payload(merged, timestamp="2026-11-08T15:00:00Z"))
    assert list(ledger.sleep["nights"]) == ["a"]
    assert list(ledger.sessions["sleep_minutes"]) == ["a"]


def test_the_limit_of_uuid_detection() -> None:
    """Without a stage in common nothing can tell: the statistics still take the main
    session of the date, and only the running sum counts both, as it always did."""
    other_night = [
        _stage("light", "2026-11-07T22:30:00Z", "2026-11-08T05:00:00Z", uuid="z", source="w")
    ]
    ledger = _ledger(_payload(_iphone("first"), _session("second", other_night, source="w")))
    assert len(ledger.sleep["nights"]) == 2
    assert main_session(ledger.sleep, NIGHT)[0] == "first"
    assert set(ledger.sessions["sleep_minutes"]) == {"first", "second"}


def test_retention_prunes_the_nights_and_takes_no_older_ones_in() -> None:
    old = _session(
        "old",
        [_stage("deep", "2024-01-01T22:00:00Z", "2024-01-02T01:00:00Z")],
        end="2024-01-02T01:00:00Z",
    )
    ledger = _ledger(_payload(old), _payload(_android(), timestamp="2026-11-08T16:00:00Z"))
    assert prune(ledger, before=date(2026, 1, 1), tz=AMS) is True
    assert list(ledger.sleep["nights"]) == ["night-1"]
    assert ledger.sleep["kept_from"] == "2026-01-01"
    apply_payload(ledger, _payload(old, timestamp="2026-11-08T17:00:00Z"), tz=AMS)
    assert "old" not in ledger.sleep["nights"]


def test_a_prune_of_a_taken_over_history_still_prunes_the_nights() -> None:
    old = _session(
        "old",
        [_stage("deep", "2024-01-01T22:00:00Z", "2024-01-02T01:00:00Z")],
        end="2024-01-02T01:00:00Z",
    )
    ledger = _ledger(_payload(old))
    ledger.kept_from = "2026-11-01"
    assert prune(ledger, before=date(2026, 1, 1), tz=AMS) is True
    assert "old" not in ledger.sleep["nights"]


def test_a_taken_over_day_does_not_stop_the_nights_of_a_backfill() -> None:
    ledger = Ledger(kept_from="2026-11-01")
    old = _session(
        "old",
        [_stage("deep", "2026-06-01T22:00:00Z", "2026-06-02T01:00:00Z")],
        end="2026-06-02T01:00:00Z",
        seconds=10800,
    )
    change = apply_payload(ledger, _payload(old), tz=AMS)
    assert ledger.sleep["nights"]["old"]["day"] == "2026-06-02"
    assert "2026-06-02" in change.night_rows
    assert "old" not in ledger.sessions.get("sleep_minutes", {}), "the sum goes the old way"
    assert change.old_days["sleep_minutes"]["2026-06-02"] == 180.0


def test_the_statistics_of_a_change_are_only_what_changed() -> None:
    ledger = _ledger(_payload(_android()))
    earlier = _session(
        "e",
        [_stage("deep", "2026-11-06T22:00:00Z", "2026-11-07T05:00:00Z")],
        end="2026-11-07T05:00:00Z",
    )
    change = apply_payload(ledger, _payload(earlier), tz=AMS)
    assert set(change.night_rows) == {"2026-11-07"}


def test_a_better_second_app_rewrites_the_date() -> None:
    worse = _session(
        "w", [_stage("light", "2026-11-07T23:00:00Z", "2026-11-08T02:00:00Z")], source="x"
    )
    ledger = _ledger(_payload(worse))
    change = apply_payload(ledger, _payload(_android("night-1")), tz=AMS)
    assert change.night_rows[NIGHT]["sleep_deep"] == 1.0


def test_rows_for_a_change_sit_at_noon_with_none_for_what_went() -> None:
    changes = {NIGHT: {"sleep_deep": 1.5, "sleep_rem": None}}
    rows = night_rows(changes, tz=AMS)
    start = datetime(2026, 11, 8, 11, 0, tzinfo=UTC)
    assert rows["sleep_deep"] == [{"start": start, "mean": 1.5, "min": 1.5, "max": 1.5}]
    assert rows["sleep_rem"] == [{"start": start, "mean": None, "min": None, "max": None}]


def test_a_night_in_the_future_does_not_stand_in_for_the_last_one() -> None:
    future = _session(
        "z",
        [_stage("deep", "2030-01-01T22:00:00Z", "2030-01-02T01:00:00Z")],
        end="2030-01-02T01:00:00Z",
    )
    ledger = _ledger(_payload(_android(), future))
    assert last_night(ledger.sleep, today=date(2026, 11, 9)).day == NIGHT
    assert last_night(ledger.sleep).day == "2030-01-02"


def test_the_last_night_sensors_read_from_the_main_session() -> None:
    ledger = _ledger(_payload(_iphone(), _nap("nap-1")))
    updates = {update.key: update for update in sleep_updates(ledger.sleep, tz=AMS)}
    asleep = updates["last_sleep_asleep"]
    assert asleep.value == pytest.approx(445 / 60, abs=1e-4)
    assert asleep.attributes["night"] == NIGHT
    assert asleep.attributes["source"] == "com.watch"
    assert asleep.attributes["deep_minutes"] == 60.0
    assert asleep.attributes["stages"][0]["stage"] == "light"
    assert set(asleep.attributes["stages"][0]) == {"stage", "start", "end"}
    assert asleep.attributes["start"].endswith("+01:00")
    assert updates["last_sleep_deep"].value == 1.0
    assert updates["last_sleep_light"].value == 0.75
    assert updates["last_sleep_rem"].value == pytest.approx(340 / 60, abs=1e-4)
    assert updates["last_sleep_awake"].value == pytest.approx(5 / 60, abs=1e-4)


def test_a_night_of_only_sleeping_has_no_stage_sensors_values() -> None:
    stages = [_stage("sleeping", "2026-11-07T22:00:00Z", "2026-11-08T06:00:00Z")]
    ledger = _ledger(_payload(_session("n", stages)))
    updates = {update.key: update for update in sleep_updates(ledger.sleep, tz=AMS)}
    assert updates["last_sleep_asleep"].value == 8.0
    assert updates["last_sleep_deep"].value is None
    assert updates["last_sleep_asleep"].attributes["asleep_minutes"] == 480.0
    assert "deep_minutes" not in updates["last_sleep_asleep"].attributes


def test_the_sensors_of_parse_payload_are_untouched() -> None:
    """Last sleep duration keeps being the newest session of any kind."""
    updates = parse_payload(_payload(_nap("nap-1"), _android()), tz=AMS)
    sleep = next(update for update in updates if update.key == "sleep_duration")
    assert sleep.attributes["uuid"] == "nap-1"


def test_a_last_night_sensor_reads_the_whole_minute_on_a_tile() -> None:
    assert tile_hours(386) == 6.4334
    assert int(tile_hours(386) * 3600) // 60 == 386
    assert tile_hours(420) == 7.0
    assert tile_hours(90) == 1.5
