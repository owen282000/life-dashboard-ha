"""A new pairing can take over the statistics of a removed phone (P2-30)."""

import json
from datetime import UTC, datetime, time, timedelta
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_list_statistic_ids,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    statistics_during_period,
)

from custom_components.life_dashboard.const import CONF_BASE_URL, CONF_TAKE_OVER, DOMAIN
from custom_components.life_dashboard.payload import SIGNATURE_HEADER, signature_for
from custom_components.life_dashboard.statistics import statistic_id

WEBHOOK_ID = "a" * 64
SECRET = "b" * 64
URL = f"/api/webhook/{WEBHOOK_ID}"
# A removed entry's id as it stands in a statistic id: a lowercased ULID.
OLD = "01jbq3z8k2v6x9w4n7m5r1t0ya"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """The recorder's database has to exist before the hass fixture starts."""
    yield


@pytest.fixture(autouse=True)
def _fixed_ids():
    with (
        patch(
            "custom_components.life_dashboard.config_flow.webhook.async_generate_id",
            return_value=WEBHOOK_ID,
        ),
        patch(
            "custom_components.life_dashboard.config_flow.secrets.token_hex",
            return_value=SECRET,
        ),
    ):
        yield


def _day(n: int):
    return dt_util.now().date() - timedelta(days=n)


def _start(n: int) -> datetime:
    return datetime.combine(_day(n), time.min, tzinfo=dt_util.get_default_time_zone())


async def _old_phone(hass: HomeAssistant) -> None:
    """What a removed phone leaves behind: steps on two days and an hour of heart rate."""
    await async_process_ha_core_config(hass, {"time_zone": "Europe/Amsterdam"})
    steps = {
        "statistic_id": f"{DOMAIN}:{OLD}_steps",
        "source": DOMAIN,
        "name": "Old Pixel steps",
        "unit_of_measurement": "steps",
        "unit_class": None,
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
    }
    async_add_external_statistics(
        hass,
        steps,
        [
            {"start": _start(3), "state": 100.0, "sum": 100.0},
            {"start": _start(2), "state": 50.0, "sum": 150.0},
        ],
    )
    heart = {
        **steps,
        "statistic_id": f"{DOMAIN}:{OLD}_heart_rate",
        "name": "Old Pixel heart rate",
        "unit_of_measurement": "bpm",
        "has_sum": False,
        "mean_type": StatisticMeanType.ARITHMETIC,
    }
    hour = _start(2) + timedelta(hours=8)
    async_add_external_statistics(
        hass, heart, [{"start": hour, "mean": 60.0, "min": 55.0, "max": 70.0}]
    )
    await async_wait_recording_done(hass)


async def _pair(hass: HomeAssistant, take_over: str | None):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "New Pixel", CONF_BASE_URL: "http://ha.local:8123"}
    )
    if take_over is not None:
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "history"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TAKE_OVER: take_over}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    return hass.config_entries.async_entries(DOMAIN)[-1]


def _rows(hass, sid, types):
    """The rows at midnight, leaving out the evening rows a day sum also has."""
    start = datetime(2020, 1, 1, tzinfo=UTC)
    rows = statistics_during_period(hass, start, None, {sid}, "hour", None, types).get(sid, [])
    tz = dt_util.get_default_time_zone()
    return [r for r in rows if datetime.fromtimestamp(r["start"], tz).hour != 23]


async def _post(client, payload: dict) -> int:
    base = {"timestamp": "2026-09-15T18:00:00Z", "source": "health_connect", "backfill": True}
    body = json.dumps({**base, **payload}).encode()
    r = await client.post(URL, data=body, headers={SIGNATURE_HEADER: signature_for(SECRET, body)})
    return r.status


