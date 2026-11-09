"""The last-night sensors, through the webhook and across a restart."""

import json
from datetime import UTC, datetime
from http import HTTPStatus

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.life_dashboard.const import (
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    URL_CHOICE_INTERNAL,
)
from custom_components.life_dashboard.history import Ledger
from custom_components.life_dashboard.payload import SIGNATURE_HEADER, signature_for

WEBHOOK_ID = "a" * 64
SECRET = "b" * 64
URL = f"/api/webhook/{WEBHOOK_ID}"
ENTRY_ID = "01SLEEPENTITIES000000000000"
ASLEEP = "sensor.owen_s_pixel_last_sleep_asleep"
DEEP = "sensor.owen_s_pixel_last_sleep_deep"
DURATION = "sensor.owen_s_pixel_last_sleep_duration"
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
async def entry(hass: HomeAssistant, freezer) -> MockConfigEntry:
    freezer.move_to(NOW)
    await async_process_ha_core_config(hass, {"time_zone": "Europe/Amsterdam"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Owen's Pixel",
        unique_id=WEBHOOK_ID,
        entry_id=ENTRY_ID,
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
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _session(uuid: str, end: str = "2026-11-08T06:00:00Z", hours: int = 7, **extra) -> dict:
    start = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() - hours * 3600
    begin = datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "uuid": uuid,
        "session_end_time": end,
        "duration_seconds": hours * 3600,
        "source": "com.watch",
        "stages": [
            {"stage": "deep", "start_time": begin, "end_time": end},
        ],
        **extra,
    }


def _health(*sleep: dict, timestamp: str = "2026-11-08T12:00:00Z", **extra) -> dict:
    return {
        "timestamp": timestamp,
        "app_version": "1.23.0",
        "source": "health_connect",
        "sleep": list(sleep),
        **extra,
    }


async def _post(hass: HomeAssistant, client, payload: dict) -> None:
    body = json.dumps(payload).encode()
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


async def test_no_stages_no_sensor(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health({**_session("n"), "stages": []}))
    assert hass.states.get(DURATION) is not None
    assert hass.states.get(ASLEEP) is None


async def test_a_night_makes_the_sensors(
    hass: HomeAssistant,
    hass_client_no_auth,
    loaded,
    entity_registry: er.EntityRegistry,
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(_session("n")))

    state = hass.states.get(ASLEEP)
    assert state.state == "7.0"
    assert state.attributes["unit_of_measurement"] == "h"
    assert state.attributes["device_class"] == "duration"
    assert state.attributes["state_class"] == "measurement"
    assert state.attributes["night"] == "2026-11-08"
    assert state.attributes["source"] == "com.watch"
    assert state.attributes["deep_minutes"] == 420.0
    assert state.attributes["stages"] == [
        {
            "stage": "deep",
            "start": "2026-11-08T00:00:00+01:00",
            "end": "2026-11-08T07:00:00+01:00",
        }
    ]

    for key, enabled in (
        ("asleep", True),
        ("deep", False),
        ("light", False),
        ("rem", False),
        ("awake", False),
    ):
        registered = entity_registry.async_get(f"sensor.owen_s_pixel_last_sleep_{key}")
        assert registered.disabled_by is None if enabled else registered.disabled_by is not None
    assert hass.states.get(DEEP) is None


async def test_the_stages_attribute_is_not_recorded(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(_session("n")))
    state = hass.states.get(ASLEEP)
    sensor = hass.data["sensor"].get_entity(ASLEEP)
    assert "stages" in state.attributes
    assert "stages" in sensor._state_info["unrecorded_attributes"]


async def test_late_data_moves_the_sensor_to_the_newer_night_and_a_deletion_back(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    client = await hass_client_no_auth()
    await _post(
        hass,
        client,
        _health(_session("older", end="2026-11-07T06:00:00Z", hours=6)),
    )
    assert hass.states.get(ASLEEP).state == "6.0"

    await _post(hass, client, _health(_session("newer"), timestamp="2026-11-08T12:01:00Z"))
    assert hass.states.get(ASLEEP).state == "7.0"
    assert hass.states.get(ASLEEP).attributes["night"] == "2026-11-08"

    await _post(
        hass,
        client,
        {
            "timestamp": "2026-11-08T12:02:00Z",
            "source": "health_connect",
            "deleted_records": [{"type": "sleep", "uuid": "newer"}],
        },
    )
    state = hass.states.get(ASLEEP)
    assert state.state == "6.0"
    assert state.attributes["night"] == "2026-11-07"


async def test_a_deletion_of_the_last_night_empties_the_sensor(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(_session("n")))
    await _post(
        hass,
        client,
        {
            "timestamp": "2026-11-08T12:02:00Z",
            "source": "health_connect",
            "deleted_records": [{"type": "sleep", "uuid": "n"}],
        },
    )
    assert hass.states.get(ASLEEP).state == "unknown"


async def test_a_restart_reads_the_sensor_and_keeps_the_stages(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(_session("n")))
    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(loaded.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(ASLEEP)
    assert state.state == "7.0"
    assert state.attributes["stages"][0]["stage"] == "deep"


async def test_the_stages_come_back_from_the_restore_cache(
    hass: HomeAssistant,
    hass_client_no_auth,
    entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """A ledger with no night, as after a downgrade: the restored state still reads."""
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_last_sleep_asleep",
        suggested_object_id="owen_s_pixel_last_sleep_asleep",
        config_entry=entry,
    )
    stages = [
        {
            "stage": "deep",
            "start": "2026-11-08T00:00:00+01:00",
            "end": "2026-11-08T07:00:00+01:00",
        }
    ]
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(
                    ASLEEP,
                    "7.0",
                    attributes={
                        "measured_at": "2026-11-08T06:00:00+00:00",
                        "night": "2026-11-08",
                        "source": "com.watch",
                        "deep_minutes": 420.0,
                        "stages": stages,
                        "friendly_name": "Owen's Pixel Last sleep: asleep",
                    },
                ),
                {"native_value": 7.0, "native_unit_of_measurement": "h"},
            ),
        ),
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(ASLEEP)
    assert state.state == "7.0"
    assert state.attributes["night"] == "2026-11-08"
    assert state.attributes["deep_minutes"] == 420.0
    assert state.attributes["stages"] == stages
    assert "friendly_name" in state.attributes


async def test_a_ledger_before_the_stages_is_migrated(
    hass: HomeAssistant, hass_storage, entry: MockConfigEntry
) -> None:
    key = f"{DOMAIN}.{ENTRY_ID}.history"
    old = {
        "days": {"steps": {"2026-10-03": 9000.0}},
        "sessions": {"sleep_minutes": {"night": ["2026-10-03", 450.0]}},
        "hours": {},
        "workouts": {},
        "units": 3,
    }
    hass_storage[key] = {"version": 1, "minor_version": 4, "key": key, "data": old}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    saved = hass_storage[key]
    assert (saved["version"], saved["minor_version"]) == (1, 5)
    assert saved["data"]["sessions"] == old["sessions"]
    assert saved["data"]["sleep"] == {}
    ledger = entry.runtime_data.history.ledger
    assert ledger.sessions == old["sessions"]
    assert not any(ledger.sleep.values())


async def test_a_release_before_the_stages_still_reads_the_file(
    hass: HomeAssistant, hass_storage, hass_client_no_auth, loaded
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(_session("n")))
    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()
    saved = hass_storage[f"{DOMAIN}.{ENTRY_ID}.history"]
    # The sessions an older release reads are what they always were.
    assert saved["data"]["sessions"]["sleep_minutes"]["n"] == ["2026-11-08", 420.0]
    Ledger.from_dict({key: value for key, value in saved["data"].items() if key != "sleep"})
    assert "n" in saved["data"]["sleep"]["nights"]


async def test_translations_stay_in_step() -> None:
    with open("custom_components/life_dashboard/strings.json") as handle:
        strings = json.load(handle)
    with open("custom_components/life_dashboard/translations/en.json") as handle:
        english = json.load(handle)
    with open("custom_components/life_dashboard/translations/de.json") as handle:
        german = json.load(handle)
    with open("custom_components/life_dashboard/icons.json") as handle:
        icons = json.load(handle)
    assert strings == english
    keys = [
        "last_sleep_asleep",
        "last_sleep_deep",
        "last_sleep_light",
        "last_sleep_rem",
        "last_sleep_awake",
    ]
    for key in keys:
        assert strings["entity"]["sensor"][key]["name"]
        assert german["entity"]["sensor"][key]["name"]
        assert icons["entity"]["sensor"][key]["default"].startswith("mdi:")
