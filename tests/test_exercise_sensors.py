"""The exercise sensors through the webhook: Last workout, Exercise today and yesterday.

A sync only carries what is new, so the day sums have to come from the sessions the
ledger kept; these tests send syncs the way the app does, and move the clock.
"""

import json
from datetime import UTC, datetime
from http import HTTPStatus

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
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
from custom_components.life_dashboard.payload import (
    APP_PACKAGE,
    SENSOR_SPECS,
    SIGNATURE_HEADER,
    signature_for,
)
from custom_components.life_dashboard.workout_types import WORKOUT_TYPES
from custom_components.life_dashboard.writeback_queue import WRITEBACK_TYPES

WEBHOOK_ID = "a" * 64
SECRET = "b" * 64
URL = f"/api/webhook/{WEBHOOK_ID}"
ENTRY_ID = "01EXERCISESENSORS0000000000"
LAST = "sensor.owen_s_pixel_last_workout"
TODAY = "sensor.owen_s_pixel_exercise_today"
YESTERDAY = "sensor.owen_s_pixel_exercise_yesterday"

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


def _record(type_: str, start: str, end: str, minutes: int, **extra) -> dict:
    return {
        "type": type_,
        "start_time": f"2026-10-04T{start}:00Z",
        "end_time": f"2026-10-04T{end}:00Z",
        "duration_seconds": minutes * 60,
        **extra,
    }


# A phone's Sunday: two walks it noticed itself, a run, and a long ride.
WALK_1 = _record("79", "05:10", "05:22", 12, uuid="walk-1", source="com.google.android.gms")
WALK_2 = _record("79", "06:00", "06:15", 15, uuid="walk-2", source="com.google.android.gms")
RUN = _record("56", "07:00", "08:00", 60, uuid="run", source="com.google.android.apps.fitness")
RIDE = _record("8", "09:00", "10:50", 110, uuid="ride", source="com.google.android.apps.fitness")


