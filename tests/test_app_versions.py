"""Payloads as older versions of the phone apps send them, end to end through the webhook.

Each scenario is built the way that app version builds its payload, and says where in
the Android app's history (git tags of life-dashboard-companion-app) or the iOS app's
the shape comes from. They pin two things:

- The sensors that existed before the per-app ones read exactly what they read on the
  main branch for the same payloads. The expected values in
  fixtures/app_versions_on_main.json were taken by running these scenarios against main
  (0.8.0) and are compared without the per-app sensors, so this file runs unchanged on
  main as well.
- The per-app sensors come only from rows that name a package, get a real name, and
  none come from a payload without apps: no "None screen time", no package "null".

The shape of the Screen Time payload over the Android versions:

- 1.0.0: `timestamp`, `app_version`, `device`, `source`, and `screen_time`: the last
  seven days, newest first, each with `date`, `total_screen_time_minutes` and `apps`
  (`package`, `name`, `minutes`, `last_used`). A day without an app over a minute is
  left out. An app the phone cannot look up is named after the last segment of its
  package (ScreenTimeManager.getAppName). Signing came in 1.4.0, the oldest version
  that can talk to this integration at all.
- 1.13.2: that stand-in skips generic segments (ScreenTimeManager.fallbackAppName), so
  org.wakingup.android is "wakingup" rather than "android".
- 1.17.0: the oldest version the README supports; the payload is the 1.13.2 one.
- 1.21.0: `sequence`, and time in the Settings app counts as screen time.
- 1.8.0 and later keep a failed week in an outbox (written ahead of the post from
  1.22.0 on), so it can arrive after a newer one.
- 1.23.0: `app_filter` with a filter on, and per day `filtered_screen_time_minutes`;
  a day whose apps were all filtered out still comes, with no apps.
"""

import json
import math
from datetime import date, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.life_dashboard.const import (
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    URL_CHOICE_INTERNAL,
)
from custom_components.life_dashboard.payload import SIGNATURE_HEADER, signature_for

WEBHOOK_ID = "a" * 64
SECRET = "b" * 64
URL = f"/api/webhook/{WEBHOOK_ID}"
ON_MAIN = Path(__file__).parent / "fixtures" / "app_versions_on_main.json"

CHROME = "com.android.chrome"
WHATSAPP = "com.whatsapp"
SPOTIFY = "com.spotify.music"
YOUTUBE = "com.google.android.youtube"
SETTINGS = "com.android.settings"
RARE = "com.example.rare"
# Uninstalled since, so every version sends it under a name made from its package.
WAKINGUP = "org.wakingup.android"

LABELS = {
    CHROME: "Chrome",
    WHATSAPP: "WhatsApp",
    SPOTIFY: "Spotify",
    YOUTUBE: "YouTube",
    SETTINGS: "Settings",
    RARE: "Rare",
}

TODAY = date(2026, 9, 15)

# Foreground seconds per app, by days before TODAY.
USAGE: dict[int, list[tuple[str, int]]] = {
    0: [(CHROME, 3725), (WHATSAPP, 2690), (YOUTUBE, 1510), (SPOTIFY, 1330)],
    1: [(SPOTIFY, 5712), (CHROME, 2405), (WAKINGUP, 1260)],
    2: [(YOUTUBE, 4100), (WHATSAPP, 1999), (RARE, 125)],
    3: [(CHROME, 1830), (WAKINGUP, 1225)],
    4: [(WHATSAPP, 3100), (SPOTIFY, 900)],
    5: [(CHROME, 2222)],
    6: [(YOUTUBE, 3601), (WAKINGUP, 615)],
}


