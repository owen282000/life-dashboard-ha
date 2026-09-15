"""Test signature verification and payload parsing.

These tests import nothing from Home Assistant, on purpose: the whole contract with
the phone is checked here, so the Home Assistant side has little left to get wrong.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.life_dashboard.payload import (
    KEY_LAST_HEALTH_SYNC,
    KEY_LAST_SCREEN_TIME_SYNC,
    SENSOR_SPECS,
    SensorUpdate,
    parse_instant,
    parse_payload,
    signature_for,
    verify_signature,
)


def _amsterdam() -> ZoneInfo:
    """A real zone, so local midnight is not the same moment as a UTC one.

    Amsterdam is +02:00 in September, which is what makes the day-total boundary
    worth asserting at all.
    """
    return ZoneInfo("Europe/Amsterdam")


def _by_key(updates: list[SensorUpdate]) -> dict[str, SensorUpdate]:
    return {update.key: update for update in updates}


# --- Signing ---------------------------------------------------------------


def test_known_answer_vector() -> None:
    """The app's own unit test vector, so both sides provably agree.

    From WebhookSupportTest.kt in the companion app.
    """
    expected = "sha256=f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
    body = b"The quick brown fox jumps over the lazy dog"
    assert signature_for("key", body) == expected
    assert verify_signature("key", body, expected)


def test_signature_rejects() -> None:
    """A changed body, a wrong secret, a missing header and uppercase hex all fail."""
    body = b'{"test":true}'
    good = signature_for("secret", body)
    assert not verify_signature("secret", body + b" ", good)
    assert not verify_signature("other", body, good)
    assert not verify_signature("secret", body, None)
    assert not verify_signature("secret", body, "")
    assert not verify_signature("secret", body, good.upper())
    assert not verify_signature("secret", body, good.removeprefix("sha256="))


# --- Timestamps ------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "microsecond"),
    [
        ("2026-09-15T16:55:02Z", 0),
        ("2026-09-15T16:55:02.123Z", 123000),
        ("2026-09-15T16:55:02.123456Z", 123456),
        # Java can emit nanoseconds; Python accepts six digits.
        ("2026-09-15T16:55:02.123456789Z", 123456),
    ],
)
def test_parse_instant(value: str, microsecond: int) -> None:
    parsed = parse_instant(value)
    assert parsed.microsecond == microsecond
    assert parsed.tzinfo is not None
    assert parsed.astimezone(UTC).hour == 16


@pytest.mark.parametrize("value", ["2026-09-15T16:55:02", "not a date", None, 12345, ""])
def test_parse_instant_rejects(value) -> None:
    with pytest.raises(ValueError):
        parse_instant(value)


# --- Test pings ------------------------------------------------------------


def test_test_ping_health() -> None:
    """A health test ping updates only the diagnostic sensor."""
    updates = parse_payload(
        {
            "test": True,
            "message": "Test ping from Life Dashboard Companion",
            "timestamp": "2026-09-15T16:55:02Z",
            "source": "health_connect",
        },
        tz=_amsterdam(),
    )
    assert [u.key for u in updates] == [KEY_LAST_HEALTH_SYNC]
    update = updates[0]
    assert update.value == parse_instant("2026-09-15T16:55:02Z")
    assert update.attributes["record_count"] == 0
    assert update.attributes["backfill"] is False


def test_test_ping_screen_time() -> None:
    updates = parse_payload(
        {"test": True, "timestamp": "2026-09-15T16:55:02Z", "source": "screen_time"},
        tz=_amsterdam(),
    )
    assert [u.key for u in updates] == [KEY_LAST_SCREEN_TIME_SYNC]


def test_ios_test_ping() -> None:
    """The iOS ping carries app_version as well."""
    updates = parse_payload(
        {
            "test": True,
            "timestamp": "2026-09-15T16:55:02Z",
            "app_version": "1.3.0",
            "source": "healthkit_ios",
        },
        tz=_amsterdam(),
    )
    assert [u.key for u in updates] == [KEY_LAST_HEALTH_SYNC]
    assert updates[0].attributes["app_version"] == "1.3.0"
    assert updates[0].attributes["source"] == "healthkit_ios"


# --- Health payloads -------------------------------------------------------


def test_health_minimal() -> None:
    """Day totals come from daily_totals, never from summing the raw records."""
    updates = _by_key(
        parse_payload(
            {
                "timestamp": "2026-09-15T16:55:02Z",
                "app_version": "1.15.0",
                "source": "health_connect",
                "daily_totals": [
                    {"date": "2026-09-14", "steps": 8000},
                    {"date": "2026-09-15", "steps": 4212, "distance_meters": 3128.5},
                ],
                "steps": [
                    {
                        "count": 120,
                        "start_time": "2026-09-15T16:50:00Z",
                        "end_time": "2026-09-15T16:55:00Z",
                        "uuid": "abc",
                    }
                ],
            },
            tz=_amsterdam(),
        )
    )

    # The newest day wins, and a missing field produces no sensor rather than a zero.
    assert updates["steps_today"].value == 4212
    assert updates["distance_today"].value == 3128.5
    assert "active_calories_today" not in updates
    assert "total_calories_today" not in updates

    # Local midnight in Amsterdam, which is 22:00 UTC the day before.
    midnight = updates["steps_today"].last_reset
    assert midnight == datetime(2026, 9, 15, 0, 0, tzinfo=_amsterdam())
    assert midnight.astimezone(UTC).hour == 22
    assert updates["steps_today"].attributes == {"date": "2026-09-15"}

    # Raw step records drive no sensor of their own, but they do count.
    assert updates[KEY_LAST_HEALTH_SYNC].attributes["record_count"] == 1


def test_health_every_latest_sensor() -> None:
    """One record of every mapped type gives every latest-value sensor."""
    time = "2026-09-15T10:00:00Z"
    payload = {
        "timestamp": "2026-09-15T16:55:02Z",
        "source": "health_connect",
        "heart_rate": [{"bpm": 62, "time": time}],
        "resting_heart_rate": [{"bpm": 54, "time": time}],
        "heart_rate_variability": [{"heart_rate_variability_millis": 48.5, "time": time}],
        "sleep": [{"session_end_time": time, "duration_seconds": 27000}],
        "weight": [{"kilograms": 78.4, "time": time}],
        "blood_pressure": [{"systolic": 118.0, "diastolic": 76.0, "time": time}],
        "blood_glucose": [{"mmol_per_liter": 5.4, "time": time}],
        "oxygen_saturation": [{"percentage": 97.0, "time": time}],
        "body_temperature": [{"celsius": 36.7, "time": time}],
        "skin_temperature": [{"delta_celsius": -0.25, "time": time}],
        "basal_body_temperature": [{"celsius": 36.4, "time": time}],
        "respiratory_rate": [{"rate": 14.2, "time": time}],
        "hydration": [{"liters": 0.33, "start_time": time, "end_time": time}],
        "body_fat": [{"percentage": 18.2, "time": time}],
        "lean_body_mass": [{"kilograms": 64.1, "time": time}],
        "bone_mass": [{"kilograms": 3.2, "time": time}],
        "body_water_mass": [{"kilograms": 45.0, "time": time}],
        "basal_metabolic_rate": [{"kilocalories_per_day": 1680.0, "time": time}],
        "vo2_max": [{"vo2_ml_per_min_per_kg": 44.1, "time": time}],
        "height": [{"meters": 1.83, "time": time}],
    }
    updates = _by_key(parse_payload(payload, tz=_amsterdam()))

    assert updates["heart_rate"].value == 62
    assert isinstance(updates["heart_rate"].value, int)
    assert updates["resting_heart_rate"].value == 54
    assert updates["heart_rate_variability"].value == 48.5
    # Sleep is sent in seconds and presented in minutes.
    assert updates["sleep_duration"].value == 450
    assert updates["weight"].value == 78.4
    assert updates["blood_pressure_systolic"].value == 118.0
    assert updates["blood_pressure_diastolic"].value == 76.0
    assert updates["blood_glucose"].value == 5.4
    assert updates["oxygen_saturation"].value == 97.0
    assert updates["body_temperature"].value == 36.7
    assert updates["skin_temperature_delta"].value == -0.25
    assert updates["basal_body_temperature"].value == 36.4
    assert updates["respiratory_rate"].value == 14.2
    assert updates["hydration"].value == 0.33
    assert updates["body_fat"].value == 18.2
    assert updates["lean_body_mass"].value == 64.1
    assert updates["bone_mass"].value == 3.2
    assert updates["body_water_mass"].value == 45.0
    assert updates["basal_metabolic_rate"].value == 1680.0
    assert updates["vo2_max"].value == 44.1
    assert updates["height"].value == 1.83

    # Every one of them describes the record's own moment, not the sync's.
    assert updates["heart_rate"].measured_at == parse_instant(time)
    assert updates[KEY_LAST_HEALTH_SYNC].measured_at == parse_instant("2026-09-15T16:55:02Z")
    # 20 arrays, 21 sensors: blood_pressure yields both systolic and diastolic.
    assert updates[KEY_LAST_HEALTH_SYNC].attributes["record_count"] == 20
    assert len([u for u in updates if u not in {KEY_LAST_HEALTH_SYNC}]) == 21


def test_latest_wins_inside_batch() -> None:
    """Records arrive in no particular order; the newest one is the value."""
    updates = _by_key(
        parse_payload(
            {
                "timestamp": "2026-09-15T16:55:02Z",
                "source": "health_connect",
                "heart_rate": [
                    {"bpm": 61, "time": "2026-09-15T12:00:00Z", "uuid": "b"},
                    {"bpm": 58, "time": "2026-09-15T08:00:00Z", "uuid": "a"},
                    {"bpm": 70, "time": "2026-09-15T10:00:00Z", "uuid": "c"},
                ],
            },
            tz=_amsterdam(),
        )
    )
    assert updates["heart_rate"].value == 61
    assert updates["heart_rate"].attributes["uuid"] == "b"


def test_record_attributes() -> None:
    """source and uuid ride along; a missing one is simply absent."""
    updates = _by_key(
        parse_payload(
            {
                "timestamp": "2026-09-15T16:55:02Z",
                "source": "health_connect",
                "weight": [
                    {
                        "kilograms": 78.4,
                        "time": "2026-09-15T10:00:00Z",
                        "uuid": "id-1",
                        "source": "com.google.android.apps.fitness",
                    }
                ],
                "height": [{"meters": 1.83, "time": "2026-09-15T10:00:00Z"}],
            },
            tz=_amsterdam(),
        )
    )
    assert updates["weight"].attributes == {
        "uuid": "id-1",
        "source": "com.google.android.apps.fitness",
    }
    assert updates["height"].attributes == {}


def test_bucketed_measured() -> None:
    """A bucketed average series uses the newest bucket's avg."""
    updates = _by_key(
        parse_payload(
            {
                "timestamp": "2026-09-15T16:55:02Z",
                "source": "health_connect",
                "heart_rate": [
                    {
                        "bucket_start": "2026-09-15T08:00:00Z",
                        "bucket_end": "2026-09-15T08:01:00Z",
                        "sample_count": 58,
                        "avg": 72.4,
                        "min": 66,
                        "max": 81,
                        "sources": ["com.garmin.android.apps.connectmobile"],
                    },
                    {
                        "bucket_start": "2026-09-15T09:00:00Z",
                        "bucket_end": "2026-09-15T09:01:00Z",
                        "sample_count": 60,
                        "avg": 68.1,
                        "min": 60,
                        "max": 77,
                        "sources": ["com.garmin.android.apps.connectmobile", "com.fitbit"],
                    },
                ],
                "_resolutions": {"heart_rate": "1m"},
            },
            tz=_amsterdam(),
        )
    )
    update = updates["heart_rate"]
    assert update.value == 68.1
    assert update.measured_at == parse_instant("2026-09-15T09:01:00Z")
    assert update.attributes["sample_count"] == 60
    assert update.attributes["min"] == 60
    assert update.attributes["max"] == 77
    assert update.attributes["sources"] == "com.garmin.android.apps.connectmobile, com.fitbit"
    assert "uuid" not in update.attributes


