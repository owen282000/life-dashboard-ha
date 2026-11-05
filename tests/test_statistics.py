"""History lands in long-term statistics, on the days it came from."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_list_statistic_ids,
)
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
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
    """The rows of the days and hours that have figures. A day sum also has a row on the
    evening before each day (history.closing_rows); on a day without an amount that row
    has no state, and is left out here."""
    sid = statistic_id(entry, key)
    asked = set(types) | ({"state"} if {"sum", "change"} & set(types) else set())
    rows = statistics_during_period(hass, start, None, {sid}, period, None, asked).get(sid, [])
    if "state" in asked and "state" not in types:
        rows = [r for r in rows if r.get("state") is not None]
        return [{k: v for k, v in r.items() if k != "state"} for r in rows]
    if {"sum", "change"} & set(types):
        rows = [r for r in rows if r.get("state") is not None]
    return rows


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
    assert [r["state"] for r in sleep] == [7.5], "in hours since 1.0.0"

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
    assert [r["state"] for r in _rows(hass, entry, "sleep_minutes", "day", {"state"})] == [7.5]


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
    hours = [(r["state"] * 60, r["sum"] * 60) for r in rows]  # stored in hours since 1.0.0
    assert hours == pytest.approx([(180.0, 180.0), (25.0, 205.0)])


def _today():
    """Pruning counts back from today, so these tests take their dates from the clock."""
    return dt_util.now(dt_util.get_time_zone("Europe/Amsterdam")).date()


async def test_the_ledger_lets_old_days_go_and_the_sums_carry_on(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    old = (_today() - timedelta(days=500)).isoformat()
    recent = (_today() - timedelta(days=1)).isoformat()

    payload = _backfill(
        daily_totals=[{"date": old, "steps": 100}, {"date": recent, "steps": 200}],
        sleep=[{"session_end_time": f"{old}T06:00:00Z", "duration_seconds": 600, "uuid": "o"}],
    )
    assert await _post(client, payload) == 200
    await async_wait_recording_done(hass)

    # Both days were written before the old one was let go.
    assert [r["sum"] for r in _rows(hass, entry, "steps", "day", {"sum"})] == [100.0, 300.0]
    ledger = entry.runtime_data.history.ledger
    assert ledger.days["steps"] == {recent: 200.0}
    assert ledger.sessions["sleep_minutes"] == {}
    assert ledger.carried == {"steps": 100.0, "sleep_minutes": 10.0}

    # A correction of the recent day still counts the old one in.
    assert await _post(client, _backfill(daily_totals=[{"date": recent, "steps": 250}])) == 200
    await async_wait_recording_done(hass)
    assert [r["sum"] for r in _rows(hass, entry, "steps", "day", {"sum"})] == [100.0, 350.0]

    # And the old day sent again, as a re-run backfill would, changes nothing.
    again = _backfill(
        daily_totals=[{"date": old, "steps": 100}],
        sleep=[{"session_end_time": f"{old}T06:00:00Z", "duration_seconds": 600, "uuid": "o"}],
    )
    assert await _post(client, again) == 200
    await async_wait_recording_done(hass)
    steps = _rows(hass, entry, "steps", "day", {"state", "sum"})
    assert [(r["state"], r["sum"]) for r in steps] == [(100.0, 100.0), (250.0, 350.0)]
    assert ledger.carried == {"steps": 100.0, "sleep_minutes": 10.0}


async def test_statistics_carry_a_unit_class_so_a_graph_can_convert(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Distance in km and sleep in hours in a statistics graph need the recorder's class."""
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    day = (_today() - timedelta(days=1)).isoformat()
    payload = _backfill(
        daily_totals=[{"date": day, "steps": 100, "distance_meters": 2500, "active_calories": 90}],
        sleep=[{"session_end_time": f"{day}T06:00:00Z", "duration_seconds": 27000, "uuid": "n"}],
        heart_rate=HEART,
    )
    assert await _post(client, payload) == 200
    await async_wait_recording_done(hass)

    classes = {
        item["statistic_id"]: item["unit_class"] for item in await async_list_statistic_ids(hass)
    }
    assert classes[statistic_id(entry, "distance")] == "distance"
    assert classes[statistic_id(entry, "sleep_minutes")] == "duration"
    assert classes[statistic_id(entry, "active_calories")] == "energy"
    assert classes[statistic_id(entry, "steps")] is None
    assert classes[statistic_id(entry, "heart_rate")] is None

    # As a graph asks for it with `unit: km`.
    rows = statistics_during_period(
        hass,
        datetime(2020, 1, 1, tzinfo=UTC),
        None,
        {statistic_id(entry, "distance")},
        "day",
        {"distance": "km"},
        {"sum"},
    )[statistic_id(entry, "distance")]
    assert rows[-1]["sum"] == 2.5