async def test_the_new_phone_continues_the_removed_ones_history(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _old_phone(hass)
    entry = await _pair(hass, OLD)

    assert CONF_TAKE_OVER not in entry.data
    steps = statistic_id(entry, "steps")
    assert [(r["state"], r["sum"]) for r in _rows(hass, steps, {"state", "sum"})] == [
        (100.0, 100.0),
        (50.0, 150.0),
    ]
    heart = _rows(hass, statistic_id(entry, "heart_rate"), {"mean", "min", "max"})
    assert [(r["mean"], r["min"], r["max"]) for r in heart] == [(60.0, 55.0, 70.0)]
    left = {item["statistic_id"] for item in await async_list_statistic_ids(hass)}
    assert not any(OLD in sid for sid in left)

    ledger = entry.runtime_data.history.ledger
    # The ledger starts on the last day the old phone sent, with that day put back.
    assert ledger.kept_from == _day(2).isoformat()
    assert ledger.carried == {"steps": 100.0}
    assert ledger.days == {"steps": {_day(2).isoformat(): 50.0}}
    assert list(ledger.hours["heart_rate"].values())[0].mean == 60.0
    units = {
        item["statistic_id"]: item["statistics_unit_of_measurement"]
        for item in await async_list_statistic_ids(hass)
    }
    assert units[steps] == "steps"
    assert units[statistic_id(entry, "heart_rate")] == "bpm"

    # The next day carries on from the old total.
    client = await hass_client_no_auth()
    assert (
        await _post(client, {"daily_totals": [{"date": _day(1).isoformat(), "steps": 70}]}) == 200
    )
    await async_wait_recording_done(hass)
    assert [r["sum"] for r in _rows(hass, steps, {"sum"})] == [100.0, 150.0, 220.0]

    # A backfill that sends the old days again counts them once; a day the old phone
    # never sent is added under the rows it had.
    again = {
        "daily_totals": [
            {"date": _day(4).isoformat(), "steps": 40},
            {"date": _day(3).isoformat(), "steps": 100},
            {"date": _day(2).isoformat(), "steps": 50},
            {"date": _day(1).isoformat(), "steps": 70},
        ]
    }
    assert await _post(client, again) == 200
    await async_wait_recording_done(hass)
    assert [r["sum"] for r in _rows(hass, steps, {"sum"})] == [40.0, 140.0, 190.0, 260.0]


async def test_the_last_day_of_the_old_phone_can_still_change(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Swapped the same day: today's total from the new phone replaces the old one's."""
    await _old_phone(hass)
    entry = await _pair(hass, OLD)
    client = await hass_client_no_auth()
    steps = statistic_id(entry, "steps")

    payload = {"daily_totals": [{"date": _day(2).isoformat(), "steps": 80}]}
    assert await _post(client, payload) == 200
    await async_wait_recording_done(hass)
    assert [(r["state"], r["sum"]) for r in _rows(hass, steps, {"state", "sum"})] == [
        (100.0, 100.0),
        (80.0, 180.0),
    ]


async def test_a_reading_dated_in_the_future_does_not_hold_the_days_before(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _old_phone(hass)
    future = _start(-20) + timedelta(hours=9)
    heart = {
        "statistic_id": f"{DOMAIN}:{OLD}_heart_rate",
        "source": DOMAIN,
        "name": "Old Pixel heart rate",
        "unit_of_measurement": "bpm",
        "unit_class": None,
        "has_sum": False,
        "mean_type": StatisticMeanType.ARITHMETIC,
    }
    async_add_external_statistics(
        hass, heart, [{"start": future, "mean": 80.0, "min": 80.0, "max": 80.0}]
    )
    await async_wait_recording_done(hass)
    entry = await _pair(hass, OLD)
    assert entry.runtime_data.history.ledger.kept_from == _day(2).isoformat()

    client = await hass_client_no_auth()
    for count in (100, 300):
        payload = {"daily_totals": [{"date": _day(0).isoformat(), "steps": count}]}
        assert await _post(client, payload) == 200
        await async_wait_recording_done(hass)
    assert _rows(hass, statistic_id(entry, "steps"), {"state"})[-1]["state"] == 300.0


async def test_a_backfill_replaces_the_stand_in_for_the_last_days_sessions(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """The statistics hold a day's sleep as one figure; a backfill brings the sessions."""
    await async_process_ha_core_config(hass, {"time_zone": "Europe/Amsterdam"})
    sleep = {
        "statistic_id": f"{DOMAIN}:{OLD}_sleep_minutes",
        "source": DOMAIN,
        "name": "Old Pixel sleep minutes",
        "unit_of_measurement": "min",
        "unit_class": None,
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
    }
    async_add_external_statistics(
        hass,
        sleep,
        [
            {"start": _start(3), "state": 400.0, "sum": 400.0},
            {"start": _start(2), "state": 420.0, "sum": 820.0},
        ],
    )
    await async_wait_recording_done(hass)
    entry = await _pair(hass, OLD)
    client = await hass_client_no_auth()
    sid = statistic_id(entry, "sleep_minutes")

    # A nap from a normal sync adds to the day.
    nap = {"session_end_time": f"{_day(2).isoformat()}T14:00:00Z", "duration_seconds": 1200}
    assert await _post(client, {"backfill": False, "sleep": [{**nap, "uuid": "nap"}]}) == 200
    await async_wait_recording_done(hass)
    last = _rows(hass, sid, {"state", "sum"})[-1]
    assert (last["state"] * 60, last["sum"] * 60) == pytest.approx((440.0, 840.0))

    # A backfill sends the night and the nap again: they replace the stand-in, once.
    night = {"session_end_time": f"{_day(2).isoformat()}T06:00:00Z", "duration_seconds": 25200}
    payload = {"sleep": [{**night, "uuid": "night"}, {**nap, "uuid": "nap"}]}
    assert await _post(client, payload) == 200
    await async_wait_recording_done(hass)
    last = _rows(hass, sid, {"state", "sum"})[-1]
    assert (last["state"] * 60, last["sum"] * 60) == pytest.approx((440.0, 840.0))


async def test_starting_empty_leaves_the_old_history_alone(hass: HomeAssistant) -> None:
    await _old_phone(hass)
    entry = await _pair(hass, "none")

    assert CONF_TAKE_OVER not in entry.data
    assert _rows(hass, statistic_id(entry, "steps"), {"sum"}) == []
    assert len(_rows(hass, f"{DOMAIN}:{OLD}_steps", {"sum"})) == 2
    assert entry.runtime_data.history.ledger.kept_from is None


async def test_a_phone_that_is_still_there_is_not_offered(hass: HomeAssistant) -> None:
    """Two phones keep their own history: only a removed one's is on the list."""
    await async_process_ha_core_config(hass, {"time_zone": "Europe/Amsterdam"})
    first = await _pair(hass, None)
    async_add_external_statistics(
        hass,
        {
            "statistic_id": statistic_id(first, "steps"),
            "source": DOMAIN,
            "name": "New Pixel steps",
            "unit_of_measurement": "steps",
            "unit_class": None,
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
        },
        [{"start": _start(2), "state": 50.0, "sum": 50.0}],
    )
    await async_wait_recording_done(hass)

    with patch(
        "custom_components.life_dashboard.config_flow.webhook.async_generate_id",
        return_value="c" * 64,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_NAME: "Second", CONF_BASE_URL: "http://ha.local:8123"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
