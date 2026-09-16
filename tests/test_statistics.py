"""History lands in long-term statistics, on the days it came from."""

import json
from datetime import UTC, datetime

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    statistics_during_period,
)

from custom_components.life_dashboard.const import (
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    URL_CHOICE_INTERNAL,
)
from custom_components.life_dashboard.payload import SIGNATURE_HEADER, signature_for
from custom_components.life_dashboard.statistics import statistic_id

WEBHOOK_ID = "a" * 64


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Overrides the conftest fixture: the recorder's database has to exist before
    the hass fixture starts, and enable_custom_integrations pulls hass in."""
    yield


SECRET = "b" * 64
URL = f"/api/webhook/{WEBHOOK_ID}"


async def _entry(hass: HomeAssistant) -> MockConfigEntry:
    await async_process_ha_core_config(hass, {"time_zone": "Europe/Amsterdam"})
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
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _post(client, payload: dict) -> int:
    body = json.dumps(payload).encode()
    r = await client.post(URL, data=body, headers={SIGNATURE_HEADER: signature_for(SECRET, body)})
    return r.status


def _rows(hass, entry, key, period, types, start=datetime(2020, 1, 1, tzinfo=UTC)):
    sid = statistic_id(entry, key)
    return statistics_during_period(hass, start, None, {sid}, period, None, types).get(sid, [])


def _backfill(**parts):
    base = {"timestamp": "2026-09-15T18:00:00Z", "source": "health_connect", "backfill": True}
    return {**base, **parts}


SLEEP = {"session_end_time": "2026-03-02T06:00:00Z", "duration_seconds": 27000, "uuid": "s"}
HEART = [{"bpm": 60, "time": "2026-03-01T08:10:00Z"}, {"bpm": 70, "time": "2026-03-01T08:50:00Z"}]


async def test_a_backfill_lands_on_its_own_days(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    assert (
        await _post(
            client,
            {
                "timestamp": "2026-09-15T18:00:00Z",
                "source": "health_connect",
                "backfill": True,
                "daily_totals": [
                    {"date": "2026-03-01", "steps": 100},
                    {"date": "2026-03-02", "steps": 250},
                ],
                "sleep": [
                    {
                        "session_end_time": "2026-03-02T06:00:00Z",
                        "duration_seconds": 27000,
                        "uuid": "s",
                    }
                ],
                "heart_rate": [
                    {"bpm": 60, "time": "2026-03-01T08:10:00Z"},
                    {"bpm": 70, "time": "2026-03-01T08:50:00Z"},
                ],
            },
        )
        == 200
    )
    await async_wait_recording_done(hass)

    steps = _rows(hass, entry, "steps", "day", {"state", "sum"})
    assert [(r["state"], r["sum"]) for r in steps] == [(100.0, 100.0), (250.0, 350.0)]
    assert datetime.fromtimestamp(steps[0]["start"], UTC).date().isoformat() in (
        "2026-02-28",
        "2026-03-01",
    )

    sleep = _rows(hass, entry, "sleep_minutes", "day", {"state"})
    assert [r["state"] for r in sleep] == [450.0]

    hr = _rows(hass, entry, "heart_rate", "hour", {"mean", "min", "max"})
    assert [(r["mean"], r["min"], r["max"]) for r in hr] == [(65.0, 60.0, 70.0)]


async def test_the_same_backfill_twice_does_not_double(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    payload = _backfill(daily_totals=[{"date": "2026-03-01", "steps": 100}], sleep=[SLEEP])
    for _ in range(2):
        assert await _post(client, payload) == 200
    await async_wait_recording_done(hass)

    assert [r["sum"] for r in _rows(hass, entry, "steps", "day", {"sum"})] == [100.0]
    assert [r["state"] for r in _rows(hass, entry, "sleep_minutes", "day", {"state"})] == [450.0]


async def test_the_ledger_survives_a_reload(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    await _post(
        client,
        {
            "timestamp": "2026-09-15T18:00:00Z",
            "source": "health_connect",
            "daily_totals": [{"date": "2026-09-14", "steps": 100}],
        },
    )
    await hass.async_block_till_done()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    await _post(
        client,
        {
            "timestamp": "2026-09-15T19:00:00Z",
            "source": "health_connect",
            "daily_totals": [{"date": "2026-09-15", "steps": 50}],
        },
    )
    await async_wait_recording_done(hass)

    # The running sum continues from the day written before the reload.
    assert [r["sum"] for r in _rows(hass, entry, "steps", "day", {"sum"})] == [
        100.0,
        150.0,
    ]


async def test_ids_are_valid_and_a_test_ping_writes_nothing(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    from homeassistant.components.recorder.statistics import valid_statistic_id

    entry = await _entry(hass)
    assert valid_statistic_id(statistic_id(entry, "heart_rate"))
    client = await hass_client_no_auth()
    assert (
        await _post(
            client, {"test": True, "timestamp": "2026-09-15T18:00:00Z", "source": "health_connect"}
        )
        == 200
    )
    await async_wait_recording_done(hass)
    assert _rows(hass, entry, "steps", "day", {"sum"}) == []


async def test_screen_time_becomes_a_day_statistic(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    payload = {
        "timestamp": "2026-09-16T10:00:00Z",
        "source": "screen_time",
        "screen_time": [
            {"date": "2026-09-15", "total_screen_time_minutes": 180, "apps": []},
            {"date": "2026-09-16", "total_screen_time_minutes": 25, "apps": []},
        ],
    }
    assert await _post(client, payload) == 200
    await async_wait_recording_done(hass)

    rows = _rows(hass, entry, "screen_time", "day", {"state", "sum"})
    assert [(r["state"], r["sum"]) for r in rows] == [(180.0, 180.0), (25.0, 205.0)]