def test_bucketed_accumulated_ignored() -> None:
    """Bucketed steps carry no average; the day figure comes from daily_totals."""
    updates = _by_key(
        parse_payload(
            {
                "timestamp": "2026-09-15T16:55:02Z",
                "source": "health_connect",
                "steps": [
                    {
                        "bucket_start": "2026-09-15T08:00:00Z",
                        "bucket_end": "2026-09-15T09:00:00Z",
                        "sample_count": 12,
                        "total": 1840,
                    }
                ],
                "_resolutions": {"steps": "1h"},
            },
            tz=_amsterdam(),
        )
    )
    assert set(updates) == {KEY_LAST_HEALTH_SYNC}


def test_unmapped_types_ignored_but_counted() -> None:
    """Event-like types get no sensor, matching the app's MQTT behaviour."""
    updates = parse_payload(
        {
            "timestamp": "2026-09-15T16:55:02Z",
            "source": "health_connect",
            "exercise": [
                {
                    "type": "running",
                    "start_time": "2026-09-15T08:00:00Z",
                    "end_time": "2026-09-15T08:40:00Z",
                    "duration_seconds": 2400,
                }
            ],
            "nutrition": [{"calories": 540.0, "start_time": "2026-09-15T12:00:00Z"}],
            "menstruation_flow": [{"flow": "light", "time": "2026-09-15T07:00:00Z"}],
            "sexual_activity": [{"protection_used": True, "time": "2026-09-14T22:00:00Z"}],
        },
        tz=_amsterdam(),
    )
    assert [u.key for u in updates] == [KEY_LAST_HEALTH_SYNC]
    assert updates[0].attributes["record_count"] == 4


