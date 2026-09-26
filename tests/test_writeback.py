"""Test the Home Assistant half of receiving measurements on the phone.

A state change of a mapped entity has to come out of the webhook's answer as a
reading in the unit Health Connect wants, once, with the moment it was measured; an
ack has to make it go away; and nothing about a value may reach a log or another
phone's queue.
"""

import json
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.life_dashboard.const import (
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    URL_CHOICE_INTERNAL,
)
from custom_components.life_dashboard.payload import SIGNATURE_HEADER, signature_for
from custom_components.life_dashboard.writeback_queue import epoch_ms

WEBHOOK_ID = "a" * 64
SECRET = "b" * 64
URL = f"/api/webhook/{WEBHOOK_ID}"
WEIGHT = "sensor.scale_weight"
WEIGHED_AT = "sensor.scale_last_measurement_time"
SYSTOLIC = "sensor.omron_systolic"
DIASTOLIC = "sensor.omron_diastolic"
START = datetime(2026, 9, 27, 6, 30, tzinfo=UTC)

WEIGHT_ONLY = {"writeback": {"weight": {"entity": WEIGHT}}}


def _entry(options: dict, *, webhook_id: str = WEBHOOK_ID, title: str = "Owen's Pixel"):
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=webhook_id,
        data={
            CONF_WEBHOOK_ID: webhook_id,
            CONF_SECRET: SECRET,
            CONF_URL_CHOICE: URL_CHOICE_INTERNAL,
        },
        options=options,
    )


@pytest.fixture
async def hass_tz(hass: HomeAssistant, freezer) -> HomeAssistant:
    """Amsterdam, at a fixed moment, so ids and offsets can be named."""
    freezer.move_to(START)
    await async_process_ha_core_config(hass, {"time_zone": "Europe/Amsterdam"})
    return hass


async def _load(hass: HomeAssistant, options: dict = WEIGHT_ONLY, **kwargs) -> MockConfigEntry:
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


async def _post(client, payload: dict, *, url: str = URL):
    body = json.dumps(payload).encode()
    return await client.post(
        url,
        data=body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature_for(SECRET, body)},
    )


async def _ask(client, types=("weight",), *, ack=None, failed=None, url: str = URL) -> dict:
    """A heartbeat with a writeback block, as the app posts it."""
    block: dict = {"protocol": 1, "types": list(types), "history": False}
    if ack is not None:
        block["ack"] = ack
    if failed is not None:
        block["failed"] = failed
    response = await _post(
        client,
        {
            "timestamp": "2026-09-27T06:35:00Z",
            "app_version": "1.20.0",
            "source": "health_connect",
            "writeback": block,
        },
        url=url,
    )
    assert response.status == HTTPStatus.OK
    return (await response.json())["writeback"]


def _id(entity_id: str, moment: datetime) -> str:
    return f"{entity_id}@{epoch_ms(moment)}"


