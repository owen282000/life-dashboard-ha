"""The four day totals on a new day (app discussion #257).

Health Connect leaves a metric out of a day that has no data for it yet. The sensor
then starts the new day at 0, at midnight and with the first sync of the day, rather
than showing the day before.
"""

import json
from datetime import UTC, datetime
from http import HTTPStatus

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.life_dashboard.const import (
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    URL_CHOICE_INTERNAL,
)
from custom_components.life_dashboard.payload import (
    SIGNATURE_HEADER,
    signature_for,
)

WEBHOOK_ID = "a" * 64
SECRET = "b" * 64
URL = f"/api/webhook/{WEBHOOK_ID}"
ENTRY_ID = "01DAYTOTALSNEWDAY0000000000"
STEPS = "sensor.owen_s_pixel_steps_today"
ENERGY = "sensor.owen_s_pixel_active_calories_today"
DISTANCE = "sensor.owen_s_pixel_distance_today"
TOTAL = "sensor.owen_s_pixel_total_calories_today"

# Sunday 4 October 2026, 14:00 in Amsterdam (UTC+2).
NOON = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
async def entry(hass: HomeAssistant, freezer) -> MockConfigEntry:
    freezer.move_to(NOON)
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


def _health(*totals: dict, timestamp: str = "2026-10-04T12:00:00Z", **extra) -> dict:
    payload = {
        "timestamp": timestamp,
        "app_version": "1.33.0",
        "source": "health_connect",
        "daily_totals": list(totals),
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


SUNDAY = {"date": "2026-10-04", "steps": 8200, "active_calories": 410.5, "distance_meters": 6100.0}
# 07:00 on Monday in Amsterdam.
MONDAY_MORNING = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)


def _state(hass: HomeAssistant, entity_id: str) -> tuple[str, str]:
    state = hass.states.get(entity_id)
    return state.state, state.attributes["date"]


async def test_a_metric_without_data_today_starts_at_0(
    hass: HomeAssistant, hass_client_no_auth, loaded, freezer
) -> None:
    """The morning sync of #257: steps for Monday already, no active energy yet."""
    client = await hass_client_no_auth()
    await _post(hass, client, _health(SUNDAY))
    assert _state(hass, ENERGY) == ("410.5", "2026-10-04")

    freezer.move_to(MONDAY_MORNING)
    await _post(
        hass,
        client,
        _health(SUNDAY, {"date": "2026-10-05", "steps": 312}, timestamp="2026-10-05T05:00:00Z"),
    )

    assert _state(hass, STEPS) == ("312", "2026-10-05")
    assert _state(hass, ENERGY) == ("0.0", "2026-10-05")
    assert _state(hass, DISTANCE) == ("0.0", "2026-10-05")


async def test_a_metric_the_phone_never_sent_gets_no_sensor(
    hass: HomeAssistant, hass_client_no_auth, loaded, freezer
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(SUNDAY))
    freezer.move_to(MONDAY_MORNING)
    await _post(
        hass,
        client,
        _health({"date": "2026-10-05", "steps": 312}, timestamp="2026-10-05T05:00:00Z"),
    )

    assert hass.states.get(TOTAL) is None


async def test_the_day_totals_go_to_0_at_midnight_without_a_sync(
    hass: HomeAssistant, hass_client_no_auth, loaded, freezer
) -> None:
    """A quiet period from 23:00 to 07:00 leaves no sync to start the day."""
    client = await hass_client_no_auth()
    await _post(hass, client, _health(SUNDAY))

    # 00:00:05 on Monday in Amsterdam.
    midnight = datetime(2026, 10, 4, 22, 0, 5, tzinfo=UTC)
    freezer.move_to(midnight)
    async_fire_time_changed(hass, midnight)
    await hass.async_block_till_done()

    assert _state(hass, STEPS) == ("0", "2026-10-05")
    assert _state(hass, ENERGY) == ("0.0", "2026-10-05")
    assert _state(hass, DISTANCE) == ("0.0", "2026-10-05")
    assert hass.states.get(TOTAL) is None


async def test_the_first_sync_of_the_day_still_sets_the_real_value(
    hass: HomeAssistant, hass_client_no_auth, loaded, freezer
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(SUNDAY))
    midnight = datetime(2026, 10, 4, 22, 0, 5, tzinfo=UTC)
    freezer.move_to(midnight)
    async_fire_time_changed(hass, midnight)
    await hass.async_block_till_done()

    freezer.move_to(MONDAY_MORNING)
    await _post(
        hass,
        client,
        _health(
            {"date": "2026-10-05", "steps": 312, "active_calories": 18.0},
            timestamp="2026-10-05T05:00:00Z",
        ),
    )
    assert _state(hass, STEPS) == ("312", "2026-10-05")
    assert _state(hass, ENERGY) == ("18.0", "2026-10-05")
    assert _state(hass, DISTANCE) == ("0.0", "2026-10-05")


async def test_a_backfill_of_old_days_resets_nothing(
    hass: HomeAssistant, hass_client_no_auth, loaded, freezer
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(SUNDAY))
    await _post(
        hass,
        client,
        _health({"date": "2026-09-20", "steps": 5000}, backfill=True),
    )
    assert _state(hass, ENERGY) == ("410.5", "2026-10-04")
    assert _state(hass, STEPS) == ("8200", "2026-10-04")
