"""Test the backfill: the recorder's past as readings for the phone.

The button and the service read the recorder, not the statistics, and form readings
with the same id rule as the listeners, so what the live path already queued is
recognised and a second press changes nothing.
"""

import json
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.life_dashboard.const import (
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    URL_CHOICE_INTERNAL,
)
from custom_components.life_dashboard.payload import SIGNATURE_HEADER, signature_for
from custom_components.life_dashboard.writeback_queue import epoch_ms


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Overrides the conftest fixture: the recorder's database has to exist before
    the hass fixture starts, and enable_custom_integrations pulls hass in."""
    yield


WEBHOOK_ID = "a" * 64
SECRET = "b" * 64
URL = f"/api/webhook/{WEBHOOK_ID}"
WEIGHT = "sensor.scale_weight"
WEIGHED_AT = "sensor.scale_last_measurement_time"
SYSTOLIC = "sensor.omron_systolic"
DIASTOLIC = "sensor.omron_diastolic"
START = datetime(2026, 9, 27, 6, 30, tzinfo=UTC)
BUTTON = "button.owen_s_pixel_send_history_to_phone"


def _entry(options: dict, *, webhook_id: str = WEBHOOK_ID) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Owen's Pixel",
        unique_id=webhook_id,
        data={
            CONF_WEBHOOK_ID: webhook_id,
            CONF_SECRET: SECRET,
            CONF_URL_CHOICE: URL_CHOICE_INTERNAL,
        },
        options=options,
    )


async def _load(hass: HomeAssistant, options: dict, **kwargs) -> MockConfigEntry:
    entry = _entry(options, **kwargs)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _set(hass: HomeAssistant, entity_id: str, value, unit: str | None = "kg", **attrs):
    attributes = {**attrs}
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    hass.states.async_set(entity_id, str(value), attributes)
    await hass.async_block_till_done()


async def _recorded(hass: HomeAssistant) -> None:
    await async_wait_recording_done(hass)


async def _ask(client, types=("weight",), *, ack=None, failed=None) -> dict:
    block: dict = {"protocol": 1, "types": list(types), "history": True}
    if ack is not None:
        block["ack"] = ack
    if failed is not None:
        block["failed"] = failed
    payload = {
        "timestamp": "2026-09-27T06:35:00Z",
        "app_version": "1.20.0",
        "source": "health_connect",
        "writeback": block,
    }
    body = json.dumps(payload).encode()
    response = await client.post(
        URL,
        data=body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature_for(SECRET, body)},
    )
    assert response.status == HTTPStatus.OK
    return (await response.json())["writeback"]


def _id(entity_id: str, moment: datetime) -> str:
    return f"{entity_id}@{epoch_ms(moment)}"


@pytest.fixture
async def amsterdam(hass: HomeAssistant, freezer) -> HomeAssistant:
    freezer.move_to(START - timedelta(days=3))
    await async_process_ha_core_config(hass, {"time_zone": "Europe/Amsterdam"})
    return hass


async def _three_weighings(hass: HomeAssistant, freezer) -> list[datetime]:
    """Three mornings in the recorder, an hour past the day boundary each.

    The entity is created first, with the 0 a helper starts at: that row is the
    creation state and no measurement, live or from the recorder.
    """
    freezer.move_to(START - timedelta(days=3))
    await _set(hass, WEIGHT, 0.0, "kg", device_class="weight")
    moments = []
    for day, value in enumerate((80.0, 80.5, 81.0)):
        moment = START - timedelta(days=3 - day) + timedelta(hours=1)
        freezer.move_to(moment)
        await _set(hass, WEIGHT, value, "kg", device_class="weight")
        moments.append(moment)
    await _recorded(hass)
    freezer.move_to(START)
    return moments


# --- The button --------------------------------------------------------------


async def test_the_button_exists_only_with_a_mapping(amsterdam: HomeAssistant) -> None:
    hass = amsterdam
    entry = await _load(hass, {})
    assert hass.states.get(BUTTON) is None
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    await _load(hass, {"writeback": {"weight": {"entity": WEIGHT}}}, webhook_id="c" * 64)
    assert hass.states.get(BUTTON) is not None


async def test_the_button_queues_the_last_thirty_days(
    amsterdam: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """Weighings from before the mapping was set up reach the phone through the button."""
    hass = amsterdam
    moments = await _three_weighings(hass, freezer)
    await _load(hass, {"writeback": {"weight": {"entity": WEIGHT}}})

    client = await hass_client_no_auth()
    assert (await _ask(client))["pending"] == []

    await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
    await hass.async_block_till_done()

    pending = (await _ask(client))["pending"]
    assert [r["id"] for r in pending] == [_id(WEIGHT, moment) for moment in moments]
    assert [r["kilograms"] for r in pending] == [80.0, 80.5, 81.0]
    assert pending[0]["time"] == "2026-09-24T07:30:00Z"
    assert pending[0]["time_source"] == "state"
    assert pending[0]["device"] == {"type": "scale"}


async def test_backfill_gives_the_same_ids_as_live_and_a_second_press_changes_nothing(
    amsterdam: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = amsterdam
    await _load(hass, {"writeback": {"weight": {"entity": WEIGHT}}})
    moments = await _three_weighings(hass, freezer)

    client = await hass_client_no_auth()
    live = (await _ask(client))["pending"]
    assert [r["id"] for r in live] == [_id(WEIGHT, moment) for moment in moments]

    for _ in range(2):
        await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
        await hass.async_block_till_done()
    # The three live readings are recognised, and the creation state is no reading.
    assert (await _ask(client))["pending"] == live

    # Also after the phone confirmed two and refused one for good: neither a
    # delivered nor a refused reading is queued again.
    await _ask(
        client,
        ack=[r["id"] for r in live[:2]],
        failed=[{"id": live[2]["id"], "code": "permission_denied"}],
    )
    await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
    await hass.async_block_till_done()
    assert (await _ask(client))["pending"] == []


# --- The service ----------------------------------------------------------------


async def test_the_service_takes_a_window_and_types(
    amsterdam: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = amsterdam
    moments = await _three_weighings(hass, freezer)
    freezer.move_to(START - timedelta(days=3))
    await _set(hass, "sensor.scale_height", 181, "cm")
    await _recorded(hass)
    freezer.move_to(START)
    await _load(
        hass,
        {
            "writeback": {
                "weight": {"entity": WEIGHT},
                "height": {"entity": "sensor.scale_height"},
            }
        },
    )

    await hass.services.async_call(
        DOMAIN, "queue_history", {"days": 2, "types": ["weight"]}, blocking=True
    )
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    pending = (await _ask(client, types=["weight", "height"]))["pending"]
    # Two days back: the oldest morning is outside the window, and no height.
    assert [r["id"] for r in pending] == [_id(WEIGHT, moment) for moment in moments[1:]]


async def test_the_service_needs_to_know_which_phone(amsterdam: HomeAssistant) -> None:
    hass = amsterdam
    mine = await _load(hass, {"writeback": {"weight": {"entity": WEIGHT}}})
    other = await _load(hass, {"writeback": {"weight": {"entity": WEIGHT}}}, webhook_id="c" * 64)

    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call(DOMAIN, "queue_history", {}, blocking=True)
    assert refused.value.translation_key == "choose_a_phone"
    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call(
            DOMAIN, "queue_history", {"config_entry": "nope"}, blocking=True
        )
    assert refused.value.translation_key == "phone_not_loaded"
    # Named, it works with two phones; with one phone the name is not needed.
    await hass.services.async_call(
        DOMAIN, "queue_history", {"config_entry": mine.entry_id}, blocking=True
    )
    await hass.config_entries.async_unload(other.entry_id)
    await hass.services.async_call(DOMAIN, "queue_history", {}, blocking=True)
    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call(
            DOMAIN, "queue_history", {"config_entry": other.entry_id}, blocking=True
        )
    assert refused.value.translation_key == "phone_not_loaded"


async def test_the_window_is_at_most_what_the_queue_keeps(amsterdam: HomeAssistant) -> None:
    """Ninety days: anything older would be pruned at the next answer anyway."""
    hass = amsterdam
    entry = await _load(hass, {"writeback": {"weight": {"entity": WEIGHT}}})
    await hass.services.async_call(DOMAIN, "queue_history", {"days": 90}, blocking=True)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "queue_history", {"days": 91}, blocking=True)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "queue_history", {"days": 0}, blocking=True)
    # The manager clamps a window handed to it directly, too.
    with patch(
        "homeassistant.components.recorder.history.state_changes_during_period", return_value={}
    ) as read:
        await entry.runtime_data.writeback.async_queue_history(days=200)
    start = read.call_args.args[1]
    assert (read.call_args.args[2] - start).days == 90


async def test_the_newest_rows_count_when_there_are_too_many(
    amsterdam: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """A limit in the recorder's query would take the oldest rows; ours takes the newest."""
    hass = amsterdam
    moments = await _three_weighings(hass, freezer)
    await _load(hass, {"writeback": {"weight": {"entity": WEIGHT}}})
    with patch("custom_components.life_dashboard.writeback.BACKFILL_MAX_PER_ENTITY", 2):
        await hass.services.async_call(DOMAIN, "queue_history", {}, blocking=True)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    pending = (await _ask(client))["pending"]
    assert [r["id"] for r in pending] == [_id(WEIGHT, moment) for moment in moments[1:]]


