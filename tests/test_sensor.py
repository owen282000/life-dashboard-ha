"""Test the sensor entities: what appears, what it says, what survives a restart."""

import json
from datetime import UTC, datetime
from http import HTTPStatus
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.sensor.const import (
    DEVICE_CLASS_STATE_CLASSES,
    DEVICE_CLASS_UNITS,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.helpers import device_registry as dr
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
    MANUFACTURER,
    MODEL,
    URL_CHOICE_INTERNAL,
)
from custom_components.life_dashboard.payload import (
    SENSOR_SPECS,
    SIGNATURE_HEADER,
    signature_for,
)

WEBHOOK_ID = "a" * 64
SECRET = "b" * 64
URL = f"/api/webhook/{WEBHOOK_ID}"
AMSTERDAM = ZoneInfo("Europe/Amsterdam")


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


async def _post(client, payload: dict) -> int:
    body = json.dumps(payload).encode()
    response = await client.post(
        URL,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            SIGNATURE_HEADER: signature_for(SECRET, body),
        },
    )
    return response.status


def _health(**extra) -> dict:
    payload = {
        "timestamp": "2026-09-15T18:00:00Z",
        "app_version": "1.15.0",
        "source": "health_connect",
    }
    payload.update(extra)
    return payload


# --- Entities appear on data, not before -----------------------------------


async def test_no_entities_before_any_payload(hass: HomeAssistant, loaded) -> None:
    """A fresh pairing shows nothing, which is honest: there is no data yet."""
    assert not hass.states.async_entity_ids("sensor")


