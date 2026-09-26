"""Test the options flow: which entities go to this phone.

The form is one collapsed section per type. What matters is what gets refused when
saving: the phone's own sensor, an entity chosen twice, and an entity whose unit is
not what the type needs, because each of those would put wrong data in a health
record without anyone noticing until much later.
"""

import json

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.life_dashboard.config_flow import _validate_mapping
from custom_components.life_dashboard.const import (
    CONF_BASE_URL,
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    URL_CHOICE_URL,
)
from custom_components.life_dashboard.writeback_queue import WRITEBACK_TYPES

WEBHOOK_ID = "a" * 64
SECRET = "b" * 64
SCALE = "sensor.scale_weight"
FAT = "sensor.scale_body_fat"
SYSTOLIC = "sensor.omron_systolic"
DIASTOLIC = "sensor.omron_diastolic"


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Owen's Pixel",
        unique_id=WEBHOOK_ID,
        data={
            CONF_WEBHOOK_ID: WEBHOOK_ID,
            CONF_SECRET: SECRET,
            CONF_URL_CHOICE: URL_CHOICE_URL,
            CONF_BASE_URL: "http://homeassistant.local:8123",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _form(**sections: dict) -> dict:
    """User input as the frontend sends it: every section, filled or empty."""
    return {kind: sections.get(kind, {}) for kind in WRITEBACK_TYPES}


async def _start(hass: HomeAssistant, entry: MockConfigEntry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    return result


async def _submit(hass: HomeAssistant, entry: MockConfigEntry, user_input: dict):
    result = await _start(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    return result


def _set(hass: HomeAssistant, entity_id: str, value: str, unit: str | None) -> None:
    attributes = {"unit_of_measurement": unit} if unit else {}
    hass.states.async_set(entity_id, value, attributes)


# --- Saving ------------------------------------------------------------------


async def test_a_weight_mapping_is_saved_and_the_entry_reloads(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    _set(hass, SCALE, "81.4", "kg")
    result = await _submit(hass, entry, _form(weight={"entity": SCALE}))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"writeback": {"weight": {"entity": SCALE}}}
    assert entry.state is ConfigEntryState.LOADED
    # The reload set the listeners: the manager knows the mapping.
    assert entry.runtime_data.writeback.configured == ["weight"]


async def test_every_slot_of_a_full_mapping(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    _set(hass, SCALE, "81.4", "kg")
    _set(hass, FAT, "20.1", "%")
    _set(hass, "sensor.scale_height", "181", "cm")
    _set(hass, "sensor.scale_lean", "60", "kg")
    _set(hass, "sensor.scale_bone", "3.1", "kg")
    _set(hass, "sensor.scale_water", "45", "kg")
    _set(hass, SYSTOLIC, "128", "mmHg")
    _set(hass, DIASTOLIC, "82", "kPa")
    hass.states.async_set(
        "sensor.scale_time", "2026-09-27T06:30:00+00:00", {"device_class": "timestamp"}
    )

    result = await _submit(
        hass,
        entry,
        _form(
            weight={"entity": SCALE, "time_entity": "sensor.scale_time"},
            height={"entity": "sensor.scale_height"},
            body_fat={"entity": FAT},
            lean_body_mass={"entity": "sensor.scale_lean"},
            bone_mass={"entity": "sensor.scale_bone"},
            body_water_mass={"entity": "sensor.scale_water"},
            blood_pressure={
                "systolic": SYSTOLIC,
                "diastolic": DIASTOLIC,
                "body_position": "sitting_down",
                "measurement_location": "left_upper_arm",
            },
        ),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["writeback"] == {
        "weight": {"entity": SCALE, "time_entity": "sensor.scale_time"},
        "height": {"entity": "sensor.scale_height"},
        "body_fat": {"entity": FAT},
        "lean_body_mass": {"entity": "sensor.scale_lean"},
        "bone_mass": {"entity": "sensor.scale_bone"},
        "body_water_mass": {"entity": "sensor.scale_water"},
        "blood_pressure": {
            "systolic": SYSTOLIC,
            "diastolic": DIASTOLIC,
            "body_position": "sitting_down",
            "measurement_location": "left_upper_arm",
        },
    }
    assert entry.runtime_data.writeback.configured == list(WRITEBACK_TYPES)


async def test_clearing_every_slot_sends_nothing(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    _set(hass, SCALE, "81.4", "kg")
    await _submit(hass, entry, _form(weight={"entity": SCALE}))
    result = await _submit(hass, entry, _form())
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"writeback": {}}
    assert entry.runtime_data.writeback.configured == []


async def test_the_form_suggests_the_current_mapping(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    _set(hass, SCALE, "81.4", "kg")
    await _submit(hass, entry, _form(weight={"entity": SCALE}))

    result = await _start(hass, entry)
    weight_section = next(marker for marker in result["data_schema"].schema if marker == "weight")
    inner = result["data_schema"].schema[weight_section].schema.schema
    entity_marker = next(marker for marker in inner if marker == "entity")
    assert entity_marker.description == {"suggested_value": SCALE}
    # Every section is collapsed: the screen is "open Weight, pick, save".
    for marker, value in result["data_schema"].schema.items():
        assert value.options == {"collapsed": True}, marker


async def test_the_unit_can_come_from_the_registry(
    hass: HomeAssistant, entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """An entity without a state yet (its integration is not loaded) still validates."""
    entity_registry.async_get_or_create(
        "sensor",
        "xiaomi_ble",
        "scale-mass",
        suggested_object_id="scale_weight",
        unit_of_measurement="kg",
    )
    result = await _submit(hass, entry, _form(weight={"entity": SCALE}))
    assert result["type"] is FlowResultType.CREATE_ENTRY


# --- Refusals -----------------------------------------------------------------


async def test_the_phones_own_sensor_is_refused(
    hass: HomeAssistant, entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """Sending the phone its own weight back would echo it into Health Connect.

    The picker leaves it out, and the schema refuses it before the flow sees it; the
    validation behind that is the second layer, for a value that did not come
    through the picker.
    """
    own = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_weight",
        suggested_object_id="owen_s_pixel_weight",
        config_entry=entry,
        unit_of_measurement="kg",
    )
    hass.states.async_set(own.entity_id, "81.4", {"unit_of_measurement": "kg"})

    result = await _start(hass, entry)
    weight_section = next(marker for marker in result["data_schema"].schema if marker == "weight")
    inner = result["data_schema"].schema[weight_section].schema.schema
    picker = next(value for marker, value in inner.items() if marker == "entity")
    assert picker.config["exclude_entities"] == [own.entity_id]

    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], _form(weight={"entity": own.entity_id})
        )
    assert "writeback" not in entry.options

    mapping, errors, placeholders = _validate_mapping(
        hass, _form(weight={"entity": own.entity_id}), [own.entity_id]
    )
    assert mapping == {}
    assert errors == {"base": "entity_is_own_sensor"}
    assert placeholders == {"entity": own.entity_id}


async def test_an_entity_chosen_twice_is_refused(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    _set(hass, SCALE, "81.4", "kg")
    result = await _submit(
        hass, entry, _form(weight={"entity": SCALE}, lean_body_mass={"entity": SCALE})
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "entity_twice"}
    assert result["description_placeholders"] == {"entity": SCALE}


@pytest.mark.parametrize(
    ("kind", "unit", "error"),
    [
        ("weight", "L", "unit_not_mass"),
        ("weight", None, "unit_not_mass"),
        ("lean_body_mass", "%", "unit_not_mass"),
        ("height", "kg", "unit_not_length"),
        ("body_fat", "kg", "unit_not_percentage"),
    ],
)
async def test_a_wrong_unit_is_refused(
    hass: HomeAssistant, entry: MockConfigEntry, kind: str, unit: str | None, error: str
) -> None:
    _set(hass, SCALE, "81.4", unit)
    result = await _submit(hass, entry, _form(**{kind: {"entity": SCALE}}))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert result["description_placeholders"] == {"entity": SCALE}


async def test_blood_pressure_needs_pressure_units_and_both_halves(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    _set(hass, SYSTOLIC, "128", "mmHg")
    _set(hass, DIASTOLIC, "82", "bpm")
    result = await _submit(
        hass, entry, _form(blood_pressure={"systolic": SYSTOLIC, "diastolic": DIASTOLIC})
    )
    assert result["errors"] == {"base": "unit_not_pressure"}
    assert result["description_placeholders"] == {"entity": DIASTOLIC}

    result = await _submit(hass, entry, _form(blood_pressure={"systolic": SYSTOLIC}))
    assert result["errors"] == {"base": "blood_pressure_incomplete"}


async def test_the_timestamp_entity_is_checked_too(
    hass: HomeAssistant, entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """The phone's own last sync would make every weighing the sync time."""
    _set(hass, SCALE, "81.4", "kg")
    _set(hass, FAT, "20.1", "%")
    hass.states.async_set(
        "sensor.scale_time", "2026-09-27T06:30:00+00:00", {"device_class": "timestamp"}
    )
    own = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_last_health_sync",
        suggested_object_id="owen_s_pixel_last_health_sync",
        config_entry=entry,
        original_device_class="timestamp",
    )
    hass.states.async_set(own.entity_id, "2026-09-27T06:00:00+00:00", {"device_class": "timestamp"})

    # The picker leaves the phone's own sensors out; underneath, they are refused too.
    result = await _start(hass, entry)
    weight_section = next(marker for marker in result["data_schema"].schema if marker == "weight")
    inner = result["data_schema"].schema[weight_section].schema.schema
    picker = next(value for marker, value in inner.items() if marker == "time_entity")
    assert picker.config["exclude_entities"] == [own.entity_id]
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], _form(weight={"entity": SCALE, "time_entity": own.entity_id})
        )
    _, errors, placeholders = _validate_mapping(
        hass, _form(weight={"entity": SCALE, "time_entity": own.entity_id}), [own.entity_id]
    )
    assert errors == {"base": "entity_is_own_sensor"}
    assert placeholders == {"entity": own.entity_id}

    # A value entity, of this type or of another, is not a timestamp.
    result = await _submit(hass, entry, _form(weight={"entity": SCALE, "time_entity": SCALE}))
    assert result["errors"] == {"base": "entity_twice"}
    result = await _submit(
        hass, entry, _form(weight={"entity": SCALE, "time_entity": FAT}, body_fat={"entity": FAT})
    )
    assert result["errors"] == {"base": "entity_twice"}
    assert result["description_placeholders"] == {"entity": FAT}

    # A sensor without the timestamp device class cannot give a moment.
    _set(hass, "sensor.scale_impedance", "512", "Ω")
    result = await _submit(
        hass, entry, _form(weight={"entity": SCALE, "time_entity": "sensor.scale_impedance"})
    )
    assert result["errors"] == {"base": "time_entity_not_timestamp"}
    assert result["description_placeholders"] == {"entity": "sensor.scale_impedance"}

    # One timestamp for several types is what a scale gives.
    result = await _submit(
        hass,
        entry,
        _form(
            weight={"entity": SCALE, "time_entity": "sensor.scale_time"},
            body_fat={"entity": FAT, "time_entity": "sensor.scale_time"},
        ),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["writeback"]["weight"]["time_entity"] == "sensor.scale_time"
    assert entry.options["writeback"]["body_fat"]["time_entity"] == "sensor.scale_time"


# --- The texts -------------------------------------------------------------------


def test_every_section_slot_and_error_has_a_text() -> None:
    """A missing key shows up as a raw identifier in the dialog."""
    with open("custom_components/life_dashboard/strings.json") as handle:
        options = json.load(handle)["options"]
    sections = options["step"]["init"]["sections"]
    assert set(sections) == set(WRITEBACK_TYPES)
    for kind, texts in sections.items():
        assert texts["name"], kind
        expected = {"time_entity"} | (
            {"systolic", "diastolic", "body_position", "measurement_location"}
            if kind == "blood_pressure"
            else {"entity"}
        )
        assert set(texts["data"]) == expected, kind
    assert set(options["error"]) == {
        "entity_is_own_sensor",
        "entity_twice",
        "unit_not_mass",
        "unit_not_length",
        "unit_not_percentage",
        "unit_not_pressure",
        "blood_pressure_incomplete",
        "time_entity_not_timestamp",
    }