def test_bad_record_skipped() -> None:
    """One unreadable record must not cost the user the rest of the sync."""
    updates = _by_key(
        parse_payload(
            {
                "timestamp": "2026-09-15T16:55:02Z",
                "source": "health_connect",
                "heart_rate": [
                    {"bpm": 61},  # no time
                    {"time": "2026-09-15T12:00:00Z"},  # no value
                    {"bpm": "many", "time": "2026-09-15T13:00:00Z"},  # not a number
                    "not even an object",
                    {"bpm": 64, "time": "2026-09-15T11:00:00Z"},  # the only usable one
                ],
                "weight": [{"kilograms": 78.4, "time": "nonsense"}],
            },
            tz=_amsterdam(),
        )
    )
    assert updates["heart_rate"].value == 64
    assert "weight" not in updates


def test_backfill_flagged() -> None:
    """Backfill payloads are ordinary payloads with a flag the caller can see."""
    updates = _by_key(
        parse_payload(
            {
                "timestamp": "2026-09-15T16:55:02Z",
                "source": "health_connect",
                "backfill": True,
                "window_start": "2026-09-01T00:00:00Z",
                "window_end": "2026-09-04T00:00:00Z",
                "weight": [{"kilograms": 77.0, "time": "2026-09-02T07:00:00Z"}],
            },
            tz=_amsterdam(),
        )
    )
    assert updates[KEY_LAST_HEALTH_SYNC].attributes["backfill"] is True
    # The old record still produces an update; the caller's ordering rule drops it.
    assert updates["weight"].measured_at == parse_instant("2026-09-02T07:00:00Z")