def _health(*records: dict, timestamp: str = "2026-10-04T12:00:00Z", **extra) -> dict:
    payload = {
        "timestamp": timestamp,
        "app_version": "1.23.0",
        "source": "health_connect",
        "exercise": list(records),
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


def _minutes(hass: HomeAssistant, entity_id: str) -> list[tuple[str, int]]:
    return [
        (row["type"], row["minutes"]) for row in hass.states.get(entity_id).attributes["sessions"]
    ]


# --- The sensors appear with the first workout --------------------------------


async def test_no_workout_no_sensor(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    """A phone that never sends exercise gets none of the three."""
    client = await hass_client_no_auth()
    await _post(hass, client, _health(heart_rate=[{"bpm": 61, "time": "2026-10-04T11:00:00Z"}]))
    ids = set(hass.states.async_entity_ids("sensor"))
    assert not {LAST, TODAY, YESTERDAY} & ids


async def test_a_sync_with_workouts(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(WALK_1, WALK_2, RUN, RIDE))

    last = hass.states.get(LAST)
    assert last.state == "biking"
    assert last.attributes["device_class"] == "enum"
    assert last.attributes["options"] == list(WORKOUT_TYPES)
    assert last.attributes["start"] == "2026-10-04T11:00:00+02:00"
    assert last.attributes["end"] == "2026-10-04T12:50:00+02:00"
    assert last.attributes["duration_minutes"] == 110
    assert last.attributes["type_code"] == "8"
    assert last.attributes["source"] == "com.google.android.apps.fitness"
    assert last.attributes["uuid"] == "ride"
    assert last.attributes["measured_at"] == "2026-10-04T10:50:00+00:00"
    assert "unit_of_measurement" not in last.attributes

    today = hass.states.get(TODAY)
    assert today.state == "197"
    assert today.attributes["unit_of_measurement"] == "min"
    assert today.attributes["device_class"] == "duration"
    assert today.attributes["date"] == "2026-10-04"
    assert today.attributes["sessions"] == [
        {
            "type": "walking",
            "start": "2026-10-04T07:10:00+02:00",
            "end": "2026-10-04T07:22:00+02:00",
            "minutes": 12,
        },
        {
            "type": "walking",
            "start": "2026-10-04T08:00:00+02:00",
            "end": "2026-10-04T08:15:00+02:00",
            "minutes": 15,
        },
        {
            "type": "running",
            "start": "2026-10-04T09:00:00+02:00",
            "end": "2026-10-04T10:00:00+02:00",
            "minutes": 60,
        },
        {
            "type": "biking",
            "start": "2026-10-04T11:00:00+02:00",
            "end": "2026-10-04T12:50:00+02:00",
            "minutes": 110,
        },
    ]

    yesterday = hass.states.get(YESTERDAY)
    assert yesterday.state == "0"
    assert yesterday.attributes["date"] == "2026-10-03"
    assert yesterday.attributes["sessions"] == []


async def test_the_entity_ids_and_the_specs(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(RUN))
    assert hass.states.get(LAST) is not None
    assert hass.states.get(TODAY) is not None
    assert hass.states.get(YESTERDAY) is not None
    for key in ("last_workout", "exercise_today", "exercise_yesterday"):
        assert SENSOR_SPECS[key].state_class is None


async def test_syncs_add_up_and_repeats_change_nothing(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """The morning, then the afternoon with the morning's records sent again."""
    client = await hass_client_no_auth()
    await _post(hass, client, _health(WALK_1, WALK_2))
    assert hass.states.get(TODAY).state == "27"

    await _post(hass, client, _health(RUN, WALK_2))
    assert hass.states.get(TODAY).state == "87"
    assert len(hass.states.get(TODAY).attributes["sessions"]) == 3

    await _post(hass, client, _health(WALK_1, WALK_2, RUN))
    assert hass.states.get(TODAY).state == "87"
    assert loaded.runtime_data.history.ledger.workouts.keys() == {"walk-1", "walk-2", "run"}


async def test_an_edited_session_replaces_itself(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """A watch that revises a run as it uploads: same uuid, longer."""
    client = await hass_client_no_auth()
    await _post(hass, client, _health(_record("56", "07:00", "07:20", 20, uuid="run")))
    assert hass.states.get(TODAY).state == "20"
    await _post(hass, client, _health(_record("56", "07:00", "08:00", 60, uuid="run")))
    assert hass.states.get(TODAY).state == "60"
    assert len(hass.states.get(TODAY).attributes["sessions"]) == 1


async def test_an_older_batch_does_not_move_the_last_workout_back(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """The outbox delivers the morning after the afternoon."""
    client = await hass_client_no_auth()
    await _post(hass, client, _health(RIDE))
    await _post(hass, client, _health(WALK_1, timestamp="2026-10-04T12:05:00Z"))
    assert hass.states.get(LAST).state == "biking"
    # But the walk is still a walk of today.
    assert hass.states.get(TODAY).state == "122"


async def test_an_iphone(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    client = await hass_client_no_auth()
    walk = _record("walking", "07:00", "07:30", 30, uuid="w")
    await _post(
        hass,
        client,
        _health(
            walk,
            _record("traditional_strength_training", "08:00", "08:45", 45, uuid="s"),
            source="healthkit_ios",
        ),
    )
    assert hass.states.get(LAST).state == "strength_training"
    assert hass.states.get(LAST).attributes["type_code"] == "traditional_strength_training"
    assert [kind for kind, _ in _minutes(hass, TODAY)] == ["walking", "strength_training"]
    assert hass.states.get(TODAY).state == "75"


async def test_a_type_nobody_knows_is_other(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(_record("9999", "07:00", "07:30", 30, uuid="new")))
    assert hass.states.get(LAST).state == "other"
    assert hass.states.get(LAST).attributes["type_code"] == "9999"
    assert hass.states.get(TODAY).state == "30"


async def test_what_the_app_wrote_itself_is_not_a_workout(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(_record("56", "07:00", "08:00", 60, source=APP_PACKAGE)))
    assert hass.states.get(LAST) is None
    assert hass.states.get(TODAY) is None


async def test_a_backfill_of_the_past_makes_no_day_sensor(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """A year of exercise is history for the statistics; it is not today or yesterday."""
    client = await hass_client_no_auth()
    old = {
        "type": "56",
        "start_time": "2026-08-01T07:00:00Z",
        "end_time": "2026-08-01T08:00:00Z",
        "duration_seconds": 3600,
        "uuid": "old",
    }
    await _post(hass, client, _health(old, backfill=True))
    assert hass.states.get(TODAY) is None
    assert loaded.runtime_data.history.ledger.workouts == {}
    # The statistic still has it.
    assert loaded.runtime_data.history.ledger.day_totals("exercise_minutes") == {"2026-08-01": 60.0}


# --- Midnight --------------------------------------------------------------


async def test_a_session_across_midnight_counts_on_both_days(
    hass: HomeAssistant, hass_client_no_auth, loaded, freezer
) -> None:
    """23:30 to 00:30 in Amsterdam, synced at 00:40: half an hour on each day."""
    freezer.move_to(datetime(2026, 10, 3, 22, 40, tzinfo=UTC))
    client = await hass_client_no_auth()
    late = {
        "type": "56",
        "start_time": "2026-10-03T21:30:00Z",
        "end_time": "2026-10-03T22:30:00Z",
        "duration_seconds": 3600,
        "uuid": "late",
    }
    await _post(hass, client, _health(late, timestamp="2026-10-03T22:40:00Z"))
    assert hass.states.get(TODAY).attributes["date"] == "2026-10-04"
    assert hass.states.get(TODAY).state == "30"
    assert hass.states.get(YESTERDAY).state == "30"
    assert _minutes(hass, TODAY) == _minutes(hass, YESTERDAY) == [("running", 30)]


async def test_yesterday_follows_at_midnight_without_a_sync(
    hass: HomeAssistant, hass_client_no_auth, loaded, freezer
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(WALK_2, RUN))
    assert hass.states.get(TODAY).state == "75"
    assert hass.states.get(YESTERDAY).state == "0"

    # 00:00:05 on Monday in Amsterdam: Sunday's sessions are yesterday's now.
    midnight = datetime(2026, 10, 4, 22, 0, 5, tzinfo=UTC)
    freezer.move_to(midnight)
    async_fire_time_changed(hass, midnight)
    await hass.async_block_till_done()

    assert hass.states.get(TODAY).state == "0"
    assert hass.states.get(TODAY).attributes["date"] == "2026-10-05"
    assert hass.states.get(TODAY).attributes["sessions"] == []
    assert hass.states.get(YESTERDAY).state == "75"
    assert hass.states.get(YESTERDAY).attributes["date"] == "2026-10-04"
    assert _minutes(hass, YESTERDAY) == [("walking", 15), ("running", 60)]


async def test_the_first_sync_after_midnight_rolls_over_too(
    hass: HomeAssistant, hass_client_no_auth, loaded, freezer
) -> None:
    """With the timer missed, as when Home Assistant was down at midnight."""
    client = await hass_client_no_auth()
    await _post(hass, client, _health(RUN))
    freezer.move_to(datetime(2026, 10, 5, 6, 0, tzinfo=UTC))
    walk = {
        "type": "79",
        "start_time": "2026-10-05T05:00:00Z",
        "end_time": "2026-10-05T05:20:00Z",
        "duration_seconds": 1200,
        "uuid": "mon",
    }
    await _post(hass, client, _health(walk, timestamp="2026-10-05T06:00:00Z"))
    assert hass.states.get(TODAY).state == "20"
    assert hass.states.get(YESTERDAY).state == "60"
    assert hass.states.get(LAST).state == "walking"


async def test_two_days_later_nothing_is_left_to_count(
    hass: HomeAssistant, hass_client_no_auth, loaded, freezer
) -> None:
    """The sensors read 0, not the old figure, while Last workout keeps its value."""
    client = await hass_client_no_auth()
    await _post(hass, client, _health(RUN))

    freezer.move_to(datetime(2026, 10, 6, 22, 1, tzinfo=UTC))
    async_fire_time_changed(hass, datetime(2026, 10, 6, 22, 1, tzinfo=UTC))
    await hass.async_block_till_done()
    await _post(hass, client, {"timestamp": "2026-10-06T22:01:00Z", "source": "health_connect"})
    assert hass.states.get(TODAY).state == "0"
    assert hass.states.get(YESTERDAY).state == "0"
    assert hass.states.get(LAST).state == "running"


# --- A restart, and the option ------------------------------------------------


async def test_a_restart_reads_the_sensors_from_what_was_kept(
    hass: HomeAssistant, hass_client_no_auth, loaded, freezer
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(WALK_2, RUN))
    assert hass.states.get(TODAY).state == "75"

    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()
    # Home Assistant comes back on Monday morning; nothing has synced.
    freezer.move_to(datetime(2026, 10, 5, 6, 0, tzinfo=UTC))
    assert await hass.config_entries.async_setup(loaded.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(TODAY).state == "0"
    assert hass.states.get(YESTERDAY).state == "75"
    assert _minutes(hass, YESTERDAY) == [("walking", 15), ("running", 60)]


async def test_last_workout_comes_back_after_a_restart(
    hass: HomeAssistant,
    hass_client_no_auth,
    entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Restored with its attributes, and an older batch still cannot overwrite it."""
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_last_workout",
        suggested_object_id="owen_s_pixel_last_workout",
        config_entry=entry,
    )
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(
                    LAST,
                    "running",
                    attributes={
                        "measured_at": "2026-10-04T08:00:00+00:00",
                        "start": "2026-10-04T09:00:00+02:00",
                        "end": "2026-10-04T10:00:00+02:00",
                        "duration_minutes": 60,
                        "type_code": "56",
                        "friendly_name": "Owen's Pixel Last workout",
                    },
                ),
                {"native_value": "running", "native_unit_of_measurement": None},
            ),
        ),
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(LAST)
    assert state.state == "running"
    assert state.attributes["type_code"] == "56"
    assert state.attributes["duration_minutes"] == 60

    client = await hass_client_no_auth()
    await _post(hass, client, _health(WALK_1))
    assert hass.states.get(LAST).state == "running"


async def _set_minimum(hass: HomeAssistant, entry: MockConfigEntry, minutes: int) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"min_workout_minutes": minutes, **{k: {} for k in _slots()}}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


def _slots() -> list[str]:
    return list(WRITEBACK_TYPES)


async def test_the_shortest_workout_option(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(WALK_1, WALK_2, RUN))
    assert hass.states.get(TODAY).state == "87"

    await _set_minimum(hass, loaded, 15)
    assert loaded.options["min_workout_minutes"] == 15
    # The reload reads the sessions again: the 12 minute walk is out, nothing was lost.
    assert hass.states.get(TODAY).state == "75"
    assert _minutes(hass, TODAY) == [("walking", 15), ("running", 60)]

    await _set_minimum(hass, loaded, 0)
    assert hass.states.get(TODAY).state == "87"


async def test_the_option_applies_to_the_next_last_workout(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(RUN))
    await _set_minimum(hass, loaded, 20)
    client = await hass_client_no_auth()

    # A short walk after the run: not a workout now, so the run stays the last one.
    walk = _record("79", "11:00", "11:12", 12, uuid="short")
    await _post(hass, client, _health(walk, timestamp="2026-10-04T12:05:00Z"))
    assert hass.states.get(LAST).state == "running"
    assert hass.states.get(TODAY).state == "60"

    long_walk = _record("79", "11:20", "11:50", 30, uuid="long")
    await _post(hass, client, _health(long_walk, timestamp="2026-10-04T12:10:00Z"))
    assert hass.states.get(LAST).state == "walking"
    assert hass.states.get(TODAY).state == "90"


async def test_the_option_leaves_the_exercise_statistic_alone(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """exercise_minutes in long-term statistics counts every session, short or not."""
    await _set_minimum(hass, loaded, 30)
    client = await hass_client_no_auth()
    await _post(hass, client, _health(WALK_1, WALK_2, RUN))
    assert hass.states.get(TODAY).state == "60"
    assert loaded.runtime_data.history.ledger.day_totals("exercise_minutes") == {"2026-10-04": 87.0}


async def test_the_form_holds_the_current_minimum(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    marker = next(m for m in result["data_schema"].schema if m == "min_workout_minutes")
    assert marker.default() == 0
    hass.config_entries.options.async_abort(result["flow_id"])

    await _set_minimum(hass, loaded, 25)
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    marker = next(m for m in result["data_schema"].schema if m == "min_workout_minutes")
    assert marker.description == {"suggested_value": 25}


async def test_the_minimum_is_bounded(hass: HomeAssistant, loaded) -> None:
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    for bad in (-1, 181):
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(
                result["flow_id"], {"min_workout_minutes": bad, **{k: {} for k in _slots()}}
            )


# --- Storage -----------------------------------------------------------------


def _ledger_key() -> str:
    return f"{DOMAIN}.{ENTRY_ID}.history"


# What 0.9.0 wrote for a phone that had synced steps, sleep and exercise.
OLD_LEDGER = {
    "days": {"steps": {"2026-10-02": 7000.0, "2026-10-03": 9000.0}},
    "sessions": {
        "exercise_minutes": {"old-run": ["2026-10-03", 45.0]},
        "sleep_minutes": {"night": ["2026-10-03", 450.0]},
    },
    "hours": {
        "heart_rate": {
            "2026-10-03T10:00:00+00:00": {"n": 3, "mean": 61.0, "min": 58.0, "max": 70.0}
        }
    },
}


async def test_a_ledger_of_0_9_0_is_taken_over_whole(
    hass: HomeAssistant, hass_storage, entry: MockConfigEntry, hass_client_no_auth
) -> None:
    hass_storage[_ledger_key()] = {
        "version": 1,
        "minor_version": 1,
        "key": _ledger_key(),
        "data": OLD_LEDGER,
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ledger = entry.runtime_data.history.ledger
    assert ledger.days == OLD_LEDGER["days"]
    assert ledger.sessions == OLD_LEDGER["sessions"]
    assert ledger.hours["heart_rate"]["2026-10-03T10:00:00+00:00"].count == 3
    assert ledger.workouts == {}
    # Migrated on the spot: the file says what it is now, and keeps everything it had.
    saved = hass_storage[_ledger_key()]
    assert (saved["version"], saved["minor_version"]) == (1, 4)
    assert saved["data"]["days"] == OLD_LEDGER["days"]
    assert saved["data"]["sessions"] == OLD_LEDGER["sessions"]
    assert saved["data"]["workouts"] == {}
    # No workouts, so no sensors yet; the statistics' own history is still there.
    assert hass.states.get(TODAY) is None

    client = await hass_client_no_auth()
    await _post(hass, client, _health(RUN))
    assert hass.states.get(TODAY).state == "60"
    ledger = entry.runtime_data.history.ledger
    assert ledger.days["steps"] == OLD_LEDGER["days"]["steps"]
    assert ledger.sessions["exercise_minutes"]["old-run"] == ["2026-10-03", 45.0]


async def test_workouts_are_written_and_read_back(
    hass: HomeAssistant, hass_storage, hass_client_no_auth, loaded
) -> None:
    client = await hass_client_no_auth()
    await _post(hass, client, _health(RUN, WALK_2))
    # Written on unload, which is when a restart flushes it.
    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()

    saved = hass_storage[_ledger_key()]
    assert (saved["version"], saved["minor_version"]) == (1, 4)
    assert set(saved["data"]["workouts"]) == {"run", "walk-2"}
    assert saved["data"]["workouts"]["run"] == {
        "type": "56",
        "start": "2026-10-04T07:00:00+00:00",
        "end": "2026-10-04T08:00:00+00:00",
        "seconds": 3600.0,
        "source": "com.google.android.apps.fitness",
    }

    assert await hass.config_entries.async_setup(loaded.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(TODAY).state == "75"


async def test_a_ledger_that_is_already_current_is_left_alone(
    hass: HomeAssistant, hass_storage, entry: MockConfigEntry
) -> None:
    ledger = {
        **OLD_LEDGER,
        "workouts": {
            "x": {
                "type": "56",
                "start": "2026-10-04T07:00:00+00:00",
                "end": "2026-10-04T08:00:00+00:00",
                "seconds": 3600.0,
            }
        },
    }
    hass_storage[_ledger_key()] = {
        "version": 1,
        "minor_version": 2,
        "key": _ledger_key(),
        "data": ledger,
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert set(entry.runtime_data.history.ledger.workouts) == {"x"}
    assert hass.states.get(TODAY).state == "60"


async def test_a_release_before_the_workouts_still_reads_the_new_file(
    hass: HomeAssistant, hass_storage, hass_client_no_auth, loaded
) -> None:
    """A downgrade: 0.9.0 opens the file as version 1 and takes the keys it knows."""
    client = await hass_client_no_auth()
    await _post(hass, client, _health(RUN))
    assert await hass.config_entries.async_unload(loaded.entry_id)

    data = await Store(hass, 1, _ledger_key()).async_load()
    assert Ledger.from_dict({k: v for k, v in data.items() if k != "workouts"}).day_totals(
        "exercise_minutes"
    ) == {"2026-10-04": 60.0}


# --- Workout details -----------------------------------------------------------


def _heart_rate(record: str, start: str, seconds: int, bpm: int, source: str) -> list[dict]:
    """One sample per second from 2026-10-04 `start` UTC, as a watch writes them."""
    first = datetime.fromisoformat(f"2026-10-04T{start}:00+00:00")
    samples = []
    for offset in range(seconds):
        millis = int(first.timestamp() * 1000) + offset * 1000
        moment = datetime.fromtimestamp(millis / 1000, UTC)
        samples.append(
            {
                "bpm": bpm,
                "time": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "uuid": f"{record}#{millis}",
                "source": source,
            }
        )
    return samples


def _calories(uuid: str, start: str, end: str, kcal: float, source: str) -> dict:
    return {
        "calories": kcal,
        "start_time": f"2026-10-04T{start}:00Z",
        "end_time": f"2026-10-04T{end}:00Z",
        "uuid": uuid,
        "source": source,
    }


FITNESS = "com.google.android.apps.fitness"


async def test_details_that_come_after_the_workout_fill_it_in(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """The run is synced first; its heart rate and calories come with the next sync."""
    client = await hass_client_no_auth()
    await _post(hass, client, _health(RUN))
    assert "heart_rate_avg" not in hass.states.get(LAST).attributes
    measured_at = hass.states.get(LAST).attributes["measured_at"]

    later = {
        "timestamp": "2026-10-04T12:15:00Z",
        "app_version": "1.26.0",
        "source": "health_connect",
        "sequence": 2,
        "heart_rate": _heart_rate("hr-1", "07:10", 120, 150, FITNESS),
        "active_calories": [_calories("cal-1", "07:00", "08:00", 480.0, FITNESS)],
    }
    await _post(hass, client, later)

    last = hass.states.get(LAST)
    assert last.state == "running"
    assert last.attributes["heart_rate_avg"] == 150
    assert last.attributes["active_calories"] == 480
    assert last.attributes["measured_at"] == measured_at
    assert last.attributes["uuid"] == "run"
    run = hass.states.get(TODAY).attributes["sessions"][0]
    assert run["heart_rate_avg"] == 150
    assert run["active_calories"] == 480
    assert hass.states.get(TODAY).state == "60"

    # The same sync again changes nothing, and a deletion takes the calories out.
    await _post(hass, client, later)
    assert hass.states.get(LAST).attributes["active_calories"] == 480
    await _post(
        hass,
        client,
        {
            "timestamp": "2026-10-04T12:30:00Z",
            "app_version": "1.26.0",
            "source": "health_connect",
            "sequence": 3,
            "deleted_records": [{"type": "active_calories", "uuid": "cal-1"}],
        },
    )
    assert "active_calories" not in hass.states.get(LAST).attributes
    assert hass.states.get(LAST).attributes["heart_rate_avg"] == 150


async def test_a_ride_with_its_records_in_the_same_sync(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    client = await hass_client_no_auth()
    distance = {
        "meters": 44000.0,
        "start_time": "2026-10-04T09:00:00Z",
        "end_time": "2026-10-04T10:50:00Z",
        "uuid": "ride-distance",
        "source": FITNESS,
    }
    await _post(hass, client, _health(RIDE, distance=[distance]))
    last = hass.states.get(LAST)
    assert last.attributes["distance_km"] == 44.0
    assert last.attributes["speed_kmh"] == 24.0
    assert "pace" not in last.attributes


async def test_a_backfilled_workout_reads_the_records_of_its_own_payload(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """Older than the workouts that are kept, so only its payload can tell."""
    client = await hass_client_no_auth()
    old_run = {
        "type": "56",
        "start_time": "2026-09-20T07:00:00Z",
        "end_time": "2026-09-20T07:30:00Z",
        "duration_seconds": 1800,
        "uuid": "old-run",
        "source": FITNESS,
    }
    old_steps = {
        "count": 4500,
        "start_time": "2026-09-20T07:00:00Z",
        "end_time": "2026-09-20T07:30:00Z",
        "uuid": "old-steps",
        "source": FITNESS,
    }
    await _post(hass, client, _health(old_run, steps=[old_steps], backfill=True))
    last = hass.states.get(LAST)
    assert last.attributes["steps"] == 4500
    assert last.attributes["cadence_spm"] == 150
    assert loaded.runtime_data.history.ledger.workouts == {}


async def test_the_details_come_back_after_a_restart(
    hass: HomeAssistant, entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_last_workout",
        suggested_object_id="owen_s_pixel_last_workout",
        config_entry=entry,
    )
    details = {
        "active_calories": 480,
        "distance_km": 10.0,
        "pace_min_per_km": 6.0,
        "pace": "6:00",
        "heart_rate_avg": 150,
        "heart_rate_min": 120,
        "heart_rate_max": 175,
        "steps": 9000,
        "cadence_spm": 150,
    }
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(
                    LAST,
                    "running",
                    attributes={
                        "measured_at": "2026-10-04T08:00:00+00:00",
                        "start": "2026-10-04T09:00:00+02:00",
                        "end": "2026-10-04T10:00:00+02:00",
                        "duration_minutes": 60,
                        "type_code": "56",
                        **details,
                    },
                ),
                {"native_value": "running", "native_unit_of_measurement": None},
            ),
        ),
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    attributes = hass.states.get(LAST).attributes
    assert {key: attributes.get(key) for key in details} == details


async def test_a_ledger_of_0_10_1_gets_the_details(
    hass: HomeAssistant, hass_storage, entry: MockConfigEntry, hass_client_no_auth
) -> None:
    """1.3 to 1.4: the kept run stays, and reads its records once they come."""
    run_row = {
        "type": "56",
        "start": "2026-10-04T07:00:00+00:00",
        "end": "2026-10-04T08:00:00+00:00",
        "seconds": 3600.0,
    }
    hass_storage[_ledger_key()] = {
        "version": 1,
        "minor_version": 3,
        "key": _ledger_key(),
        "data": {**OLD_LEDGER, "workouts": {"run": run_row}},
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    saved = hass_storage[_ledger_key()]
    assert (saved["version"], saved["minor_version"]) == (1, 4)
    assert saved["data"]["workouts"] == {"run": run_row}
    assert saved["data"]["activity"] == {}
    assert hass.states.get(TODAY).state == "60"

    client = await hass_client_no_auth()
    await _post(
        hass,
        client,
        _health(heart_rate=_heart_rate("hr-1", "07:30", 60, 140, FITNESS)) | {"exercise": []},
    )
    assert hass.states.get(TODAY).attributes["sessions"][0]["heart_rate_avg"] == 140


async def test_a_release_before_the_details_still_reads_the_new_file(
    hass: HomeAssistant, hass_storage, hass_client_no_auth, loaded
) -> None:
    """A downgrade to 0.10.1 takes the keys it knows and leaves the details out."""
    client = await hass_client_no_auth()
    await _post(
        hass, client, _health(RUN, heart_rate=_heart_rate("hr-1", "07:30", 60, 140, FITNESS))
    )
    assert await hass.config_entries.async_unload(loaded.entry_id)
    data = await Store(hass, 1, _ledger_key()).async_load()
    assert data["activity"]["heart_rate"]
    known = {k: v for k, v in data.items() if k in {"days", "sessions", "hours", "workouts"}}
    old = Ledger.from_dict(known)
    assert set(old.workouts) == {"run"}
    assert old.day_totals("exercise_minutes") == {"2026-10-04": 60.0}
