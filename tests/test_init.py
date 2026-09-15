"""Test setting up and unloading a Life Dashboard config entry."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.life_dashboard.const import (
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    URL_CHOICE_INTERNAL,
)

WEBHOOK_ID = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
SECRET = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"


async def test_setup_and_unload(hass: HomeAssistant) -> None:
    """An entry loads and unloads again."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Life Dashboard",
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
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