def test_healthkit_ios_payload() -> None:
    """iOS sends no daily_totals, and its blood pressure can lack diastolic."""
    updates = _by_key(
        parse_payload(
            {
                "timestamp": "2026-09-15T16:55:02Z",
                "app_version": "1.3.0",
                "source": "healthkit_ios",
                "blood_pressure": [
                    {
                        "systolic": 120.0,
                        "diastolic": 78.0,
                        "time": "2026-09-15T08:00:00Z",
                        "uuid": "older-pair",
                    },
                    {
                        "systolic": 124.0,
                        "time": "2026-09-15T10:00:00Z",
                        "uuid": "newer-systolic-only",
                    },
                ],
                "steps": [
                    {
                        "count": 900,
                        "start_time": "2026-09-15T09:00:00Z",
                        "end_time": "2026-09-15T10:00:00Z",
                    }
                ],
            },
            tz=_amsterdam(),
        )
    )
    # No day totals for an iPhone.
    assert "steps_today" not in updates
    # Systolic from the newest record, diastolic from the newest one that has it.
    assert updates["blood_pressure_systolic"].value == 124.0
    assert updates["blood_pressure_systolic"].attributes["uuid"] == "newer-systolic-only"
    assert updates["blood_pressure_diastolic"].value == 78.0
    assert updates["blood_pressure_diastolic"].attributes["uuid"] == "older-pair"
    assert updates[KEY_LAST_HEALTH_SYNC].attributes["source"] == "healthkit_ios"