async def test_only_the_types_that_arrived(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """Syncing three types gives three sensors plus the diagnostic one."""
    client = await hass_client_no_auth()
    assert (
        await _post(
            client,
            _health(
                daily_totals=[{"date": "2026-09-15", "steps": 8421}],
                heart_rate=[{"bpm": 61, "time": "2026-09-15T17:58:00Z", "uuid": "hr-1"}],
            ),
        )
        == HTTPStatus.OK
    )
    await hass.async_block_till_done()

    ids = set(hass.states.async_entity_ids("sensor"))
    assert ids == {
        "sensor.owen_s_pixel_steps_today",
        "sensor.owen_s_pixel_heart_rate",
        "sensor.owen_s_pixel_last_health_sync",
    }
    # Nothing for a type the phone never sent.
    assert "sensor.owen_s_pixel_weight" not in ids


async def test_a_new_type_adds_a_sensor_later(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """Enabling another data type in the app must not need a reload here."""
    client = await hass_client_no_auth()
    await _post(client, _health(heart_rate=[{"bpm": 61, "time": "2026-09-15T17:58:00Z"}]))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.owen_s_pixel_weight") is None

    await _post(
        client,
        _health(
            timestamp="2026-09-15T19:00:00Z",
            weight=[{"kilograms": 78.4, "time": "2026-09-15T07:10:00Z"}],
        ),
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.owen_s_pixel_weight").state == "78.4"


# --- What a sensor says ----------------------------------------------------


async def test_latest_value_sensor(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    client = await hass_client_no_auth()
    await _post(
        client,
        _health(
            heart_rate=[
                {"bpm": 61, "time": "2026-09-15T17:58:00Z", "uuid": "hr-1", "source": "Fitbit"},
                {"bpm": 70, "time": "2026-09-15T10:00:00Z", "uuid": "hr-0"},
            ]
        ),
    )
    await hass.async_block_till_done()

    state = hass.states.get("sensor.owen_s_pixel_heart_rate")
    assert state.state == "61"
    assert state.attributes["unit_of_measurement"] == "bpm"
    assert state.attributes["state_class"] == SensorStateClass.MEASUREMENT
    assert state.attributes["friendly_name"] == "Owen's Pixel Heart rate"
    assert state.attributes["uuid"] == "hr-1"
    assert state.attributes["source"] == "Fitbit"
    assert state.attributes["measured_at"] == "2026-09-15T17:58:00+00:00"


async def test_day_total_resets_at_local_midnight(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """A day total has to be TOTAL with a last_reset, or statistics mis-sums it."""
    client = await hass_client_no_auth()
    await _post(
        client,
        _health(daily_totals=[{"date": "2026-09-15", "steps": 8421, "distance_meters": 6234.5}]),
    )
    await hass.async_block_till_done()

    state = hass.states.get("sensor.owen_s_pixel_steps_today")
    assert state.state == "8421"
    assert state.attributes["state_class"] == SensorStateClass.TOTAL
    assert state.attributes["date"] == "2026-09-15"
    # Midnight in Amsterdam, which is 22:00 UTC the day before.
    last_reset = datetime.fromisoformat(state.attributes["last_reset"])
    # The same instant, whatever object carries the offset.
    assert last_reset == datetime(2026, 9, 15, 0, 0, tzinfo=AMSTERDAM)
    assert last_reset.astimezone(UTC) == datetime(2026, 9, 14, 22, 0, tzinfo=UTC)

    distance = hass.states.get("sensor.owen_s_pixel_distance_today")
    assert distance.attributes["device_class"] == SensorDeviceClass.DISTANCE
    assert distance.attributes["unit_of_measurement"] == "m"


async def test_sleep_is_presented_in_minutes(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    client = await hass_client_no_auth()
    await _post(
        client,
        _health(sleep=[{"session_end_time": "2026-09-15T06:45:00Z", "duration_seconds": 27300}]),
    )
    await hass.async_block_till_done()

    state = hass.states.get("sensor.owen_s_pixel_last_sleep_duration")
    assert state.state == "455.0"
    assert state.attributes["device_class"] == SensorDeviceClass.DURATION
    assert state.attributes["unit_of_measurement"] == "min"


async def test_screen_time_sensors(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    client = await hass_client_no_auth()
    body = {
        "timestamp": "2026-09-15T18:08:00Z",
        "app_version": "1.15.0",
        "device": "Google Pixel 8",
        "source": "screen_time",
        "screen_time": [
            {
                "date": "2026-09-14",
                "total_screen_time_minutes": 212,
                "apps": [{"package": "com.spotify.music", "name": "Spotify", "minutes": 95}],
            },
            {
                "date": "2026-09-15",
                "total_screen_time_minutes": 143,
                "apps": [
                    {"package": "com.android.chrome", "name": "Chrome", "minutes": 61},
                    {"package": "com.whatsapp", "name": "WhatsApp", "minutes": 44},
                ],
            },
        ],
    }
    assert await _post(client, body) == HTTPStatus.OK
    await hass.async_block_till_done()

    today = hass.states.get("sensor.owen_s_pixel_screen_time_today")
    assert today.state == "143"
    assert today.attributes["state_class"] == SensorStateClass.TOTAL
    assert today.attributes["top_apps"] == "Chrome (61 min), WhatsApp (44 min)"
    assert today.attributes["app_count"] == 2

    yesterday = hass.states.get("sensor.owen_s_pixel_screen_time_yesterday")
    assert yesterday.state == "212"
    assert "state_class" not in yesterday.attributes
    assert "last_reset" not in yesterday.attributes

    top = hass.states.get("sensor.owen_s_pixel_most_used_app_today")
    assert top.state == "Chrome"
    assert top.attributes["package"] == "com.android.chrome"
    # A text sensor must carry none of the numeric trappings, or Home Assistant
    # refuses the state outright.
    assert "unit_of_measurement" not in top.attributes
    assert "state_class" not in top.attributes
    assert "device_class" not in top.attributes


async def test_diagnostic_sensors(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry
) -> None:
    client = await hass_client_no_auth()
    await _post(client, _health(heart_rate=[{"bpm": 61, "time": "2026-09-15T17:58:00Z"}]))
    await hass.async_block_till_done()

    state = hass.states.get("sensor.owen_s_pixel_last_health_sync")
    assert state.attributes["device_class"] == SensorDeviceClass.TIMESTAMP
    assert state.state == "2026-09-15T18:00:00+00:00"
    assert state.attributes["app_version"] == "1.15.0"
    assert state.attributes["record_count"] == 1
    assert state.attributes["backfill"] is False

    registry_entry = entity_registry.async_get("sensor.owen_s_pixel_last_health_sync")
    assert registry_entry.entity_category is EntityCategory.DIAGNOSTIC


async def test_one_device_for_the_phone(
    hass: HomeAssistant,
    hass_client_no_auth,
    loaded,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Every sensor hangs under one device, named after the phone."""
    client = await hass_client_no_auth()
    await _post(
        client,
        _health(
            heart_rate=[{"bpm": 61, "time": "2026-09-15T17:58:00Z"}],
            weight=[{"kilograms": 78.4, "time": "2026-09-15T07:10:00Z"}],
        ),
    )
    await hass.async_block_till_done()

    devices = dr.async_entries_for_config_entry(device_registry, loaded.entry_id)
    assert len(devices) == 1
    device = devices[0]
    assert device.name == "Owen's Pixel"
    assert device.manufacturer == MANUFACTURER
    assert device.model == MODEL
    assert device.sw_version == "1.15.0"

    entities = er.async_entries_for_device(entity_registry, device.id)
    assert {e.entity_id for e in entities} >= {
        "sensor.owen_s_pixel_heart_rate",
        "sensor.owen_s_pixel_weight",
    }


async def test_sw_version_follows_the_app(
    hass: HomeAssistant, hass_client_no_auth, loaded, device_registry: dr.DeviceRegistry
) -> None:
    """Updating the app must show up on the device, not stay on the old version."""
    client = await hass_client_no_auth()
    await _post(client, _health(heart_rate=[{"bpm": 61, "time": "2026-09-15T17:58:00Z"}]))
    await hass.async_block_till_done()

    payload = _health(
        timestamp="2026-09-16T08:00:00Z",
        heart_rate=[{"bpm": 59, "time": "2026-09-16T07:55:00Z"}],
    )
    payload["app_version"] = "1.16.0"
    await _post(client, payload)
    await hass.async_block_till_done()

    device = dr.async_entries_for_config_entry(device_registry, loaded.entry_id)[0]
    assert device.sw_version == "1.16.0"


# --- Across a restart ------------------------------------------------------


async def test_entities_come_back_before_the_phone_does(
    hass: HomeAssistant, entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """After a restart the sensors exist with their old values, unsynced."""
    await async_process_ha_core_config(hass, {"time_zone": "Europe/Amsterdam"})
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_heart_rate",
        suggested_object_id="owen_s_pixel_heart_rate",
        config_entry=entry,
    )
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(
                    "sensor.owen_s_pixel_heart_rate",
                    "61",
                    attributes={
                        "measured_at": "2026-09-15T17:58:00+00:00",
                        "uuid": "hr-1",
                        "friendly_name": "Owen's Pixel Heart rate",
                    },
                ),
                {"native_value": 61, "native_unit_of_measurement": "bpm"},
            ),
        ),
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.owen_s_pixel_heart_rate")
    assert state.state == "61"
    assert state.attributes["uuid"] == "hr-1"
    # Home Assistant's own attribute must not have been restored as payload data.
    assert "friendly_name" in state.attributes
    # And the ordering rule knows where we were.
    held = entry.runtime_data.latest["heart_rate"]
    assert held.measured_at.isoformat() == "2026-09-15T17:58:00+00:00"


async def test_an_old_batch_after_a_restart_is_still_old(
    hass: HomeAssistant,
    hass_client_no_auth,
    entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The restored timestamp is what protects a fresh boot from a backfill."""
    await async_process_ha_core_config(hass, {"time_zone": "Europe/Amsterdam"})
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_heart_rate",
        suggested_object_id="owen_s_pixel_heart_rate",
        config_entry=entry,
    )
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(
                    "sensor.owen_s_pixel_heart_rate",
                    "61",
                    attributes={"measured_at": "2026-09-15T17:58:00+00:00"},
                ),
                {"native_value": 61, "native_unit_of_measurement": "bpm"},
            ),
        ),
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    await _post(
        client,
        _health(backfill=True, heart_rate=[{"bpm": 95, "time": "2026-09-01T10:00:00Z"}]),
    )
    await hass.async_block_till_done()

    assert hass.states.get("sensor.owen_s_pixel_heart_rate").state == "61"

    # A newer reading still gets through.
    await _post(
        client,
        _health(
            timestamp="2026-09-15T19:00:00Z",
            heart_rate=[{"bpm": 58, "time": "2026-09-15T18:30:00Z"}],
        ),
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.owen_s_pixel_heart_rate").state == "58"


async def test_a_restored_value_without_a_timestamp_is_dropped(
    hass: HomeAssistant, entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """Without measured_at there is nothing to order on, so it is not trusted."""
    await async_process_ha_core_config(hass, {"time_zone": "Europe/Amsterdam"})
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_heart_rate",
        suggested_object_id="owen_s_pixel_heart_rate",
        config_entry=entry,
    )
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State("sensor.owen_s_pixel_heart_rate", "61", attributes={}),
                {"native_value": 61, "native_unit_of_measurement": "bpm"},
            ),
        ),
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.owen_s_pixel_heart_rate").state == "unknown"
    assert "heart_rate" not in entry.runtime_data.latest


# --- The table, checked against Home Assistant itself ----------------------


def test_every_unit_is_valid_for_its_device_class() -> None:
    """Home Assistant warns on a mismatch and the entity reads wrong, silently."""
    for key, spec in SENSOR_SPECS.items():
        if not spec.device_class:
            continue
        device_class = SensorDeviceClass(spec.device_class)
        allowed = DEVICE_CLASS_UNITS.get(device_class)
        if allowed is None:
            assert spec.unit is None, f"{key}: {device_class} takes no unit"
            continue
        assert spec.unit in allowed, f"{key}: {spec.unit!r} not valid for {device_class}"


def test_every_state_class_is_valid_for_its_device_class() -> None:
    for key, spec in SENSOR_SPECS.items():
        if not spec.device_class or not spec.state_class:
            continue
        device_class = SensorDeviceClass(spec.device_class)
        state_class = SensorStateClass(spec.state_class)
        allowed = DEVICE_CLASS_STATE_CLASSES.get(device_class, set())
        assert state_class in allowed, f"{key}: {state_class} not valid for {device_class}"


def test_every_sensor_has_a_name() -> None:
    """A missing translation shows up as a blank entity name."""
    with open("custom_components/life_dashboard/strings.json") as handle:
        strings = json.load(handle)
    names = strings["entity"]["sensor"]
    assert set(names) == set(SENSOR_SPECS)
    for key, value in names.items():
        assert value["name"], key


def test_strings_and_translations_match() -> None:
    """A HACS install reads translations/en.json, not strings.json."""
    with open("custom_components/life_dashboard/strings.json") as handle:
        strings = json.load(handle)
    with open("custom_components/life_dashboard/translations/en.json") as handle:
        translations = json.load(handle)
    assert strings == translations
