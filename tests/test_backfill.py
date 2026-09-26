"""Test the backfill: the recorder's past as readings for the phone.

The button and the service read the recorder, not the statistics, and form readings
with the same id rule as the listeners, so what the live path already queued is
recognised and a second press changes nothing.
"""

import json
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

import pytest
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


async def _ask(client, types=("weight",), *, ack=None) -> dict:
    block: dict = {"protocol": 1, "types": list(types), "history": True}
    if ack is not None:
        block["ack"] = ack
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
    """Three mornings in the recorder, an hour past the day boundary each."""
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
    freezer.move_to(START - timedelta(days=4))
    await _set(hass, WEIGHT, 79.0)
    await _load(hass, {"writeback": {"weight": {"entity": WEIGHT}}})
    moments = await _three_weighings(hass, freezer)

    client = await hass_client_no_auth()
    live = (await _ask(client))["pending"]
    assert [r["id"] for r in live] == [_id(WEIGHT, moment) for moment in moments]

    for _ in range(2):
        await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
        await hass.async_block_till_done()
    pending = (await _ask(client))["pending"]
    # The three live readings are recognised; only the weighing from before the
    # mapping existed is new, and once.
    assert pending[1:] == live
    assert pending[0]["id"] == _id(WEIGHT, START - timedelta(days=4))
    assert pending[0]["kilograms"] == 79.0

    # Also after the phone confirmed them: a delivered reading is not queued again.
    await _ask(client, ack=[r["id"] for r in pending])
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


async def test_the_service_refuses_a_window_out_of_range(amsterdam: HomeAssistant) -> None:
    hass = amsterdam
    await _load(hass, {"writeback": {"weight": {"entity": WEIGHT}}})
    with pytest.raises(Exception, match="days"):
        await hass.services.async_call(DOMAIN, "queue_history", {"days": 400}, blocking=True)


# --- The same assembly as live ----------------------------------------------------


async def test_blood_pressure_pairs_from_the_recorder(
    amsterdam: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = amsterdam
    readings = ((128, 82), (131, 84))
    moments = []
    for day, (systolic, diastolic) in enumerate(readings):
        freezer.move_to(START - timedelta(days=2 - day))
        await _set(hass, SYSTOLIC, systolic, "mmHg")
        freezer.tick(timedelta(seconds=10))
        await _set(hass, DIASTOLIC, diastolic, "mmHg")
        moments.append(START - timedelta(days=2 - day))
    # A lone systolic change with no diastolic near it belongs to nothing.
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
    ]
    assert pending[0]["body_position"] == "sitting_down"
    assert pending[0]["recording_method"] == "active"


async def test_the_timestamp_entity_is_read_from_the_recorder_too(
    amsterdam: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = amsterdam
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
