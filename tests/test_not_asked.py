"""A mapped type the phone stops asking for gets a repair after three requests in a row.

The app lists every type that is switched on under Receive and may be written, whether
or not anything was measured: a type it leaves out was switched off or lost its
permission. A request without a writeback block says nothing about types.
"""

from http import HTTPStatus

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.life_dashboard.const import DOMAIN

from .test_writeback import _ask, _load, _post


def _issue(hass: HomeAssistant, entry, kind: str = "weight"):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"writeback_{entry.entry_id}_{kind}_not_asked"
    )


async def test_three_requests_without_the_type_raise_a_repair(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _load(hass)
    client = await hass_client_no_auth()
    for _ in range(2):
        await _ask(client, types=())
        assert _issue(hass, entry) is None
    await _ask(client, types=())

    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.translation_key == "writeback_not_asked"
    assert issue.translation_placeholders == {"phone": "Owen's Pixel", "type": "weight"}

    # Switched on again: the repair goes by itself.
    await _ask(client, types=("weight",))
    assert _issue(hass, entry) is None


async def test_a_type_that_is_asked_for_without_new_readings_raises_nothing(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Weight on in the app, but nobody stepped on the scale for weeks."""
    entry = await _load(hass)
    client = await hass_client_no_auth()
    for _ in range(6):
        answer = await _ask(client, types=("weight",))
        assert answer.get("pending", []) == []
    assert _issue(hass, entry) is None
    assert entry.runtime_data.writeback.queue.not_asked == {}


async def test_a_request_without_a_writeback_block_does_not_count(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _load(hass)
    client = await hass_client_no_auth()
    for _ in range(4):
        response = await _post(
            client,
            {"timestamp": "2026-09-27T06:35:00Z", "app_version": "1.30.0", "source": "screen_time"},
        )
        assert response.status == HTTPStatus.OK
    assert _issue(hass, entry) is None
    assert entry.runtime_data.writeback.queue.not_asked == {}


async def test_the_count_starts_over_when_the_type_comes_back(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _load(hass)
    client = await hass_client_no_auth()
    await _ask(client, types=())
    await _ask(client, types=())
    await _ask(client, types=("weight",))
    await _ask(client, types=())
    await _ask(client, types=())
    assert _issue(hass, entry) is None
    assert entry.runtime_data.writeback.queue.not_asked == {"weight": 2}


async def test_the_count_survives_a_reload(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await _load(hass)
    client = await hass_client_no_auth()
    await _ask(client, types=())
    await _ask(client, types=())
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    await _ask(await hass_client_no_auth(), types=())
    assert _issue(hass, entry) is not None


async def test_only_mapped_types_are_counted(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await _load(hass)
    client = await hass_client_no_auth()
    for _ in range(3):
        await _ask(client, types=("weight",))
    assert _issue(hass, entry, "blood_pressure") is None
    assert entry.runtime_data.writeback.queue.not_asked == {}
