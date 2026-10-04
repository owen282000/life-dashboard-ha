"""Test the per-app screen time sensors: created disabled, one per package, never stale."""

import json
from http import HTTPStatus
from unittest.mock import patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.life_dashboard.apps import MAX_APP_SENSORS, MIN_WEEK_MINUTES
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

CHROME = "com.android.chrome"
WHATSAPP = "com.whatsapp"
SPOTIFY = "com.spotify.music"


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


def _day(date: str, *apps: tuple[str, str, int]) -> dict:
    return {
        "date": date,
        "total_screen_time_minutes": sum(minutes for _, _, minutes in apps),
        "apps": [
            {"package": package, "name": name, "minutes": minutes}
            for package, name, minutes in apps
        ],
    }


def _screen_time(*days: dict, **extra) -> dict:
    payload = {
        "timestamp": "2026-09-15T18:00:00Z",
        "app_version": "1.23.0",
        "device": "Google Pixel 8",
        "source": "screen_time",
        "screen_time": list(days),
    }
    payload.update(extra)
    return payload


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


def _app_entries(
    registry: er.EntityRegistry, entry: MockConfigEntry
) -> dict[str, er.RegistryEntry]:
    """The per-app registry entries of an entry, by package."""
    prefix = f"{entry.entry_id}_screen_time_app_"
    return {
        registry_entry.unique_id.removeprefix(prefix): registry_entry
        for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id)
        if registry_entry.unique_id.startswith(prefix)
    }