def test_health_payload_with_only_empty_arrays() -> None:
    """The documented example payload: every array present, all empty."""
    payload = {
        "timestamp": "2026-09-15T12:00:00Z",
        "app_version": "1.15.0",
        "source": "health_connect",
    }
    from custom_components.life_dashboard.payload import HEALTH_ARRAYS

    payload.update({name: [] for name in HEALTH_ARRAYS})
    updates = parse_payload(payload, tz=_amsterdam())
    assert [u.key for u in updates] == [KEY_LAST_HEALTH_SYNC]
    assert updates[0].attributes["record_count"] == 0


# --- Screen time -----------------------------------------------------------


def _screen_time_payload() -> dict:
    return {
        "timestamp": "2026-09-15T16:55:02Z",
        "app_version": "1.15.0",
        "device": "Google Pixel 8",
        "source": "screen_time",
        "screen_time": [
            {
                "date": "2026-09-14",
                "total_screen_time_minutes": 212,
                "apps": [
                    {
                        "package": "com.spotify.music",
                        "name": "Spotify",
                        "minutes": 95,
                        "last_used": "2026-09-14T22:10:00Z",
                    }
                ],
            },
            {
                "date": "2026-09-15",
                "total_screen_time_minutes": 143,
                "apps": [
                    {
                        "package": "com.android.chrome",
                        "name": "Chrome",
                        "minutes": 61,
                        "last_used": "2026-09-15T16:40:00Z",
                    },
                    {
                        "package": "com.whatsapp",
                        "name": "WhatsApp",
                        "minutes": 44,
                        "last_used": "2026-09-15T16:50:00Z",
                    },
                    {
                        "package": "com.spotify.music",
                        "name": "Spotify",
                        "minutes": 22,
                        "last_used": "2026-09-15T15:00:00Z",
                    },
                ],
            },
        ],
    }


def test_screen_time() -> None:
    updates = _by_key(parse_payload(_screen_time_payload(), tz=_amsterdam()))

    assert updates["screen_time_today"].value == 143
    assert updates["screen_time_today"].last_reset == datetime(
        2026, 9, 15, 0, 0, tzinfo=_amsterdam()
    )
    assert updates["screen_time_today"].attributes["app_count"] == 3
    assert updates["screen_time_today"].attributes["top_apps"] == (
        "Chrome (61 min), WhatsApp (44 min), Spotify (22 min)"
    )

    assert updates["screen_time_yesterday"].value == 212
    # Only today's total resets; yesterday is a fixed figure.
    assert updates["screen_time_yesterday"].last_reset is None

    assert updates["screen_time_top_app"].value == "Chrome"
    assert updates["screen_time_top_app"].attributes == {
        "package": "com.android.chrome",
        "minutes": 61,
        "date": "2026-09-15",
    }

    diagnostic = updates[KEY_LAST_SCREEN_TIME_SYNC]
    assert diagnostic.attributes["device"] == "Google Pixel 8"
    assert "record_count" not in diagnostic.attributes


def test_screen_time_single_day() -> None:
    """A fresh install has one day; yesterday simply does not appear."""
    payload = _screen_time_payload()
    payload["screen_time"] = payload["screen_time"][1:]
    updates = _by_key(parse_payload(payload, tz=_amsterdam()))
    assert "screen_time_today" in updates
    assert "screen_time_yesterday" not in updates


def test_screen_time_day_without_apps() -> None:
    """A day with no app rows still gives a total, but no top app."""
    payload = _screen_time_payload()
    payload["screen_time"] = [{"date": "2026-09-15", "total_screen_time_minutes": 12, "apps": []}]
    updates = _by_key(parse_payload(payload, tz=_amsterdam()))
    assert updates["screen_time_today"].value == 12
    assert updates["screen_time_today"].attributes["top_apps"] == ""
    assert "screen_time_top_app" not in updates