async def test_the_creation_state_and_a_value_out_of_range_are_skipped(
    amsterdam: HomeAssistant, hass_client_no_auth, freezer, caplog
) -> None:
    """A helper starts at 0, a scale integration may start at a placeholder."""
    hass = amsterdam
    freezer.move_to(START - timedelta(days=3))
    await _set(hass, WEIGHT, 75.0)  # a plausible creation state
    freezer.move_to(START - timedelta(days=2))
    await _set(hass, WEIGHT, 0.0)  # what the app refuses as out_of_range
    freezer.move_to(START - timedelta(days=1))
    await _set(hass, WEIGHT, 81.0)
    await _recorded(hass)
    freezer.move_to(START)
    await _load(hass, {"writeback": {"weight": {"entity": WEIGHT}}})

    await hass.services.async_call(DOMAIN, "queue_history", {}, blocking=True)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    pending = (await _ask(client))["pending"]
    assert [r["kilograms"] for r in pending] == [81.0]
    assert "Queued 1 readings" in caplog.text
    # Only the integration's own warnings: on Python 3.14 asyncio logs a slow-callback
    # warning that quotes this test's name, which itself contains "range".
    assert not [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and r.name.startswith("custom_components.life_dashboard")
        and "range" in r.getMessage()
    ]


async def test_the_first_change_counts_when_the_entity_existed_before_the_window(
    amsterdam: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = amsterdam
    freezer.move_to(START - timedelta(days=3))
    await _set(hass, WEIGHT, 75.0)
    freezer.move_to(START - timedelta(days=1))
    await _set(hass, WEIGHT, 81.0)
    await _recorded(hass)
    freezer.move_to(START)
    await _load(hass, {"writeback": {"weight": {"entity": WEIGHT}}})

    # A two-day window: the state from three days ago is what the entity held when the
    # window opened, so the change inside it is the first reading, not the creation.
    await hass.services.async_call(DOMAIN, "queue_history", {"days": 2}, blocking=True)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    pending = (await _ask(client))["pending"]
    assert [r["kilograms"] for r in pending] == [81.0]


async def test_a_restart_is_not_a_measurement(
    amsterdam: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """The restored state after a restart is a recorder row on the boot time with the
    old value; a restart within the window must not become a weighing."""
    hass = amsterdam
    freezer.move_to(START - timedelta(days=3))
    await _set(hass, WEIGHT, 0.0)
    freezer.move_to(START - timedelta(days=2))
    await _set(hass, WEIGHT, 81.0)
    weighed = START - timedelta(days=2)
    # The restart: Home Assistant writes the restored value as a new row.
    boot = START - timedelta(days=1)
    freezer.move_to(boot)
    hass.states.async_remove(WEIGHT)
    await _set(hass, WEIGHT, 81.0, "kg", restored=True)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=30))
    await _set(hass, WEIGHT, 81.0)  # the integration polls and writes the value again
    await _recorded(hass)
    freezer.move_to(START)
    await _load(hass, {"writeback": {"weight": {"entity": WEIGHT}}})

    await hass.services.async_call(DOMAIN, "queue_history", {}, blocking=True)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    pending = (await _ask(client))["pending"]
    assert [r["id"] for r in pending] == [_id(WEIGHT, weighed)]


