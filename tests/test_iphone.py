"""An iPhone has no Receive: once the integration knows the phone is one, the
write-back options, the Send history to phone button, the queue and the action go away,
and the mapping stays in the options for a phone that moves back to Android."""

from http import HTTPStatus

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.life_dashboard.const import (
    CONF_PLATFORM,
    DOMAIN,
    PLATFORM_ANDROID,
    PLATFORM_IPHONE,
)

from .test_writeback import WEIGHT, WEIGHT_ONLY, _ask, _load, _post, _set

BUTTON = "button.owen_s_pixel_send_history_to_phone"


def _health(source: str) -> dict:
    return {"timestamp": "2026-09-27T06:35:00Z", "app_version": "1.11.0", "source": source}


async def test_an_iphone_payload_takes_receive_away(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _load(hass)
    await _set(hass, WEIGHT, 81.0)
    await _set(hass, WEIGHT, 81.4)
    assert entry.runtime_data.writeback.queue.pending
    assert er.async_get(hass).async_get(BUTTON) is not None

    client = await hass_client_no_auth()
    response = await _post(client, _health("healthkit_ios"))
    assert response.status == HTTPStatus.OK
    await hass.async_block_till_done()

    assert entry.data[CONF_PLATFORM] == PLATFORM_IPHONE
    assert entry.state is ConfigEntryState.LOADED
    writeback = entry.runtime_data.writeback
    assert writeback.receives is False
    assert writeback.configured == []
    # What waited for the phone is dropped, and nothing new is queued.
    assert not writeback.queue.pending
    await _set(hass, WEIGHT, 82.0)
    assert not writeback.queue.pending
    # The button is gone rather than unavailable; the mapping is kept.
    assert er.async_get(hass).async_get(BUTTON) is None
    assert entry.options["writeback"] == WEIGHT_ONLY["writeback"]


async def test_the_answer_to_an_iphone_lists_no_types(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _load(hass)
    client = await hass_client_no_auth()
    await _post(client, _health("healthkit_ios"))
    await hass.async_block_till_done()
    assert entry.runtime_data.writeback.configured == []


async def test_the_first_android_payload_reloads_nothing(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _load(hass)
    before = entry.runtime_data
    answer = await _ask(await hass_client_no_auth())
    await hass.async_block_till_done()

    assert entry.data[CONF_PLATFORM] == PLATFORM_ANDROID
    assert entry.runtime_data is before
    assert answer["configured"] == ["weight"]


async def test_back_to_android_brings_receive_back(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _load(hass)
    client = await hass_client_no_auth()
    await _post(client, _health("healthkit_ios"))
    await hass.async_block_till_done()
    assert entry.runtime_data.writeback.configured == []

    await _ask(client)
    await hass.async_block_till_done()

    assert entry.data[CONF_PLATFORM] == PLATFORM_ANDROID
    assert entry.runtime_data.writeback.configured == ["weight"]
    assert er.async_get(hass).async_get(BUTTON) is not None


async def test_the_options_of_an_iphone_have_no_mapping(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _load(hass)
    await _post(await hass_client_no_auth(), _health("healthkit_ios"))
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "iphone"
    assert [str(key) for key in result["data_schema"].schema] == ["min_workout_minutes"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"min_workout_minutes": 10}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["min_workout_minutes"] == 10
    assert entry.options["writeback"] == WEIGHT_ONLY["writeback"]


async def test_the_action_refuses_an_iphone(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await _load(hass)
    await _post(await hass_client_no_auth(), _health("healthkit_ios"))
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, "queue_history", {"config_entry": entry.entry_id, "days": 2}, blocking=True
        )
    assert raised.value.translation_key == "iphone_receives_nothing"
