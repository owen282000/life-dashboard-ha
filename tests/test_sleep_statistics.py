"""The sleep stages and the sleep sum in the recorder, row by row."""

import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.recorder.statistics import async_list_statistic_ids
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.util import dt as dt_util
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
from custom_components.life_dashboard.history import Ledger, apply_payload
from custom_components.life_dashboard.payload import SIGNATURE_HEADER, signature_for
from custom_components.life_dashboard.statistics import statistic_id

WEBHOOK_ID = "a" * 64
SECRET = "b" * 64
URL = f"/api/webhook/{WEBHOOK_ID}"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    yield


async def _entry(
    hass: HomeAssistant, time_zone: str = "Europe/Amsterdam", **extra
) -> MockConfigEntry:
    await async_process_ha_core_config(hass, {"time_zone": time_zone})
    entry = MockConfigEntry(
        **extra,
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


async def _post(hass: HomeAssistant, client, payload: dict) -> None:
    body = json.dumps(payload).encode()
    response = await client.post(
        URL, data=body, headers={SIGNATURE_HEADER: signature_for(SECRET, body)}
    )
    assert response.status == 200
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


def _day(n: int) -> date:
    return dt_util.now().date() - timedelta(days=n)


def _night(n: int, uuid: str, hours: int = 6) -> dict:
    day = _day(n).isoformat()
    previous = _day(n + 1).isoformat()
    return {
        "uuid": uuid,
        "session_end_time": f"{day}T05:00:00Z",
        "duration_seconds": (hours + 1) * 3600,
        "stages": [
            {
                "stage": "deep",
                "start_time": f"{previous}T23:00:00Z",
                "end_time": f"{day}T01:00:00Z",
            },
            {
                "stage": "light",
                "start_time": f"{day}T01:00:00Z",
                "end_time": f"{day}T05:00:00Z",
            },
        ],
    }


def _payload(*sleep, offset: int = 0, **extra) -> dict:
    moment = (datetime.now(UTC) + timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"timestamp": moment, "source": "health_connect", "sleep": list(sleep), **extra}


def _rows(hass, entry, key, period="hour", types=frozenset({"mean", "min", "max"})):
    sid = statistic_id(entry, key)
    start = datetime(2020, 1, 1, tzinfo=UTC)
    return statistics_during_period(hass, start, None, {sid}, period, None, set(types)).get(sid, [])


def _sums(hass, entry, key):
    rows = statistics_during_period(
        hass,
        datetime(2020, 1, 1, tzinfo=UTC),
        None,
        {statistic_id(entry, key)},
        "hour",
        None,
        {"state", "sum"},
    ).get(statistic_id(entry, key), [])
    return [r for r in rows if r["state"] is not None]


async def test_one_row_per_night_at_local_noon(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    await _post(hass, client, _payload(_night(1, "a")))

    rows = _rows(hass, entry, "sleep_deep")
    assert len(rows) == 1
    assert rows[0]["mean"] == 2.0
    assert rows[0]["min"] == 2.0
    start = datetime.fromtimestamp(rows[0]["start"], dt_util.get_default_time_zone())
    assert start.date() == _day(1)
    assert start.hour == 12 and start.minute == 0

    assert [r["mean"] for r in _rows(hass, entry, "sleep_asleep")] == [6.0]
    assert [r["mean"] for r in _rows(hass, entry, "sleep_awake")] == [0.0]
    items = {item["statistic_id"]: item for item in await async_list_statistic_ids(hass)}
    item = items[statistic_id(entry, "sleep_deep")]
    assert item["has_sum"] is False
    assert item["statistics_unit_of_measurement"] == "h"
    assert item["unit_class"] == "duration"
    assert item["has_mean"] is True


async def test_a_day_bin_holds_one_nights_value(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    await _post(hass, client, _payload(_night(2, "a"), _night(1, "b", hours=5)))
    rows = _rows(hass, entry, "sleep_deep", period="day", types={"mean"})
    assert [r["mean"] for r in rows] == [2.0, 2.0]


async def test_a_half_hour_zone_still_writes_on_the_hour(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _entry(hass, "Asia/Kolkata")
    client = await hass_client_no_auth()
    await _post(hass, client, _payload(_night(1, "a")))
    rows = _rows(hass, entry, "sleep_deep")
    assert len(rows) == 1
    assert datetime.fromtimestamp(rows[0]["start"], UTC).minute == 0


async def test_a_correction_rewrites_the_row(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    await _post(hass, client, _payload(_night(1, "a")))
    revised = _night(1, "a")
    revised["stages"] = revised["stages"][:1]
    await _post(hass, client, _payload(revised, offset=5))
    assert [r["mean"] for r in _rows(hass, entry, "sleep_deep")] == [2.0]
    assert [r["mean"] for r in _rows(hass, entry, "sleep_light")] == [0.0]
    assert [r["mean"] for r in _rows(hass, entry, "sleep_asleep")] == [2.0]


async def test_a_late_night_touches_only_its_own_row(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    await _post(hass, client, _payload(_night(1, "a")))
    await _post(hass, client, _payload(_night(3, "b", hours=5), offset=5))
    means = [r["mean"] for r in _rows(hass, entry, "sleep_deep")]
    assert means == [2.0, 2.0]
    starts = [r["start"] for r in _rows(hass, entry, "sleep_deep")]
    assert starts == sorted(starts)


async def test_a_deletion_empties_the_row_and_keeps_the_sum_in_line(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    await _post(hass, client, _payload(_night(3, "a"), _night(2, "b"), _night(1, "c")))
    before = _sums(hass, entry, "sleep_minutes")
    assert before[-1]["sum"] == 21.0

    await _post(
        hass,
        client,
        _payload(offset=5, deleted_records=[{"type": "sleep", "uuid": "b"}]),
    )

    # The middle day of the running sum is 0, and every later row went down with it.
    sums = _sums(hass, entry, "sleep_minutes")
    assert sums[-1]["sum"] == 14.0
    midnight = datetime.combine(_day(2), datetime.min.time(), dt_util.get_default_time_zone())
    middle = [r for r in sums if r["start"] == midnight.timestamp()]
    assert [(r["state"], r["sum"]) for r in middle] == [(0.0, 7.0)]
    # Never above a later row: no jump.
    totals = [r["sum"] for r in sums]
    assert totals == sorted(totals)

    # The stage row of the deleted night is taken away: its mean is gone, not zero.
    deep = _rows(hass, entry, "sleep_deep")
    assert [r["mean"] for r in deep] == [2.0, None, 2.0]


async def test_a_recorder_that_can_clear_part_of_a_statistic_clears_the_row(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    await _post(hass, client, _payload(_night(1, "a")))

    calls = []

    class Recorder:
        def async_clear_statistics(self, statistic_ids, *, start_time=None, end_time=None):
            calls.append((statistic_ids, start_time, end_time))

    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=Recorder(),
    ):
        writer = entry.runtime_data.history
        rows = writer._take_away_rows(
            "sleep_deep",
            [
                {
                    "start": datetime(2026, 11, 8, 11, 0, tzinfo=UTC),
                    "mean": None,
                    "min": None,
                    "max": None,
                },
                {
                    "start": datetime(2026, 11, 7, 11, 0, tzinfo=UTC),
                    "mean": 1.0,
                    "min": 1.0,
                    "max": 1.0,
                },
            ],
        )
    assert [row["mean"] for row in rows] == [1.0]
    assert calls[0][0] == [statistic_id(entry, "sleep_deep")]
    assert calls[0][2] - calls[0][1] == timedelta(hours=1)


async def test_nothing_to_report_writes_no_row(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    night = _night(1, "a")
    night["stages"] = []
    await _post(hass, client, _payload(night))
    assert _rows(hass, entry, "sleep_deep") == []
    assert _rows(hass, entry, "sleep_asleep") == []
    assert [r["state"] for r in _sums(hass, entry, "sleep_minutes")] == [7.0]


def _version(uuid: str, deep_hours: int, light_hours: int) -> dict:
    """One of the versions Samsung writes of yesterday's night, all from 23:00 UTC."""
    day = _day(1).isoformat()
    previous = _day(2).isoformat()
    end = 1 + light_hours
    return {
        "uuid": uuid,
        "source": "com.sec.android.app.shealth",
        "session_end_time": f"{day}T{end:02d}:00:00Z",
        "duration_seconds": (deep_hours + light_hours) * 3600,
        "stages": [
            {
                "stage": "deep",
                "start_time": f"{previous}T23:00:00Z",
                "end_time": f"{day}T{deep_hours - 1:02d}:00:00Z",
            },
            {
                "stage": "light",
                "start_time": f"{day}T{deep_hours - 1:02d}:00:00Z",
                "end_time": f"{day}T{end:02d}:00:00Z",
            },
        ],
    }


async def test_a_ledger_of_1_5_1_writes_its_versions_as_one_night(
    hass: HomeAssistant, hass_storage
) -> None:
    """ha#87: three versions of one night counted three times in sleep_minutes, and the
    roughest spoke for its stages. Starting up writes that date again, once."""
    await async_process_ha_core_config(hass, {"time_zone": "Europe/Amsterdam"})
    ledger = Ledger()
    # The night with its stages, 2 h deep and 5 h light, and a rougher estimate with more
    # time asleep: 1 h deep and 7 h light.
    apply_payload(
        ledger,
        _payload(_version("shown", 2, 5), _version("rough", 1, 7)),
        tz=dt_util.get_default_time_zone(),
    )
    written = ledger.to_dict()
    del written["nights_settled"]
    key = f"{DOMAIN}.settle.history"
    hass_storage[key] = {"version": 1, "minor_version": 6, "key": key, "data": written}

    entry = await _entry(hass, entry_id="settle")
    await async_wait_recording_done(hass)

    assert [r["mean"] for r in _rows(hass, entry, "sleep_deep")] == [2.0]
    assert [r["mean"] for r in _rows(hass, entry, "sleep_asleep")] == [7.0]
    assert [r["state"] for r in _sums(hass, entry, "sleep_minutes")][-1] == 7.0
    assert entry.runtime_data.history.ledger.nights_settled is True