def loaded_queue(hass: HomeAssistant):
    """The queue of the one entry, for looking before the phone asks."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    return entry.runtime_data.writeback.queue


def _our_warnings(caplog) -> list[str]:
    return [
        record.message
        for record in caplog.records
        if record.levelname == "WARNING" and record.name.startswith("custom_components.")
    ]


# --- From a state to a reading -------------------------------------------------


async def test_a_pound_sensor_becomes_a_kilogram_reading(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = hass_tz
    await _set(hass, WEIGHT, 180.0, "lb", device_class="weight")
    await _load(hass)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 181.0, "lb", device_class="weight")

    client = await hass_client_no_auth()
    answer = await _ask(client)
    assert answer["configured"] == ["weight"]
    assert answer["more"] is False
    assert len(answer["pending"]) == 1
    reading = answer["pending"][0]
    measured = START + timedelta(minutes=1)
    assert reading == {
        "id": _id(WEIGHT, measured),
        "version": 1,
        "type": "weight",
        "kilograms": pytest.approx(82.1002),
        "time": "2026-09-27T06:31:00Z",
        "zone_offset": "+02:00",
        "recording_method": "auto",
        "device": {"type": "scale"},
        "time_source": "state",
    }


async def test_without_types_there_is_no_pending(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """An app that has not turned receiving on gets the announcement only."""
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    await _load(hass)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0)

    client = await hass_client_no_auth()
    response = await _post(
        client, {"timestamp": "2026-09-27T06:35:00Z", "source": "health_connect"}
    )
    answer = (await response.json())["writeback"]
    assert answer["configured"] == ["weight"]
    assert "pending" not in answer
    # The reading is still waiting for a phone that asks.
    assert len((await _ask(client))["pending"]) == 1


async def test_only_the_types_the_phone_asked_for(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    await _load(hass)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0)

    client = await hass_client_no_auth()
    answer = await _ask(client, types=["height", "body_fat"])
    assert answer["pending"] == []
    assert answer["more"] is False


async def test_an_ack_takes_the_reading_away(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer, hass_storage
) -> None:
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    entry = await _load(hass)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0)

    client = await hass_client_no_auth()
    reading = (await _ask(client))["pending"][0]
    # Offered again until confirmed: a lost outbox costs nothing.
    assert (await _ask(client))["pending"] == [reading]

    answer = await _ask(client, ack=[reading["id"]])
    assert answer["pending"] == []
    assert (await _ask(client))["pending"] == []

    await hass.config_entries.async_unload(entry.entry_id)
    stored = hass_storage[f"life_dashboard.{entry.entry_id}.writeback"]["data"]
    assert stored["pending"] == {}
    assert reading["id"] in stored["delivered"]
    assert "81" not in json.dumps(stored["delivered"])


async def test_a_reload_keeps_the_queue(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    entry = await _load(hass)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    assert len((await _ask(client))["pending"]) == 1


# --- What is one measurement --------------------------------------------------------


async def test_the_same_value_reported_again_is_one_reading_until_it_is_old(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """A BLE scale reports one weighing for a while; the next morning is another."""
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    await _load(hass)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0)
    first = START + timedelta(minutes=1)
    for _ in range(3):
        freezer.tick(timedelta(seconds=20))
        await _set(hass, WEIGHT, 81.0)  # state_reported, not state_changed

    client = await hass_client_no_auth()
    assert [r["id"] for r in (await _ask(client))["pending"]] == [_id(WEIGHT, first)]

    freezer.tick(timedelta(hours=24))
    await _set(hass, WEIGHT, 81.0)
    second = START + timedelta(minutes=2, hours=24)
    assert [r["id"] for r in (await _ask(client))["pending"]] == [
        _id(WEIGHT, first),
        _id(WEIGHT, second),
    ]


async def test_a_polled_value_after_a_restart_is_not_a_reading(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """A polled integration writes its unchanged value again after every restart."""
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    entry = await _load(hass)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0)
    client = await hass_client_no_auth()
    reading = (await _ask(client))["pending"][0]
    await _ask(client, ack=[reading["id"]])

    freezer.tick(timedelta(hours=3))
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0)

    assert (await _ask(client))["pending"] == []


async def test_unknown_unavailable_and_text_are_skipped(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    await _load(hass)
    for value in ("unknown", "unavailable", "heavy"):
        freezer.tick(timedelta(minutes=1))
        await _set(hass, WEIGHT, value)
    client = await hass_client_no_auth()
    assert (await _ask(client))["pending"] == []


async def test_a_unit_outside_the_family_is_skipped_and_warned_once(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer, caplog
) -> None:
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0, "L")
    await _load(hass)
    for value in (81.0, 82.0):
        freezer.tick(timedelta(minutes=1))
        await _set(hass, WEIGHT, value, "L")

    client = await hass_client_no_auth()
    assert (await _ask(client))["pending"] == []
    warnings = _our_warnings(caplog)
    assert len(warnings) == 1
    assert WEIGHT in warnings[0]
    assert "not a mass unit" in warnings[0]
    assert "81" not in warnings[0]


async def test_a_restored_state_is_not_a_measurement(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """The first state of an entity, and a restored one, have no measured moment."""
    hass = hass_tz
    await _load(hass)
    await _set(hass, WEIGHT, 80.0)  # first state: nothing to compare with
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0, restored=True)
    client = await hass_client_no_auth()
    assert (await _ask(client))["pending"] == []


# --- Blood pressure ----------------------------------------------------------------


PRESSURE = {
    "writeback": {
        "blood_pressure": {
            "systolic": SYSTOLIC,
            "diastolic": DIASTOLIC,
            "body_position": "sitting_down",
            "measurement_location": "left_upper_arm",
        }
    }
}


async def test_a_pair_within_ninety_seconds_is_one_reading(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = hass_tz
    await _set(hass, SYSTOLIC, 120, "mmHg")
    await _set(hass, DIASTOLIC, 80, "mmHg")
    await _load(hass, PRESSURE)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, SYSTOLIC, 128, "mmHg")
    measured = START + timedelta(minutes=1)
    freezer.tick(timedelta(seconds=30))
    await _set(hass, DIASTOLIC, 82, "mmHg")

    client = await hass_client_no_auth()
    answer = await _ask(client, types=["blood_pressure"])
    assert answer["configured"] == ["blood_pressure"]
    assert answer["pending"] == [
        {
            "id": _id(SYSTOLIC, measured),
            "version": 1,
            "type": "blood_pressure",
            "systolic": 128.0,
            "diastolic": 82.0,
            "time": "2026-09-27T06:31:00Z",
            "zone_offset": "+02:00",
            "recording_method": "active",
            "time_source": "state",
            "body_position": "sitting_down",
            "measurement_location": "left_upper_arm",
        }
    ]


async def test_kilopascal_is_converted_to_mmhg(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = hass_tz
    await _set(hass, SYSTOLIC, 16.0, "kPa")
    await _set(hass, DIASTOLIC, 10.0, "kPa")
    await _load(hass, PRESSURE)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, DIASTOLIC, 10.9, "kPa")
    await _set(hass, SYSTOLIC, 17.0, "kPa")

    client = await hass_client_no_auth()
    reading = (await _ask(client, types=["blood_pressure"]))["pending"][0]
    assert reading["systolic"] == pytest.approx(127.5, abs=0.05)
    assert reading["diastolic"] == pytest.approx(81.8, abs=0.05)


async def test_an_unchanged_diastolic_reported_again_pairs(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """The common case: systolic changed, diastolic did not, and the integration wrote
    both again, so the diastolic is a report rather than a change."""
    hass = hass_tz
    await _set(hass, SYSTOLIC, 128, "mmHg")
    await _set(hass, DIASTOLIC, 82, "mmHg")
    await _load(hass, PRESSURE)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, SYSTOLIC, 131, "mmHg")
    await _set(hass, DIASTOLIC, 82, "mmHg")  # state_reported

    client = await hass_client_no_auth()
    pending = (await _ask(client, types=["blood_pressure"]))["pending"]
    assert [(r["systolic"], r["diastolic"]) for r in pending] == [(131.0, 82.0)]
    assert pending[0]["id"] == _id(SYSTOLIC, START + timedelta(minutes=1))


async def test_half_a_blood_pressure_is_nothing(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = hass_tz
    await _set(hass, SYSTOLIC, 120, "mmHg")
    await _set(hass, DIASTOLIC, 80, "mmHg")
    await _load(hass, PRESSURE)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, SYSTOLIC, 128, "mmHg")
    client = await hass_client_no_auth()
    assert (await _ask(client, types=["blood_pressure"]))["pending"] == []

    # The other half arrives too late to belong to it.
    freezer.tick(timedelta(minutes=2))
    await _set(hass, DIASTOLIC, 82, "mmHg")
    assert (await _ask(client, types=["blood_pressure"]))["pending"] == []


# --- Units ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "unit", "value", "field", "expected"),
    [
        ("weight", "st", 12.5, "kilograms", 79.3786),
        ("weight", "oz", 2800.0, "kilograms", 79.3786),
        ("weight", "g", 79378.6, "kilograms", 79.3786),
        ("height", "in", 71.0, "meters", 1.8034),
        ("height", "ft", 6.0, "meters", 1.8288),
        ("height", "mm", 1803.0, "meters", 1.803),
        ("lean_body_mass", "lb", 130.0, "kilograms", 58.967),
        ("body_fat", "%", 21.4, "percentage", 21.4),
    ],
)
async def test_every_unit_of_a_family_lands_in_health_connects(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer, kind, unit, value, field, expected
) -> None:
    hass = hass_tz
    await _set(hass, WEIGHT, value - 1, unit)
    await _load(hass, {"writeback": {kind: {"entity": WEIGHT}}})
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, value, unit)
    client = await hass_client_no_auth()
    reading = (await _ask(client, types=[kind]))["pending"][0]
    assert reading["type"] == kind
    assert reading[field] == pytest.approx(expected, abs=0.001)


async def test_hectopascal_is_a_pressure_unit_too(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = hass_tz
    await _set(hass, SYSTOLIC, 160.0, "hPa")
    await _set(hass, DIASTOLIC, 100.0, "hPa")
    await _load(hass, PRESSURE)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, SYSTOLIC, 170.0, "hPa")
    await _set(hass, DIASTOLIC, 109.0, "hPa")
    client = await hass_client_no_auth()
    reading = (await _ask(client, types=["blood_pressure"]))["pending"][0]
    assert reading["systolic"] == pytest.approx(127.5, abs=0.05)
    assert reading["diastolic"] == pytest.approx(81.8, abs=0.05)


async def test_a_unit_that_changes_after_configuration_is_converted_per_event(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """Switching Home Assistant to imperial changes the state's unit; each event
    is converted on the unit it carries, never on the one seen at configuration."""
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0, "kg")
    await _load(hass)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0, "kg")
    freezer.tick(timedelta(hours=1))
    await _set(hass, WEIGHT, 180.0, "lb")
    client = await hass_client_no_auth()
    pending = (await _ask(client))["pending"]
    assert [r["kilograms"] for r in pending] == [81.0, pytest.approx(81.6466, abs=0.001)]


async def test_percent_is_taken_literally_only(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer, caplog
) -> None:
    """Body fat needs the % sign; "percent" or a mass unit is not a percentage."""
    hass = hass_tz
    await _set(hass, WEIGHT, 20.0, "percent")
    await _load(hass, {"writeback": {"body_fat": {"entity": WEIGHT}}})
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 21.0, "percent")
    client = await hass_client_no_auth()
    assert (await _ask(client, types=["body_fat"]))["pending"] == []
    assert any("not a percentage unit" in message for message in _our_warnings(caplog))


async def test_a_zero_or_a_nan_state_makes_no_reading(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """A scale that resets to 0, a template that fails to nan: no record, no broken answer."""
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    await _load(hass)
    for value in ("0", "0.0", "nan", "inf", "-inf", "1e400", "600"):
        freezer.tick(timedelta(minutes=1))
        await _set(hass, WEIGHT, value)
    client = await hass_client_no_auth()
    response = await _post(
        client,
        {
            "timestamp": "2026-09-27T06:45:00Z",
            "source": "health_connect",
            "writeback": {"protocol": 1, "types": ["weight"]},
        },
    )
    raw = await response.read()
    assert b"NaN" not in raw and b"Infinity" not in raw
    assert json.loads(raw)["writeback"]["pending"] == []


# --- The measured moment --------------------------------------------------------------


TIMED = {"writeback": {"weight": {"entity": WEIGHT, "time_entity": WEIGHED_AT}}}


async def test_the_timestamp_entity_gives_the_moment(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """BodyMiScale's last_measurement_time, written just before the weight."""
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    await _set(hass, WEIGHED_AT, "2026-09-26T21:00:00+00:00", None, device_class="timestamp")
    await _load(hass, TIMED)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHED_AT, "2026-09-27T06:30:40+00:00", None, device_class="timestamp")
    freezer.tick(timedelta(seconds=3))
    await _set(hass, WEIGHT, 81.0)

    client = await hass_client_no_auth()
    reading = (await _ask(client))["pending"][0]
    assert reading["id"] == _id(WEIGHT, datetime(2026, 9, 27, 6, 30, 40, tzinfo=UTC))
    assert reading["time"] == "2026-09-27T06:30:40Z"
    assert "time_source" not in reading