def test_screen_time_top_apps_caps_at_five() -> None:
    payload = _screen_time_payload()
    payload["screen_time"] = [
        {
            "date": "2026-09-15",
            "total_screen_time_minutes": 300,
            "apps": [
                {"package": f"app.{n}", "name": f"App {n}", "minutes": n * 10} for n in range(1, 9)
            ],
        }
    ]
    updates = _by_key(parse_payload(payload, tz=_amsterdam()))
    top_apps = updates["screen_time_today"].attributes["top_apps"]
    assert top_apps.count(",") == 4
    assert top_apps.startswith("App 8 (80 min), App 7 (70 min)")
    assert updates["screen_time_today"].attributes["app_count"] == 8


# --- The table itself ------------------------------------------------------


def test_every_emitted_key_has_a_spec() -> None:
    """Whatever the parser can emit, sensor.py must be able to present."""
    payloads = [
        _screen_time_payload(),
        {
            "timestamp": "2026-09-15T16:55:02Z",
            "source": "health_connect",
            "daily_totals": [
                {
                    "date": "2026-09-15",
                    "steps": 1,
                    "distance_meters": 1.0,
                    "active_calories": 1.0,
                    "total_calories": 1.0,
                }
            ],
        },
    ]
    time = "2026-09-15T10:00:00Z"
    payloads.append(
        {
            "timestamp": "2026-09-15T16:55:02Z",
            "source": "health_connect",
            "heart_rate": [{"bpm": 60, "time": time}],
            "resting_heart_rate": [{"bpm": 50, "time": time}],
            "heart_rate_variability": [{"heart_rate_variability_millis": 40.0, "time": time}],
            "sleep": [{"session_end_time": time, "duration_seconds": 100}],
            "weight": [{"kilograms": 70.0, "time": time}],
            "blood_pressure": [{"systolic": 120.0, "diastolic": 80.0, "time": time}],
            "blood_glucose": [{"mmol_per_liter": 5.0, "time": time}],
            "oxygen_saturation": [{"percentage": 98.0, "time": time}],
            "body_temperature": [{"celsius": 36.6, "time": time}],
            "skin_temperature": [{"delta_celsius": 0.1, "time": time}],
            "basal_body_temperature": [{"celsius": 36.5, "time": time}],
            "respiratory_rate": [{"rate": 14.0, "time": time}],
            "hydration": [{"liters": 0.2, "end_time": time}],
            "body_fat": [{"percentage": 20.0, "time": time}],
            "lean_body_mass": [{"kilograms": 60.0, "time": time}],
            "bone_mass": [{"kilograms": 3.0, "time": time}],
            "body_water_mass": [{"kilograms": 40.0, "time": time}],
            "basal_metabolic_rate": [{"kilocalories_per_day": 1600.0, "time": time}],
            "vo2_max": [{"vo2_ml_per_min_per_kg": 40.0, "time": time}],
            "height": [{"meters": 1.8, "time": time}],
        }
    )

    emitted: set[str] = set()
    for payload in payloads:
        for update in parse_payload(payload, tz=_amsterdam()):
            emitted.add(update.key)
            assert update.key in SENSOR_SPECS, f"no spec for {update.key}"

    # And the other way round: a spec nothing can emit is dead weight.
    assert set(SENSOR_SPECS) == emitted, set(SENSOR_SPECS) ^ emitted


def test_text_sensor_has_no_numeric_trappings() -> None:
    """Home Assistant refuses a non-numeric state on a sensor with a unit or class."""
    spec = SENSOR_SPECS["screen_time_top_app"]
    assert spec.unit is None
    assert spec.device_class is None
    assert spec.state_class is None


def test_timestamp_sensors_are_diagnostic() -> None:
    for key in (KEY_LAST_HEALTH_SYNC, KEY_LAST_SCREEN_TIME_SYNC):
        spec = SENSOR_SPECS[key]
        assert spec.diagnostic is True
        assert spec.device_class == "timestamp"
        assert spec.state_class is None
        assert spec.unit is None


def test_not_a_payload_is_harmless() -> None:
    """An empty object yields only the diagnostic sensor, and never raises."""
    updates = parse_payload({}, tz=_amsterdam())
    assert [u.key for u in updates] == [KEY_LAST_HEALTH_SYNC]