def _version(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _label(package: str, version: str) -> str:
    """The name the app sends: the label, or its stand-in for an app it cannot see."""
    if package in LABELS:
        return LABELS[package]
    if _version(version) < (1, 13, 2):
        return package.rsplit(".", 1)[-1]
    generic = {"android", "app", "apps", "mobile", "client", "main", "release", "prod"}
    generic |= {"free", "pro", "lite"}
    segments = [segment for segment in package.split(".") if segment.strip()][1:]
    return next((s for s in reversed(segments) if s.lower() not in generic), package)


def _day(
    version: str,
    day: date,
    usage: list[tuple[str, int]],
    *,
    keep: set[str] | None = None,
) -> dict[str, Any] | None:
    """One day as ScreenTimeSyncManager.buildJsonPayload writes it, or None when the
    app leaves it out (no app over a minute). keep is the app filter of 1.23.0."""
    usage = sorted(((p, s) for p, s in usage if s > 60), key=lambda item: -item[1])
    if not usage:
        return None
    entry: dict[str, Any] = {
        "date": day.isoformat(),
        # The sum of the milliseconds, divided once: not the sum of the rounded minutes.
        "total_screen_time_minutes": sum(seconds for _, seconds in usage) // 60,
    }
    if keep is not None:
        usage = [(p, s) for p, s in usage if p in keep]
        entry["filtered_screen_time_minutes"] = sum(seconds for _, seconds in usage) // 60
    entry["apps"] = [
        {
            "package": package,
            "name": _label(package, version),
            "minutes": seconds // 60,
            "last_used": f"{day.isoformat()}T17:{index:02d}:10.123Z",
        }
        for index, (package, seconds) in enumerate(usage)
    ]
    return entry


def _screen_time(
    version: str,
    *,
    timestamp: str = "2026-09-15T18:00:00Z",
    today: date = TODAY,
    usage: dict[int, list[tuple[str, int]]] | None = None,
    sequence: int | None = None,
    app_filter: tuple[str, set[str]] | None = None,
) -> dict[str, Any]:
    """A Screen Time payload of one Android version: the last seven days, newest first."""
    usage = USAGE if usage is None else usage
    payload: dict[str, Any] = {
        "timestamp": timestamp,
        "app_version": version,
        "device": "samsung SM-S908B",
        "source": "screen_time",
    }
    if sequence is not None:
        payload["sequence"] = sequence
    keep = None
    if app_filter is not None:
        mode, packages = app_filter
        payload["app_filter"] = mode
        everything = {package for day in usage.values() for package, _ in day}
        keep = packages if mode == "allowlist" else everything - packages
    days = []
    for offset in range(7):
        entry = _day(version, today - timedelta(days=offset), usage.get(offset, []), keep=keep)
        if entry is not None:
            days.append(entry)
    payload["screen_time"] = days
    return payload


def _shifted(days: int, extra: dict[int, list[tuple[str, int]]] | None = None) -> dict:
    """USAGE seen from a later day: what was today is `days` days ago."""
    usage = {offset + days: apps for offset, apps in USAGE.items() if offset + days < 7}
    for offset, apps in (extra or {}).items():
        # One row per package and day, as the app's map of foreground time has it.
        seconds = dict(usage.get(offset, []))
        for package, more in apps:
            seconds[package] = seconds.get(package, 0) + more
        usage[offset] = list(seconds.items())
    return usage


def _with_settings() -> dict[int, list[tuple[str, int]]]:
    """1.21.0 counts time in the Settings app."""
    usage = dict(USAGE)
    usage[0] = [*USAGE[0], (SETTINGS, 400)]
    return usage


def _health_connect() -> dict[str, Any]:
    """A Health Connect sync of 1.17.0, the fields of docs/webhook.md at that tag."""
    return {
        "timestamp": "2026-09-15T18:00:00Z",
        "app_version": "1.17.0",
        "source": "health_connect",
        "steps": [
            {
                "count": 1234,
                "start_time": "2026-09-15T08:00:00Z",
                "end_time": "2026-09-15T09:00:00Z",
                "uuid": "steps-1",
                "source": "com.sec.android.app.shealth",
            }
        ],
        "heart_rate": [
            {"bpm": 72, "time": "2026-09-15T10:30:00Z", "uuid": "hr-1", "source": "com.zepp.app"}
        ],
        "weight": [{"kilograms": 75.5, "time": "2026-09-15T07:00:00Z", "uuid": "w-1"}],
        "sleep": [
            {
                "uuid": "sleep-1",
                "session_end_time": "2026-09-15T05:30:00Z",
                "duration_seconds": 27000,
                "stages": [
                    {
                        "stage": "STAGE_TYPE_DEEP",
                        "start_time": "2026-09-14T23:00:00Z",
                        "end_time": "2026-09-15T01:00:00Z",
                        "duration_seconds": 7200,
                    }
                ],
            }
        ],
        "daily_totals": [{"date": "2026-09-15", "steps": 4212, "distance_meters": 3150.25}],
    }


def _iphone() -> dict[str, Any]:
    """A HealthKit sync of the iOS app 1.4.0 (HealthSyncManager.swift): no screen time."""
    return {
        "app_version": "1.4.0",
        "daily_totals": [
            {"date": "2026-09-14", "steps": 8100},
            {"date": "2026-09-15", "steps": 4212, "distance_meters": 3150.25},
        ],
        "heart_rate": [{"bpm": 61, "time": "2026-09-15T12:00:00Z", "source": "Apple Watch"}],
        "source": "healthkit_ios",
        "timestamp": "2026-09-15T16:55:02Z",
    }


def _mutated(mutate) -> list[dict[str, Any]]:
    """A 1.23.0 week with something no app version sends, for the parser's sake."""
    payload = _screen_time("1.23.0", sequence=5001)
    mutate(payload)
    return [payload]


def _set_app(day: int, app: int, **fields):
    def mutate(payload: dict[str, Any]) -> None:
        payload["screen_time"][day]["apps"][app].update(fields)

    return mutate


def _drop_app_field(day: int, app: int, field: str):
    def mutate(payload: dict[str, Any]) -> None:
        del payload["screen_time"][day]["apps"][app][field]

    return mutate


def _set_day(day: int, **fields):
    def mutate(payload: dict[str, Any]) -> None:
        payload["screen_time"][day].update(fields)

    return mutate


def _set_top(**fields):
    def mutate(payload: dict[str, Any]) -> None:
        payload.update(fields)

    return mutate


SCENARIOS: dict[str, list[dict[str, Any]]] = {
    # 1.0.0 to 1.20.0 build the same payload; 1.4.0 is the first that signs it.
    "android_1_4_0": [_screen_time("1.4.0")],
    "android_1_13_2_fallback_names": [_screen_time("1.13.2")],
    # Just after midnight a new day has no app over a minute yet, so the app leaves it
    # out and the newest day sent is still yesterday's, now with the evening in it.
    "android_1_17_0_past_midnight": [
        _screen_time("1.17.0"),
        _screen_time(
            "1.17.0",
            timestamp="2026-09-15T22:20:00Z",
            today=TODAY + timedelta(days=1),
            usage=_shifted(1, {1: [(CHROME, 600)]}),
        ),
    ],
    "android_1_17_0_test_ping": [
        {
            "test": True,
            "message": "Test ping from Life Dashboard Companion",
            "timestamp": "2026-09-15T18:00:00Z",
            "source": "screen_time",
        }
    ],
    "android_1_21_0_sequence_settings": [
        _screen_time("1.21.0", sequence=4182, usage=_with_settings())
    ],
    # The newer week arrives first, the week that waited in the outbox after it.
    "android_1_22_0_late_outbox_week": [
        _screen_time(
            "1.22.0",
            timestamp="2026-09-16T07:00:00Z",
            today=TODAY + timedelta(days=1),
            usage=_shifted(1, {0: [(WHATSAPP, 900)]}),
            sequence=4190,
        ),
        _screen_time("1.22.0", sequence=4185),
    ],
    "android_1_23_0_blocklist": [
        _screen_time("1.23.0", sequence=4300, app_filter=("blocklist", {WHATSAPP}))
    ],
    # Spotify only, and Spotify not used yet today: today comes with no apps at all.
    "android_1_23_0_allowlist_nothing_today": [
        _screen_time(
            "1.23.0",
            sequence=4301,
            usage={**USAGE, 0: [p for p in USAGE[0] if p[0] != SPOTIFY]},
            app_filter=("allowlist", {SPOTIFY}),
        )
    ],
    "android_1_17_0_health_connect": [_health_connect()],
    "ios_1_4_0_healthkit": [_iphone()],
    # What no version sends: the parser has to survive it, and make nothing up.
    "malformed_app_without_name": [*_mutated(_drop_app_field(0, 0, "name"))],
    "malformed_app_name_null": [*_mutated(_set_app(0, 1, name=None))],
    "malformed_app_name_empty": [*_mutated(_set_app(0, 2, name=""))],
    "malformed_app_name_blank": [*_mutated(_set_app(1, 0, name="   "))],
    "malformed_app_name_number": [*_mutated(_set_app(0, 0, name=42))],
    "malformed_only_row_without_a_name": [
        *_mutated(
            _set_day(0, apps=[{"package": "com.example.nameless", "name": "", "minutes": 30}])
        )
    ],
    "malformed_app_without_minutes": [*_mutated(_drop_app_field(0, 0, "minutes"))],
    "malformed_app_minutes_null": [*_mutated(_set_app(0, 1, minutes=None))],
    "malformed_app_minutes_string": [*_mutated(_set_app(0, 1, minutes="44"))],
    "malformed_app_minutes_bool": [*_mutated(_set_app(0, 1, minutes=True))],
    "malformed_app_minutes_float": [*_mutated(_set_app(0, 1, minutes=44.7))],
    "malformed_app_minutes_not_finite": [*_mutated(_set_app(3, 0, minutes=math.inf))],
    "malformed_app_minutes_nan": [*_mutated(_set_app(2, 1, minutes=math.nan))],
    "malformed_app_without_package": [*_mutated(_drop_app_field(0, 0, "package"))],
    "malformed_app_package_null": [*_mutated(_set_app(0, 0, package=None))],
    "malformed_app_package_empty": [*_mutated(_set_app(0, 0, package=""))],
    "malformed_app_package_number": [*_mutated(_set_app(0, 0, package=7))],
    "malformed_app_not_an_object": [*_mutated(_set_day(0, apps=["com.android.chrome", None, 3]))],
    "malformed_apps_null": [*_mutated(_set_day(0, apps=None))],
    "malformed_apps_as_object": [*_mutated(_set_day(0, apps={CHROME: 62, WHATSAPP: 44}))],
    "malformed_apps_as_string": [*_mutated(_set_day(0, apps="Chrome, WhatsApp"))],
    "malformed_day_total_null": [*_mutated(_set_day(0, total_screen_time_minutes=None))],
    "malformed_day_filtered_null": [*_mutated(_set_day(0, filtered_screen_time_minutes=None))],
    "malformed_day_date_null": [*_mutated(_set_day(0, date=None))],
    "malformed_day_date_garbage": [*_mutated(_set_day(1, date="yesterday"))],
    "malformed_screen_time_null": [*_mutated(_set_top(screen_time=None))],
    "malformed_screen_time_as_object": [*_mutated(_set_top(screen_time={"date": "2026-09-15"}))],
    "malformed_screen_time_empty": [*_mutated(_set_top(screen_time=[]))],
    "malformed_screen_time_days_not_objects": [*_mutated(_set_top(screen_time=["x", 1, None]))],
}


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Owen's Pixel",
        unique_id=WEBHOOK_ID,
        data={
            CONF_WEBHOOK_ID: WEBHOOK_ID,
            CONF_SECRET: SECRET,
            CONF_URL_CHOICE: URL_CHOICE_INTERNAL,
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def loaded(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    await async_process_ha_core_config(hass, {"time_zone": "Europe/Amsterdam"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _deliver(hass: HomeAssistant, client, payloads: list[dict[str, Any]]) -> None:
    """Post each payload as the app does, and expect each to be taken."""
    for payload in payloads:
        # Compact, as kotlinx.serialization writes it; NaN and Infinity only where a
        # scenario put them in on purpose.
        body = json.dumps(payload, separators=(",", ":")).encode()
        response = await client.post(
            URL,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                SIGNATURE_HEADER: signature_for(SECRET, body),
            },
        )
        assert response.status == HTTPStatus.OK
        await hass.async_block_till_done()


def _app_prefix(entry: MockConfigEntry) -> str:
    return f"{entry.entry_id}_screen_time_app_"


# Set by the entity description, the same for every payload.
_STATIC_ATTRIBUTES = {"friendly_name", "unit_of_measurement", "device_class", "state_class", "icon"}


def _existing_sensors(
    hass: HomeAssistant, registry: er.EntityRegistry, entry: MockConfigEntry
) -> dict[str, Any]:
    """Every sensor but the per-app ones, by key: the state and what it carries, and
    the days the history ledger holds."""
    sensors = {}
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.unique_id.startswith(_app_prefix(entry)):
            continue
        key = registry_entry.unique_id.removeprefix(f"{entry.entry_id}_")
        state = hass.states.get(registry_entry.entity_id)
        sensors[key] = {
            "entity_id": registry_entry.entity_id,
            "state": state.state if state else None,
            "attributes": {
                name: value
                for name, value in (state.attributes.items() if state else ())
                if name not in _STATIC_ATTRIBUTES
            },
        }
    ledger = entry.runtime_data.history.ledger.to_dict()
    # Through JSON, so a tuple and a list compare alike, as they do in the fixture file.
    return json.loads(json.dumps({"sensors": sensors, "ledger": ledger}, default=str))


def _app_sensors(
    registry: er.EntityRegistry, entry: MockConfigEntry
) -> dict[str, er.RegistryEntry]:
    prefix = _app_prefix(entry)
    return {
        registry_entry.unique_id.removeprefix(prefix): registry_entry
        for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id)
        if registry_entry.unique_id.startswith(prefix)
    }


def _on_main() -> dict[str, Any]:
    return json.loads(ON_MAIN.read_text())


def test_every_scenario_has_its_main_reading() -> None:
    assert set(_on_main()) == set(SCENARIOS)


@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_existing_sensors_read_as_on_main(
    hass: HomeAssistant,
    hass_client_no_auth,
    loaded,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
    scenario: str,
) -> None:
    """Taken with a 200, nothing logged as an error, every older sensor as on main."""
    await _deliver(hass, await hass_client_no_auth(), SCENARIOS[scenario])

    assert [r.getMessage() for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")] == []
    assert _existing_sensors(hass, entity_registry, loaded) == _on_main()[scenario]


# Per package: the name of its sensor, then what it reads once enabled: today's
# minutes, the week's, and the date. Worked out by hand from USAGE: whole minutes per
# app and day, as the app rounds them, and Rare stays under the five minutes a week
# that earn a sensor.
_WEEK = {
    CHROME: ("Chrome", 62, 169),
    WHATSAPP: ("WhatsApp", 44, 128),
    YOUTUBE: ("YouTube", 25, 153),
    SPOTIFY: ("Spotify", 22, 132),
    WAKINGUP: ("wakingup", 0, 51),
}


def _expect(day: str, **changes: tuple[str, int, int] | None) -> dict[str, tuple]:
    apps = {package: (*row, day) for package, row in _WEEK.items()}
    names = {"chrome": CHROME, "whatsapp": WHATSAPP, "youtube": YOUTUBE}
    names |= {"spotify": SPOTIFY, "wakingup": WAKINGUP, "settings": SETTINGS}
    for name, row in changes.items():
        if row is None:
            del apps[names[name]]
        else:
            apps[names[name]] = (*row, day)
    return apps


APP_SENSORS: dict[str, dict[str, tuple]] = {
    # The stand-in "android" of before 1.13.2 is named as the app names it now.
    "android_1_4_0": _expect("2026-09-15"),
    "android_1_13_2_fallback_names": _expect("2026-09-15"),
    # Still 2026-09-15, with the evening in it, and the oldest day gone from the week.
    "android_1_17_0_past_midnight": _expect(
        "2026-09-15",
        chrome=("Chrome", 72, 179),
        youtube=("YouTube", 25, 93),
        wakingup=("wakingup", 0, 41),
    ),
    "android_1_17_0_test_ping": {},
    "android_1_21_0_sequence_settings": _expect("2026-09-15", settings=("Settings", 6, 6)),
    # The late week is older than the table held, so it changes nothing.
    "android_1_22_0_late_outbox_week": _expect(
        "2026-09-16",
        chrome=("Chrome", 0, 169),
        whatsapp=("WhatsApp", 15, 143),
        youtube=("YouTube", 0, 93),
        spotify=("Spotify", 0, 132),
        wakingup=("wakingup", 0, 41),
    ),
    "android_1_23_0_blocklist": _expect("2026-09-15", whatsapp=None),
    "android_1_23_0_allowlist_nothing_today": {SPOTIFY: ("Spotify", 0, 110, "2026-09-15")},
    "android_1_17_0_health_connect": {},
    "ios_1_4_0_healthkit": {},
}

# Payloads with no usable app at all: no app sensor may come from them.
_NO_APPS = {
    "malformed_screen_time_null",
    "malformed_screen_time_as_object",
    "malformed_screen_time_empty",
    "malformed_screen_time_days_not_objects",
}

# Names that would mean a missing value was turned into text somewhere.
_JUNK = {"", "none", "null", "nan", "inf", "infinity", "unknown", "undefined"}


def _assert_no_junk(registry: er.EntityRegistry, entry: MockConfigEntry) -> None:
    for package, registry_entry in _app_sensors(registry, entry).items():
        assert package.strip() == package
        assert package.lower() not in _JUNK
        assert "." in package, package
        name = registry_entry.original_name
        assert name.endswith(" screen time"), name
        label = name.removesuffix(" screen time")
        assert label.strip() == label
        assert label.lower() not in _JUNK, name
        for junk in ("_none_", "_null_", "_nan_"):
            assert junk not in registry_entry.entity_id, registry_entry.entity_id


@pytest.mark.parametrize("scenario", list(APP_SENSORS))
async def test_app_sensors_of_each_version(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry, scenario
) -> None:
    """One disabled sensor per app with a package, named after the app; enabled, it
    reads the newest day the phone sent."""
    await _deliver(hass, await hass_client_no_auth(), SCENARIOS[scenario])
    expected = APP_SENSORS[scenario]

    sensors = _app_sensors(entity_registry, loaded)
    assert {
        package: registry_entry.original_name for package, registry_entry in sensors.items()
    } == {package: f"{row[0]} screen time" for package, row in expected.items()}
    assert all(r.disabled_by is er.RegistryEntryDisabler.INTEGRATION for r in sensors.values())
    _assert_no_junk(entity_registry, loaded)
    if not expected:
        return

    for registry_entry in sensors.values():
        entity_registry.async_update_entity(registry_entry.entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done()

    reads = {}
    for package, registry_entry in _app_sensors(entity_registry, loaded).items():
        state = hass.states.get(registry_entry.entity_id)
        assert state.attributes["package"] == package
        reads[package] = (
            state.attributes["app"],
            int(state.state),
            state.attributes["week_minutes"],
            state.attributes["date"],
        )
    assert reads == expected


@pytest.mark.parametrize("scenario", [name for name in SCENARIOS if name.startswith("malformed")])
async def test_a_malformed_payload_makes_no_junk_app_sensor(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry, scenario
) -> None:
    """Every app sensor comes from a row with a package; none at all from no rows."""
    await _deliver(hass, await hass_client_no_auth(), SCENARIOS[scenario])

    sensors = _app_sensors(entity_registry, loaded)
    _assert_no_junk(entity_registry, loaded)
    clean = set(_WEEK)
    if scenario in _NO_APPS:
        assert sensors == {}
    elif scenario == "malformed_only_row_without_a_name":
        # Named after its package, the one thing known about it.
        assert sensors["com.example.nameless"].original_name == ("com.example.nameless screen time")
        assert set(sensors) - {"com.example.nameless"} <= clean
    else:
        # A broken row of today drops that row only; the app's other days still count.
        assert set(sensors) == clean