async def test_a_restart_is_not_a_blood_pressure_reading_either(
    amsterdam: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = amsterdam
    freezer.move_to(START - timedelta(days=3))
    await _set(hass, SYSTOLIC, 0, "mmHg")
    await _set(hass, DIASTOLIC, 0, "mmHg")
    freezer.move_to(START - timedelta(days=2))
    await _set(hass, SYSTOLIC, 130, "mmHg")
    await _set(hass, DIASTOLIC, 82, "mmHg")
    measured = START - timedelta(days=2)
    for restart in (START - timedelta(days=1), START - timedelta(hours=6)):
        freezer.move_to(restart)
        hass.states.async_remove(SYSTOLIC)
        hass.states.async_remove(DIASTOLIC)
        await _set(hass, SYSTOLIC, 130, "mmHg", restored=True)
        await _set(hass, DIASTOLIC, 82, "mmHg", restored=True)
        freezer.tick(timedelta(seconds=30))
        await _set(hass, SYSTOLIC, 130, "mmHg")
        await _set(hass, DIASTOLIC, 82, "mmHg")
    await _recorded(hass)
    freezer.move_to(START)
    await _load(
        hass, {"writeback": {"blood_pressure": {"systolic": SYSTOLIC, "diastolic": DIASTOLIC}}}
    )

    await hass.services.async_call(DOMAIN, "queue_history", {}, blocking=True)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    pending = (await _ask(client, types=["blood_pressure"]))["pending"]
    assert [(r["id"], r["systolic"], r["diastolic"]) for r in pending] == [
        (_id(SYSTOLIC, measured), 130.0, 82.0)
    ]


async def test_backfill_with_a_timestamp_entity_is_idempotent(
    amsterdam: HomeAssistant, hass_client_no_auth, freezer, caplog
) -> None:
    """Several rows within the window of one timestamp value are one reading with
    the last value; a second press bumps no version."""
    hass = amsterdam
    freezer.move_to(START - timedelta(days=2))
    await _set(hass, WEIGHT, 0.0)
    await _set(hass, WEIGHED_AT, "2026-09-25T06:00:00+00:00", None, device_class="timestamp")
    freezer.move_to(START - timedelta(days=1))
    await _set(hass, WEIGHED_AT, "2026-09-26T06:29:40+00:00", None, device_class="timestamp")
    freezer.tick(timedelta(seconds=2))
    await _set(hass, WEIGHT, 83.7)
    freezer.tick(timedelta(seconds=3))
    await _set(hass, WEIGHT, 84.2)  # the weight with impedance, rounded differently
    freezer.tick(timedelta(seconds=3))
    await _set(hass, WEIGHT, 84.4)  # a correction
    await _recorded(hass)
    freezer.move_to(START)
    await _load(hass, {"writeback": {"weight": {"entity": WEIGHT, "time_entity": WEIGHED_AT}}})
    measured = datetime(2026, 9, 26, 6, 29, 40, tzinfo=UTC)

    await hass.services.async_call(DOMAIN, "queue_history", {}, blocking=True)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    pending = (await _ask(client))["pending"]
    assert [(r["id"], r["version"], r["kilograms"]) for r in pending] == [
        (_id(WEIGHT, measured), 1, 84.4)
    ]

    caplog.clear()
    await hass.services.async_call(DOMAIN, "queue_history", {"days": 90}, blocking=True)
    await hass.async_block_till_done()
    assert (await _ask(client))["pending"] == pending
    assert "Queued 0 readings" in caplog.text

    # Also after the phone wrote it: nothing comes back, no version moves.
    await _ask(client, ack=[pending[0]["id"]])
    await hass.services.async_call(DOMAIN, "queue_history", {}, blocking=True)
    await hass.async_block_till_done()
    assert (await _ask(client))["pending"] == []


# --- The same assembly as live ----------------------------------------------------


async def test_blood_pressure_pairs_from_the_recorder(
    amsterdam: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = amsterdam
    freezer.move_to(START - timedelta(days=3))
    await _set(hass, SYSTOLIC, 0, "mmHg")
    await _set(hass, DIASTOLIC, 0, "mmHg")
    readings = ((128, 82), (131, 84))
    moments = []
    for day, (systolic, diastolic) in enumerate(readings):
        freezer.move_to(START - timedelta(days=2 - day))
        await _set(hass, SYSTOLIC, systolic, "mmHg")
        freezer.tick(timedelta(seconds=10))
        await _set(hass, DIASTOLIC, diastolic, "mmHg")
        moments.append(START - timedelta(days=2 - day))
    # A systolic change on its own: the diastolic did not change, so the recorder has
    # no row for it, and the reading takes the last known diastolic.
    freezer.move_to(START - timedelta(hours=5))
    await _set(hass, SYSTOLIC, 140, "mmHg")
    await _recorded(hass)
    freezer.move_to(START)
    await _load(
        hass,
        {
            "writeback": {
                "blood_pressure": {
                    "systolic": SYSTOLIC,
                    "diastolic": DIASTOLIC,
                    "body_position": "sitting_down",
                }
            }
        },
    )

    await hass.services.async_call(DOMAIN, "queue_history", {}, blocking=True)
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    pending = (await _ask(client, types=["blood_pressure"]))["pending"]
    assert [(r["id"], r["systolic"], r["diastolic"]) for r in pending] == [
        (_id(SYSTOLIC, moments[0]), 128.0, 82.0),
        (_id(SYSTOLIC, moments[1]), 131.0, 84.0),
        (_id(SYSTOLIC, START - timedelta(hours=5)), 140.0, 84.0),
    ]
    assert pending[0]["body_position"] == "sitting_down"
    assert pending[0]["recording_method"] == "active"


async def test_a_half_takes_its_value_from_before_the_window(
    amsterdam: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """Yesterday 128/82, today 131/82: the recorder has no diastolic row today."""
    hass = amsterdam
    freezer.move_to(START - timedelta(days=3))
    await _set(hass, SYSTOLIC, 128, "mmHg")
    await _set(hass, DIASTOLIC, 82, "mmHg")
    freezer.move_to(START - timedelta(days=1))
    await _set(hass, SYSTOLIC, 131, "mmHg")
    freezer.move_to(START - timedelta(hours=3))
    await _set(hass, DIASTOLIC, 79, "mmHg")
    await _recorded(hass)
    freezer.move_to(START)
    await _load(
        hass, {"writeback": {"blood_pressure": {"systolic": SYSTOLIC, "diastolic": DIASTOLIC}}}
    )

    await hass.services.async_call(DOMAIN, "queue_history", {"days": 2}, blocking=True)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    pending = (await _ask(client, types=["blood_pressure"]))["pending"]
    assert [(r["id"], r["systolic"], r["diastolic"]) for r in pending] == [
        (_id(SYSTOLIC, START - timedelta(days=1)), 131.0, 82.0),
        (_id(SYSTOLIC, START - timedelta(hours=3)), 131.0, 79.0),
    ]


async def test_the_timestamp_entity_is_read_from_the_recorder_too(
    amsterdam: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = amsterdam
    freezer.move_to(START - timedelta(days=2))
    await _set(hass, WEIGHT, 0.0)
    await _set(hass, WEIGHED_AT, "2026-09-25T06:00:00+00:00", None, device_class="timestamp")
    freezer.move_to(START - timedelta(days=1))
    await _set(hass, WEIGHT, 80.0)
    freezer.tick(timedelta(seconds=5))
    await _set(hass, WEIGHED_AT, "2026-09-26T06:29:40+00:00", None, device_class="timestamp")
    await _recorded(hass)
    freezer.move_to(START)
    await _load(hass, {"writeback": {"weight": {"entity": WEIGHT, "time_entity": WEIGHED_AT}}})

    await hass.services.async_call(DOMAIN, "queue_history", {}, blocking=True)
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    pending = (await _ask(client))["pending"]
    assert [r["id"] for r in pending] == [_id(WEIGHT, datetime(2026, 9, 26, 6, 29, 40, tzinfo=UTC))]
    assert "time_source" not in pending[0]