async def test_an_app_whose_sensor_is_on_keeps_its_days_as_a_statistic(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """The app's sensor shows the newest day only; its statistic keeps every day."""
    from homeassistant.helpers import entity_registry as er

    entry = await _entry(hass)
    client = await hass_client_no_auth()
    yesterday = (_today() - timedelta(days=1)).isoformat()
    today = _today().isoformat()
    youtube, chrome = "com.google.android.youtube", "com.android.chrome"

    def week(youtube_today: int) -> dict:
        def app(package: str, name: str, minutes: int) -> dict:
            return {"package": package, "name": name, "minutes": minutes}

        return {
            "timestamp": "2026-09-15T18:00:00Z",
            "source": "screen_time",
            "screen_time": [
                {
                    "date": yesterday,
                    "total_screen_time_minutes": 100,
                    "apps": [app(youtube, "YouTube", 60), app(chrome, "Chrome", 40)],
                },
                {
                    "date": today,
                    "total_screen_time_minutes": youtube_today,
                    "apps": [app(youtube, "YouTube", youtube_today)],
                },
            ],
        }

    assert await _post(client, week(30)) == 200
    await hass.async_block_till_done()
    # The app sensors come disabled; the user turns on YouTube's only.
    registry = er.async_get(hass)
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if item.unique_id.endswith(f"screen_time_app_{youtube}"):
            registry.async_update_entity(item.entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert await _post(client, week(45)) == 200
    await async_wait_recording_done(hass)

    rows = _rows(hass, entry, "app_com_google_android_youtube", "day", {"state", "sum"})
    assert [(r["state"], r["sum"]) for r in rows] == [(1.0, 1.0), (0.75, 1.75)], "in hours"
    assert _rows(hass, entry, "app_com_android_chrome", "day", {"sum"}) == []
    items = {i["statistic_id"]: i for i in await async_list_statistic_ids(hass)}
    item = items[statistic_id(entry, "app_com_google_android_youtube")]
    assert item["name"] == "Owen's Pixel YouTube screen time"
    assert (item["statistics_unit_of_measurement"], item["unit_class"]) == ("h", "duration")


async def _change(hass, entry, key, start):
    """A period's change as a statistic card reads it, from `start` until now."""
    from homeassistant.components.recorder.statistics import statistic_during_period

    found = await hass.async_add_executor_job(
        statistic_during_period, hass, start, None, statistic_id(entry, key), {"change"}, None
    )
    return found["change"]


async def test_a_period_counts_its_first_day(hass: HomeAssistant, hass_client_no_auth) -> None:
    """A week or a month that starts on a day with steps counts that day (1.0.0).

    The recorder reads a period's start from the first row an hour before it; with only
    the day's own row at midnight that was the day itself, and its steps fell out.
    """
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    day = {n: _today() - timedelta(days=n) for n in (6, 5, 3, 2, 1)}
    steps = {6: 1000, 5: 2000, 2: 3000, 1: 4000}
    totals = [{"date": day[n].isoformat(), "steps": count} for n, count in steps.items()]
    assert await _post(client, _backfill(daily_totals=totals)) == 200
    await async_wait_recording_done(hass)

    def midnight(n: int) -> datetime:
        return dt_util.start_of_local_day(day[n])

    assert await _change(hass, entry, "steps", midnight(5)) == 9000.0
    assert await _change(hass, entry, "steps", midnight(2)) == 7000.0
    # From a day without steps: the next day's evening row has the same total.
    assert await _change(hass, entry, "steps", midnight(3)) == 7000.0
    # From before the first day: everything.
    assert await _change(hass, entry, "steps", midnight(6) - timedelta(days=1)) == 10000.0
    # The days themselves still read as before.
    states = [r["state"] for r in _rows(hass, entry, "steps", "day", {"state", "sum"})]
    assert states == [1000.0, 2000.0, 3000.0, 4000.0]


async def test_distance_is_kept_in_km_and_sleep_in_hours(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """A graph cannot convert an external statistic, so they are stored as they read."""
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    day = (_today() - timedelta(days=1)).isoformat()
    payload = _backfill(
        daily_totals=[{"date": day, "distance_meters": 4200}],
        sleep=[{"session_end_time": f"{day}T06:00:00Z", "duration_seconds": 27000, "uuid": "n"}],
    )
    assert await _post(client, payload) == 200
    await async_wait_recording_done(hass)

    assert [r["state"] for r in _rows(hass, entry, "distance", "day", {"state"})] == [4.2]
    items = {i["statistic_id"]: i for i in await async_list_statistic_ids(hass)}
    distance = items[statistic_id(entry, "distance")]
    sleep = items[statistic_id(entry, "sleep_minutes")]
    assert distance["statistics_unit_of_measurement"] == "km"
    assert (sleep["statistics_unit_of_measurement"], sleep["name"]) == ("h", "Owen's Pixel sleep")
    # The ledger keeps what the phone sends.
    assert entry.runtime_data.history.ledger.days["distance"] == {day: 4200.0}


async def test_statistics_from_0_12_in_m_and_min_move_to_km_and_hours(
    hass: HomeAssistant, hass_client_no_auth, hass_storage
) -> None:
    """Rewritten once after the start, before the webhook is up, so the totals carry on."""
    await async_process_ha_core_config(hass, {"time_zone": "Europe/Amsterdam"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="01JTESTPHONE00000000000000",
        title="Owen's Pixel",
        unique_id=WEBHOOK_ID,
        data={
            CONF_WEBHOOK_ID: WEBHOOK_ID,
            CONF_SECRET: SECRET,
            CONF_URL_CHOICE: URL_CHOICE_INTERNAL,
        },
    )
    entry.add_to_hass(hass)
    days = [_today() - timedelta(days=n) for n in (3, 2)]
    # A ledger of 0.12, which says nothing of units, with the distance it wrote.
    key = f"{DOMAIN}.{entry.entry_id}.history"
    distance = {days[0].isoformat(): 3000.0, days[1].isoformat(): 2000.0}
    data = {"days": {"distance": distance}, "sessions": {}, "hours": {}, "workouts": {}}
    hass_storage[key] = {"version": 1, "minor_version": 4, "key": key, "data": data}
    starts = [dt_util.start_of_local_day(day) for day in days]
    old = {
        "distance": ("m", [(3000.0, 3000.0), (2000.0, 5000.0)]),
        "sleep_minutes": ("min", [(420.0, 420.0), (480.0, 900.0)]),
    }
    for key, (unit, values) in old.items():
        async_add_external_statistics(
            hass,
            {
                "statistic_id": statistic_id(entry, key),
                "source": DOMAIN,
                "name": f"Owen's Pixel {key}",
                "unit_of_measurement": unit,
                "unit_class": None,
                "has_sum": True,
                "mean_type": StatisticMeanType.NONE,
            },
            [
                {"start": start, "state": state, "sum": total}
                for start, (state, total) in zip(starts, values, strict=True)
            ],
        )
    await async_wait_recording_done(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await async_wait_recording_done(hass)

    distance = _rows(hass, entry, "distance", "day", {"state", "sum"})
    assert [(r["state"], r["sum"]) for r in distance] == [(3.0, 3.0), (2.0, 5.0)]
    sleep = _rows(hass, entry, "sleep_minutes", "day", {"state", "sum"})
    assert [(r["state"], r["sum"]) for r in sleep] == [(7.0, 7.0), (8.0, 15.0)]
    units = {
        i["statistic_id"]: i["statistics_unit_of_measurement"]
        for i in await async_list_statistic_ids(hass)
    }
    assert units[statistic_id(entry, "distance")] == "km"
    assert units[statistic_id(entry, "sleep_minutes")] == "h"
    assert entry.runtime_data.history.ledger.units == 3
    # And a period from the second day counts it: the evening rows came with the move.
    assert await _change(hass, entry, "distance", starts[1]) == 2.0
    # The webhook opens once that is done.
    payload = _backfill(daily_totals=[{"date": _today().isoformat(), "distance_meters": 1500}])
    assert await _post(await hass_client_no_auth(), payload) == 200
    await async_wait_recording_done(hass)
    assert _rows(hass, entry, "distance", "day", {"sum"})[-1]["sum"] == 6.5

    # A second start leaves them as they are.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await async_wait_recording_done(hass)
    distance = _rows(hass, entry, "distance", "day", {"sum"})
    assert [r["sum"] for r in distance] == [3.0, 5.0, 6.5], "not divided a second time"


async def test_days_from_before_the_ledger_are_added_and_later_sums_move_up(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """P2-28: a day the recorder has no row for counts, however old it is."""
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    day = {n: (_today() - timedelta(days=n)).isoformat() for n in (600, 500, 450, 1)}
    first = _backfill(
        daily_totals=[{"date": day[500], "steps": 100}, {"date": day[1], "steps": 200}]
    )
    assert await _post(client, first) == 200
    await async_wait_recording_done(hass)
    ledger = entry.runtime_data.history.ledger
    assert ledger.carried == {"steps": 100.0}

    # Older than anything kept, and a gap between two rows: both before kept_from.
    older = _backfill(
        daily_totals=[{"date": day[600], "steps": 50}, {"date": day[450], "steps": 30}],
        sleep=[{"session_end_time": f"{day[600]}T06:00:00Z", "duration_seconds": 600, "uuid": "x"}],
    )
    assert await _post(client, older) == 200
    await async_wait_recording_done(hass)
    steps = _rows(hass, entry, "steps", "day", {"state", "sum"})
    assert [(r["state"], r["sum"]) for r in steps] == [
        (50.0, 50.0),
        (100.0, 150.0),
        (30.0, 180.0),
        (200.0, 380.0),
    ]
    sleep = _rows(hass, entry, "sleep_minutes", "day", {"sum"})
    assert [r["sum"] * 60 for r in sleep] == pytest.approx([10.0]), (
        "in hours, the ledger in minutes"
    )
    assert ledger.carried == {"steps": 180.0, "sleep_minutes": 10.0}

    # The rows the ledger writes later count them in.
    assert await _post(client, _backfill(daily_totals=[{"date": day[1], "steps": 250}])) == 200
    await async_wait_recording_done(hass)
    assert _rows(hass, entry, "steps", "day", {"sum"})[-1]["sum"] == 430.0

    # Sent again, they have rows now, so nothing moves.
    assert await _post(client, older) == 200
    await async_wait_recording_done(hass)
    assert [r["sum"] for r in _rows(hass, entry, "steps", "day", {"sum"})] == [
        50.0,
        150.0,
        180.0,
        430.0,
    ]
    assert ledger.carried == {"steps": 180.0, "sleep_minutes": 10.0}


async def test_hours_from_before_the_ledger_are_written_where_none_are(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _entry(hass)
    client = await hass_client_no_auth()
    oldest = (_today() - timedelta(days=600)).isoformat()
    old = (_today() - timedelta(days=500)).isoformat()
    recent = (_today() - timedelta(days=1)).isoformat()
    first = _backfill(daily_totals=[{"date": oldest, "steps": 1}, {"date": recent, "steps": 1}])
    assert await _post(client, first) == 200
    await async_wait_recording_done(hass)
    assert entry.runtime_data.history.ledger.kept_from is not None

    heart = [{"bpm": 60, "time": f"{old}T08:10:00Z"}, {"bpm": 70, "time": f"{old}T08:50:00Z"}]
    assert await _post(client, _backfill(heart_rate=heart)) == 200
    await async_wait_recording_done(hass)
    rows = _rows(hass, entry, "heart_rate", "hour", {"mean", "min", "max"})
    assert [(r["mean"], r["min"], r["max"]) for r in rows] == [(65.0, 60.0, 70.0)]

    # The hour has a row now; a later delivery of it is left alone.
    again = [{"bpm": 100, "time": f"{old}T08:30:00Z"}]
    assert await _post(client, _backfill(heart_rate=again)) == 200
    await async_wait_recording_done(hass)
    rows = _rows(hass, entry, "heart_rate", "hour", {"mean", "min", "max"})
    assert [(r["mean"], r["min"], r["max"]) for r in rows] == [(65.0, 60.0, 70.0)]


async def test_a_ledger_that_was_pruned_is_written(
    hass: HomeAssistant, hass_client_no_auth, hass_storage, freezer
) -> None:
    """A payload that changes nothing still writes the ledger when the prune let a day go."""
    freezer.move_to("2026-10-14 10:00:00+00:00")
    old = (_today() - timedelta(days=500)).isoformat()
    recent = (_today() - timedelta(days=1)).isoformat()
    key = f"{DOMAIN}.ledger_entry.history"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 2,
        "key": key,
        "data": {
            "days": {"steps": {old: 100.0, recent: 200.0}},
            "sessions": {},
            "hours": {},
            "workouts": {},
        },
    }
    await async_process_ha_core_config(hass, {"time_zone": "Europe/Amsterdam"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="ledger_entry",
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
    assert "kept_from" not in hass_storage[key]["data"]

    client = await hass_client_no_auth()
    same = _backfill(daily_totals=[{"date": recent, "steps": 200}])
    assert await _post(client, same) == 200
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()

    saved = hass_storage[key]
    assert (saved["version"], saved["minor_version"]) == (1, 4)
    assert saved["data"]["days"] == {"steps": {recent: 200.0}}
    assert saved["data"]["carried"] == {"steps": 100.0}
    assert saved["data"]["kept_from"] == (_today() - timedelta(days=401)).isoformat()