async def test_a_timestamp_that_follows_its_value_moves_the_reading(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    await _set(hass, WEIGHED_AT, "2026-09-26T21:00:00+00:00", None, device_class="timestamp")
    await _load(hass, TIMED)
    # Yesterday's timestamp is well outside the window when the weight arrives.
    freezer.tick(timedelta(minutes=5))
    await _set(hass, WEIGHT, 81.0)
    provisional = _id(WEIGHT, START + timedelta(minutes=5))
    assert provisional in loaded_queue(hass).pending

    freezer.tick(timedelta(seconds=3))
    await _set(hass, WEIGHED_AT, "2026-09-27T06:30:40+00:00", None, device_class="timestamp")
    client = await hass_client_no_auth()
    pending = (await _ask(client))["pending"]
    assert [r["id"] for r in pending] == [_id(WEIGHT, datetime(2026, 9, 27, 6, 30, 40, tzinfo=UTC))]
    assert "time_source" not in pending[0]

    # A timestamp long after the value belongs to nothing that is queued.
    freezer.tick(timedelta(minutes=5))
    await _set(hass, WEIGHED_AT, "2026-09-27T06:36:00+00:00", None, device_class="timestamp")
    assert [r["id"] for r in (await _ask(client))["pending"]] == [pending[0]["id"]]


async def test_a_reading_the_phone_has_seen_does_not_move(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """Once offered, the id may be written on the phone; moving it would double it."""
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    await _set(hass, WEIGHED_AT, "2026-09-26T21:00:00+00:00", None, device_class="timestamp")
    await _load(hass, TIMED)
    freezer.tick(timedelta(minutes=5))
    await _set(hass, WEIGHT, 81.0)
    client = await hass_client_no_auth()
    offered = (await _ask(client))["pending"][0]
    assert offered["time_source"] == "state"

    freezer.tick(timedelta(seconds=3))
    await _set(hass, WEIGHED_AT, "2026-09-27T06:30:40+00:00", None, device_class="timestamp")
    assert (await _ask(client))["pending"] == [offered]


async def test_a_stale_timestamp_entity_falls_back_to_last_changed(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    await _set(hass, WEIGHED_AT, "2026-09-26T21:00:00+00:00", None, device_class="timestamp")
    await _load(hass, TIMED)
    freezer.tick(timedelta(hours=2))
    await _set(hass, WEIGHT, 81.0)
    client = await hass_client_no_auth()
    reading = (await _ask(client))["pending"][0]
    assert reading["id"] == _id(WEIGHT, START + timedelta(hours=2))
    assert reading["time_source"] == "state"


# --- Metadata ---------------------------------------------------------------------------


async def test_the_device_comes_from_the_registry(
    hass_tz: HomeAssistant,
    hass_client_no_auth,
    freezer,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    hass = hass_tz
    scale = MockConfigEntry(domain="xiaomi_ble", title="Scale")
    scale.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=scale.entry_id,
        identifiers={("xiaomi_ble", "scale")},
        manufacturer="Xiaomi",
        model="Mi Body Composition Scale 2",
    )
    entity_registry.async_get_or_create(
        "sensor",
        "xiaomi_ble",
        "scale-mass",
        suggested_object_id="scale_weight",
        device_id=device.id,
        original_device_class="weight",
    )
    await _set(hass, WEIGHT, 80.0)
    await _load(hass)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0)

    client = await hass_client_no_auth()
    reading = (await _ask(client))["pending"][0]
    assert reading["device"] == {
        "type": "scale",
        "manufacturer": "Xiaomi",
        "model": "Mi Body Composition Scale 2",
    }


async def test_an_input_number_is_a_manual_entry(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = hass_tz
    entity_id = "input_number.my_height"
    await _set(hass, entity_id, 180, "cm")
    await _load(hass, {"writeback": {"height": {"entity": entity_id}}})
    freezer.tick(timedelta(minutes=1))
    await _set(hass, entity_id, 181, "cm")

    client = await hass_client_no_auth()
    reading = (await _ask(client, types=["height"]))["pending"][0]
    assert reading["type"] == "height"
    assert reading["meters"] == pytest.approx(1.81)
    assert reading["recording_method"] == "manual"
    assert "device" not in reading


async def test_removing_a_mapping_drops_its_pending_readings(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """The way out of a queue stuck on one type: take the mapping away."""
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    await _set(hass, SYSTOLIC, 120, "mmHg")
    await _set(hass, DIASTOLIC, 80, "mmHg")
    entry = await _load(hass, {"writeback": {**WEIGHT_ONLY["writeback"], **PRESSURE["writeback"]}})
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0)
    await _set(hass, SYSTOLIC, 128, "mmHg")
    await _set(hass, DIASTOLIC, 82, "mmHg")
    client = await hass_client_no_auth()
    assert len((await _ask(client, types=["weight", "blood_pressure"]))["pending"]) == 2

    hass.config_entries.async_update_entry(entry, options=PRESSURE)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    answer = await _ask(client, types=["weight", "blood_pressure"])
    assert answer["configured"] == ["blood_pressure"]
    assert [r["type"] for r in answer["pending"]] == ["blood_pressure"]


# --- Two phones -------------------------------------------------------------------------


async def test_two_entries_share_nothing(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    hass = hass_tz
    partner = "sensor.partner_weight"
    await _set(hass, WEIGHT, 80.0)
    await _set(hass, partner, 60.0)
    await _load(hass)
    await _load(
        hass,
        {"writeback": {"weight": {"entity": partner}}},
        webhook_id="c" * 64,
        title="Partner's phone",
    )
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0)
    await _set(hass, partner, 61.0)

    client = await hass_client_no_auth()
    mine = (await _ask(client))["pending"]
    theirs = (await _ask(client, url=f"/api/webhook/{'c' * 64}"))["pending"]
    assert [r["id"] for r in mine] == [_id(WEIGHT, START + timedelta(minutes=1))]
    assert [r["id"] for r in theirs] == [_id(partner, START + timedelta(minutes=1))]

    # An ack on one phone changes nothing for the other.
    await _ask(client, ack=[mine[0]["id"]])
    assert (await _ask(client, url=f"/api/webhook/{'c' * 64}"))["pending"] == theirs


# --- What the phone refused ----------------------------------------------------------


async def test_permission_denied_opens_a_repair_until_the_type_is_written(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer, issue_registry: ir.IssueRegistry
) -> None:
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    entry = await _load(hass)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0)
    client = await hass_client_no_auth()
    reading = (await _ask(client))["pending"][0]

    answer = await _ask(client, failed=[{"id": reading["id"], "code": "permission_denied"}])
    assert answer["pending"] == []
    issue = issue_registry.async_get_issue(DOMAIN, f"writeback_{entry.entry_id}_weight")
    assert issue is not None
    assert issue.translation_key == "writeback_permission_denied"
    assert issue.translation_placeholders == {"phone": "Owen's Pixel", "type": "weight"}

    freezer.tick(timedelta(hours=1))
    await _set(hass, WEIGHT, 82.0)
    later = (await _ask(client))["pending"][0]
    await _ask(client, ack=[later["id"]])
    assert issue_registry.async_get_issue(DOMAIN, f"writeback_{entry.entry_id}_weight") is None


async def test_a_refusal_is_logged_without_the_value(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer, caplog
) -> None:
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    await _load(hass)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0)
    client = await hass_client_no_auth()
    reading = (await _ask(client))["pending"][0]

    await _ask(client, failed=[{"id": reading["id"], "code": "too_old"}])
    warnings = _our_warnings(caplog)
    assert len(warnings) == 1
    assert WEIGHT in warnings[0]
    assert "too_old" in warnings[0]
    assert "Accept older measurements" in warnings[0]
    assert "81" not in warnings[0]
    assert (await _ask(client))["pending"] == []


async def test_a_transient_refusal_keeps_the_reading(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer, caplog
) -> None:
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    await _load(hass)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0)
    client = await hass_client_no_auth()
    reading = (await _ask(client))["pending"][0]

    answer = await _ask(client, failed=[{"id": reading["id"], "code": "hc_unavailable"}])
    assert answer["pending"] == [reading]
    assert _our_warnings(caplog) == []