async def _enable(
    hass: HomeAssistant, registry: er.EntityRegistry, entry: MockConfigEntry, *packages: str
) -> None:
    """What the user does in the entity settings, without waiting for the delayed reload."""
    entries = _app_entries(registry, entry)
    for package in packages:
        registry.async_update_entity(entries[package].entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


TWO_DAYS = (
    _day("2026-09-14", (SPOTIFY, "Spotify", 95)),
    _day(
        "2026-09-15", (CHROME, "Chrome", 61), (WHATSAPP, "WhatsApp", 44), (SPOTIFY, "Spotify", 22)
    ),
)


# --- Created on the first payload, disabled --------------------------------


async def test_every_app_of_the_week_gets_a_disabled_sensor(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _screen_time(*TWO_DAYS))

    entries = _app_entries(entity_registry, loaded)
    assert set(entries) == {CHROME, WHATSAPP, SPOTIFY}
    for registry_entry in entries.values():
        assert registry_entry.disabled_by is RegistryEntryDisabler.INTEGRATION
        assert registry_entry.translation_key == "app_screen_time"
        # Disabled means never added: nothing on the state machine.
        assert hass.states.get(registry_entry.entity_id) is None
    assert entries[CHROME].entity_id == "sensor.owen_s_pixel_chrome_screen_time"
    assert entries[CHROME].original_name == "Chrome screen time"
    assert entries[CHROME].original_icon is None  # From icons.json, by translation key.

    # The device page is what it was: the aggregate sensors are there, enabled.
    assert hass.states.get("sensor.owen_s_pixel_screen_time_today").state == "127"


async def test_an_enabled_sensor_reads_today_straight_after_the_reload(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry
) -> None:
    """Enabling reloads the entry; the stored table fills it before the next sync."""
    client = await hass_client_no_auth()
    await _post(hass, client, _screen_time(*TWO_DAYS))
    await _enable(hass, entity_registry, loaded, CHROME, SPOTIFY)

    chrome = hass.states.get("sensor.owen_s_pixel_chrome_screen_time")
    assert chrome.state == "61"
    assert chrome.attributes["unit_of_measurement"] == "min"
    assert chrome.attributes["device_class"] == SensorDeviceClass.DURATION
    # Like Screen time today: no state class, so no long-term statistics per app.
    assert "state_class" not in chrome.attributes
    assert chrome.attributes["friendly_name"] == "Owen's Pixel Chrome screen time"
    assert chrome.attributes["app"] == "Chrome"
    assert chrome.attributes["package"] == CHROME
    assert chrome.attributes["date"] == "2026-09-15"
    assert chrome.attributes["week_minutes"] == 61

    spotify = hass.states.get("sensor.owen_s_pixel_spotify_screen_time")
    assert spotify.state == "22"
    assert spotify.attributes["week_minutes"] == 117

    # The one left disabled stays off the state machine.
    assert hass.states.get("sensor.owen_s_pixel_whatsapp_screen_time") is None


# --- Later payloads --------------------------------------------------------


async def test_a_new_app_gets_a_sensor_without_a_reload(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _screen_time(_day("2026-09-15", (CHROME, "Chrome", 61))))
    assert set(_app_entries(entity_registry, loaded)) == {CHROME}

    await _post(
        hass,
        client,
        _screen_time(
            _day("2026-09-15", (CHROME, "Chrome", 70), ("org.mozilla.firefox", "Firefox", 12)),
            timestamp="2026-09-15T19:00:00Z",
        ),
    )
    entries = _app_entries(entity_registry, loaded)
    assert set(entries) == {CHROME, "org.mozilla.firefox"}
    assert entries["org.mozilla.firefox"].disabled_by is RegistryEntryDisabler.INTEGRATION


async def test_a_filtered_app_gets_no_sensor_and_reads_zero_once_filtered(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry
) -> None:
    """An app left out in the app never arrives: no sensor, and no stale value."""
    client = await hass_client_no_auth()
    filtered = _day("2026-09-15", (CHROME, "Chrome", 61), (SPOTIFY, "Spotify", 22))
    filtered["filtered_screen_time_minutes"] = 83
    await _post(hass, client, _screen_time(filtered, app_filter="blocklist"))
    assert set(_app_entries(entity_registry, loaded)) == {CHROME, SPOTIFY}
    await _enable(hass, entity_registry, loaded, SPOTIFY)
    assert hass.states.get("sensor.owen_s_pixel_spotify_screen_time").state == "22"

    # Spotify is now left out too: the same day comes again without it.
    narrower = _day("2026-09-15", (CHROME, "Chrome", 65))
    narrower["filtered_screen_time_minutes"] = 65
    await _post(
        hass,
        client,
        _screen_time(narrower, app_filter="blocklist", timestamp="2026-09-15T19:00:00Z"),
    )
    spotify = hass.states.get("sensor.owen_s_pixel_spotify_screen_time")
    assert spotify.state == "0"
    assert spotify.attributes["week_minutes"] == 0
    # It keeps its name while it is gone from the payload.
    assert spotify.attributes["app"] == "Spotify"
    # And nothing for WhatsApp, which never arrived at all.
    assert WHATSAPP not in _app_entries(entity_registry, loaded)


async def test_the_next_day_starts_at_zero(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry
) -> None:
    """The week is sent again with a new day; an app not used yet today reads 0."""
    client = await hass_client_no_auth()
    await _post(hass, client, _screen_time(*TWO_DAYS))
    await _enable(hass, entity_registry, loaded, CHROME)
    assert hass.states.get("sensor.owen_s_pixel_chrome_screen_time").state == "61"

    await _post(
        hass,
        client,
        _screen_time(
            *TWO_DAYS,
            _day("2026-09-16", (WHATSAPP, "WhatsApp", 3)),
            timestamp="2026-09-16T06:30:00Z",
        ),
    )
    chrome = hass.states.get("sensor.owen_s_pixel_chrome_screen_time")
    assert chrome.state == "0"
    assert chrome.attributes["date"] == "2026-09-16"
    assert chrome.attributes["week_minutes"] == 61

    # A week that waited in the outbox and arrives late does not bring yesterday back.
    await _post(hass, client, _screen_time(*TWO_DAYS, timestamp="2026-09-15T20:00:00Z"))
    assert hass.states.get("sensor.owen_s_pixel_chrome_screen_time").state == "0"


async def test_a_renamed_app_keeps_its_sensor(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _screen_time(_day("2026-09-15", (SPOTIFY, "Spotify", 22))))
    await _enable(hass, entity_registry, loaded, SPOTIFY)
    before = _app_entries(entity_registry, loaded)[SPOTIFY]

    await _post(
        hass,
        client,
        _screen_time(
            _day("2026-09-15", (SPOTIFY, "Spotify: Music and Podcasts", 30)),
            timestamp="2026-09-15T19:00:00Z",
        ),
    )
    entries = _app_entries(entity_registry, loaded)
    assert set(entries) == {SPOTIFY}
    assert entries[SPOTIFY].entity_id == before.entity_id
    state = hass.states.get(before.entity_id)
    assert state.state == "30"
    assert state.attributes["app"] == "Spotify: Music and Podcasts"

    # Once rebuilt, the name follows too; the entity id stays what automations use.
    assert await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(before.entity_id)
    assert (
        state.attributes["friendly_name"] == "Owen's Pixel Spotify: Music and Podcasts screen time"
    )
    assert entity_registry.async_get(before.entity_id).original_name == (
        "Spotify: Music and Podcasts screen time"
    )


async def test_an_uninstalled_app_keeps_its_real_name(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry
) -> None:
    """The phone names an app it can no longer look up after its package; that is no
    rename."""
    client = await hass_client_no_auth()
    await _post(hass, client, _screen_time(_day("2026-09-15", (SPOTIFY, "Spotify", 22))))
    await _enable(hass, entity_registry, loaded, SPOTIFY)

    await _post(
        hass,
        client,
        _screen_time(
            _day("2026-09-16", (SPOTIFY, "music", 3)),
            timestamp="2026-09-16T08:00:00Z",
        ),
    )
    state = hass.states.get("sensor.owen_s_pixel_spotify_screen_time")
    assert state.state == "3"
    assert state.attributes["app"] == "Spotify"
    assert loaded.runtime_data.apps.labels[SPOTIFY] == "Spotify"


# --- Bounds ----------------------------------------------------------------


async def test_an_app_barely_used_gets_no_sensor(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry
) -> None:
    """Under the threshold over the whole week; the minutes of several days add up."""
    client = await hass_client_no_auth()
    below = MIN_WEEK_MINUTES - 1
    await _post(
        hass,
        client,
        _screen_time(
            _day("2026-09-14", ("com.example.often", "Often", 2)),
            _day(
                "2026-09-15",
                ("com.example.often", "Often", MIN_WEEK_MINUTES - 2),
                ("com.example.once", "Once", below),
            ),
        ),
    )
    assert set(_app_entries(entity_registry, loaded)) == {"com.example.often"}


async def test_the_number_of_app_sensors_is_capped(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry
) -> None:
    """The most used apps get the room there is; the rest get none, also later."""
    client = await hass_client_no_auth()
    apps = [(f"com.example.app{n}", f"App {n}", 10 + n) for n in range(MAX_APP_SENSORS + 10)]
    await _post(hass, client, _screen_time(_day("2026-09-15", *apps)))

    entries = _app_entries(entity_registry, loaded)
    assert len(entries) == MAX_APP_SENSORS
    # The ten least used are the ones without.
    assert "com.example.app0" not in entries
    assert "com.example.app9" not in entries
    assert f"com.example.app{MAX_APP_SENSORS + 9}" in entries

    await _post(
        hass,
        client,
        _screen_time(
            _day("2026-09-15", *apps, ("com.example.new", "New", 500)),
            timestamp="2026-09-15T19:00:00Z",
        ),
    )
    assert len(_app_entries(entity_registry, loaded)) == MAX_APP_SENSORS


# --- Across a restart ------------------------------------------------------


async def test_app_sensors_come_back_before_the_phone_does(
    hass: HomeAssistant,
    hass_client_no_auth,
    loaded,
    entity_registry: er.EntityRegistry,
    hass_storage,
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _screen_time(*TWO_DAYS))
    await _enable(hass, entity_registry, loaded, WHATSAPP)
    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()

    stored = hass_storage[f"{DOMAIN}.{loaded.entry_id}.apps"]["data"]
    assert stored["labels"] == {SPOTIFY: "Spotify", CHROME: "Chrome", WHATSAPP: "WhatsApp"}
    assert stored["table"]["date"] == "2026-09-15"

    assert await hass.config_entries.async_setup(loaded.entry_id)
    await hass.async_block_till_done()
    whatsapp = hass.states.get("sensor.owen_s_pixel_whatsapp_screen_time")
    assert whatsapp.state == "44"
    assert whatsapp.attributes["friendly_name"] == "Owen's Pixel WhatsApp screen time"
    # The disabled ones keep their name in the registry too.
    assert _app_entries(entity_registry, loaded)[CHROME].original_name == "Chrome screen time"


async def test_removing_the_phone_removes_its_app_list(
    hass: HomeAssistant, hass_client_no_auth, loaded, hass_storage
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _screen_time(*TWO_DAYS))
    assert await hass.config_entries.async_unload(loaded.entry_id)
    assert f"{DOMAIN}.{loaded.entry_id}.apps" in hass_storage

    await hass.config_entries.async_remove(loaded.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{loaded.entry_id}.apps" not in hass_storage


# --- A deleted sensor stays deleted ----------------------------------------


def _stored(hass_storage, entry: MockConfigEntry) -> str:
    return json.dumps(hass_storage[f"{DOMAIN}.{entry.entry_id}.apps"]["data"])


async def test_a_deleted_sensor_does_not_come_back(
    hass: HomeAssistant,
    hass_client_no_auth,
    loaded,
    entity_registry: er.EntityRegistry,
    hass_storage,
) -> None:
    """Not at the next sync, not after a reload, and its name leaves the store."""
    client = await hass_client_no_auth()
    await _post(hass, client, _screen_time(*TWO_DAYS))
    entity_registry.async_remove(_app_entries(entity_registry, loaded)[WHATSAPP].entity_id)
    await hass.async_block_till_done()

    # The phone still sends it.
    await _post(hass, client, _screen_time(*TWO_DAYS, timestamp="2026-09-15T19:00:00Z"))
    assert WHATSAPP not in _app_entries(entity_registry, loaded)

    # And now leaves it out, as an app filter would; then a reload.
    await _post(
        hass,
        client,
        _screen_time(
            _day("2026-09-15", (CHROME, "Chrome", 61), (SPOTIFY, "Spotify", 22)),
            timestamp="2026-09-15T20:00:00Z",
        ),
    )
    assert await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done()
    assert set(_app_entries(entity_registry, loaded)) == {CHROME, SPOTIFY}

    stored = _stored(hass_storage, loaded)
    assert "WhatsApp" not in stored
    assert WHATSAPP not in stored
    assert len(json.loads(stored)["dismissed"]) == 1


async def test_a_deleted_enabled_sensor_goes_from_the_state_machine_too(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _screen_time(*TWO_DAYS))
    await _enable(hass, entity_registry, loaded, CHROME)
    # Renamed first: the removal names the new entity id only.
    entity_registry.async_update_entity(
        "sensor.owen_s_pixel_chrome_screen_time", new_entity_id="sensor.browser_minutes"
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.browser_minutes").state == "61"

    entity_registry.async_remove("sensor.browser_minutes")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.browser_minutes") is None

    await _post(hass, client, _screen_time(*TWO_DAYS, timestamp="2026-09-15T19:00:00Z"))
    assert CHROME not in _app_entries(entity_registry, loaded)
    assert CHROME not in loaded.runtime_data.apps.labels


async def test_a_sensor_deleted_while_unloaded_stays_deleted(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry
) -> None:
    """Deleted while the entry was not loaded: no event was seen, the registry tells."""
    client = await hass_client_no_auth()
    await _post(hass, client, _screen_time(*TWO_DAYS))
    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()
    entity_registry.async_remove(_app_entries(entity_registry, loaded)[SPOTIFY].entity_id)

    assert await hass.config_entries.async_setup(loaded.entry_id)
    await hass.async_block_till_done()
    assert SPOTIFY not in _app_entries(entity_registry, loaded)
    await _post(hass, client, _screen_time(*TWO_DAYS, timestamp="2026-09-15T19:00:00Z"))
    assert set(_app_entries(entity_registry, loaded)) == {CHROME, WHATSAPP}


async def test_a_deleted_sensor_frees_its_slot(
    hass: HomeAssistant, hass_client_no_auth, loaded, entity_registry: er.EntityRegistry
) -> None:
    client = await hass_client_no_auth()
    apps = [(f"com.example.app{n}", f"App {n}", 10 + n) for n in range(MAX_APP_SENSORS + 10)]
    await _post(hass, client, _screen_time(_day("2026-09-15", *apps)))
    top = f"com.example.app{MAX_APP_SENSORS + 9}"
    entity_registry.async_remove(_app_entries(entity_registry, loaded)[top].entity_id)
    await hass.async_block_till_done()

    await _post(
        hass,
        client,
        _screen_time(_day("2026-09-15", *apps), timestamp="2026-09-15T19:00:00Z"),
    )
    entries = _app_entries(entity_registry, loaded)
    assert len(entries) == MAX_APP_SENSORS
    # The most used app that had no room gets the slot; the deleted one does not.
    assert "com.example.app9" in entries
    assert top not in entries


# --- Writes ----------------------------------------------------------------


async def test_a_sync_that_changes_nothing_writes_nothing(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """A screen time sync every few minutes must not mean a write every few minutes."""
    client = await hass_client_no_auth()
    store = loaded.runtime_data.apps._store
    with patch.object(store, "async_delay_save", wraps=store.async_delay_save) as save:
        await _post(hass, client, _screen_time(*TWO_DAYS))
        assert save.call_count == 1

        # Same minutes for the apps with a sensor; only an app without one moved.
        await _post(
            hass,
            client,
            _screen_time(
                TWO_DAYS[0],
                _day(
                    "2026-09-15",
                    (CHROME, "Chrome", 61),
                    (WHATSAPP, "WhatsApp", 44),
                    (SPOTIFY, "Spotify", 22),
                    ("com.example.once", "Once", 1),
                ),
                timestamp="2026-09-15T19:00:00Z",
            ),
        )
        assert save.call_count == 1

        await _post(
            hass,
            client,
            _screen_time(
                TWO_DAYS[0],
                _day(
                    "2026-09-15",
                    (CHROME, "Chrome", 75),
                    (WHATSAPP, "WhatsApp", 44),
                    (SPOTIFY, "Spotify", 22),
                ),
                timestamp="2026-09-15T20:00:00Z",
            ),
        )
        assert save.call_count == 2