async def test_a_queue_failure_costs_the_phone_nothing(
    hass_tz: HomeAssistant, hass_client_no_auth, caplog
) -> None:
    """A bug in the direction to the phone: the sync is taken, the answer is bare."""
    hass = hass_tz
    entry = await _load(hass)
    client = await hass_client_no_auth()
    with patch(
        "custom_components.life_dashboard.writeback.WritebackManager.async_respond",
        side_effect=RuntimeError("boom"),
    ):
        response = await _post(
            client,
            {
                "timestamp": "2026-09-27T06:35:00Z",
                "source": "health_connect",
                "heart_rate": [{"bpm": 61, "time": "2026-09-27T06:30:00Z"}],
                "writeback": {"protocol": 1, "types": ["weight"]},
            },
        )
    assert response.status == HTTPStatus.OK
    answer = await response.json()
    assert answer["life_dashboard"]["writeback"] == 1
    assert answer["writeback"]["configured"] == ["weight"]
    assert "pending" not in answer["writeback"]
    assert entry.runtime_data.latest["heart_rate"].value == 61
    assert "Could not prepare the readings for Owen's Pixel" in caplog.text


async def test_a_failure_to_frame_the_answer_is_not_a_success(
    hass_tz: HomeAssistant, hass_client_no_auth
) -> None:
    """The bytes and the signature are made inside the try: a bug there is a 400."""
    hass = hass_tz
    await _load(hass)
    client = await hass_client_no_auth()
    body = json.dumps({"timestamp": "2026-09-27T06:35:00Z", "source": "health_connect"}).encode()
    headers = {"Content-Type": "application/json", SIGNATURE_HEADER: signature_for(SECRET, body)}
    with patch("custom_components.life_dashboard.payload.json.dumps", side_effect=TypeError("no")):
        response = await client.post(URL, data=body, headers=headers)
    assert response.status == HTTPStatus.BAD_REQUEST
    assert await response.read() == b""


async def test_a_screen_time_payload_gets_no_readings(
    hass_tz: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """The app puts the block in health payloads only; one elsewhere is not honoured."""
    hass = hass_tz
    await _set(hass, WEIGHT, 80.0)
    await _load(hass)
    freezer.tick(timedelta(minutes=1))
    await _set(hass, WEIGHT, 81.0)
    client = await hass_client_no_auth()
    response = await _post(
        client,
        {
            "timestamp": "2026-09-27T06:35:00Z",
            "source": "screen_time",
            "screen_time": [],
            "writeback": {"protocol": 1, "types": ["weight"]},
        },
    )
    assert response.status == HTTPStatus.OK
    answer = (await response.json())["writeback"]
    assert answer["configured"] == ["weight"]
    assert "pending" not in answer
    assert len((await _ask(client))["pending"]) == 1


async def test_a_newer_protocol_is_answered_with_ours(
    hass_tz: HomeAssistant, hass_client_no_auth
) -> None:
    hass = hass_tz
    await _load(hass)
    client = await hass_client_no_auth()
    response = await _post(
        client,
        {
            "timestamp": "2026-09-27T06:35:00Z",
            "source": "health_connect",
            "writeback": {"protocol": 2, "types": ["weight"], "ack": "not-a-list"},
        },
    )
    assert response.status == HTTPStatus.OK
    answer = await response.json()
    assert answer["life_dashboard"]["writeback"] == 1
    assert answer["writeback"]["pending"] == []
