"""Test the Life Dashboard config flow."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.life_dashboard.config_flow import (
    CONF_REGENERATE_SECRET,
    CloudUnavailable,
)
from custom_components.life_dashboard.const import (
    CONF_CLOUDHOOK_URL,
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    URL_CHOICE_CLOUD,
    URL_CHOICE_EXTERNAL,
    URL_CHOICE_INTERNAL,
)

WEBHOOK_ID = "a" * 64
SECRET = "b" * 64
INTERNAL_URL = "http://homeassistant.local:8123"
EXTERNAL_URL = "https://home.example.com"
CLOUDHOOK_URL = "https://hooks.nabu.casa/ABC123"


@pytest.fixture(autouse=True)
def _fixed_ids():
    """Fix the generated webhook id and secret so assertions can name them."""
    with (
        patch(
            "custom_components.life_dashboard.config_flow.webhook.async_generate_id",
            return_value=WEBHOOK_ID,
        ),
        patch(
            "custom_components.life_dashboard.config_flow.secrets.token_hex",
            return_value=SECRET,
        ),
    ):
        yield


def _entry(**overrides) -> MockConfigEntry:
    data = {
        CONF_WEBHOOK_ID: WEBHOOK_ID,
        CONF_SECRET: SECRET,
        CONF_URL_CHOICE: URL_CHOICE_INTERNAL,
    }
    data.update(overrides)
    return MockConfigEntry(domain=DOMAIN, title="Life Dashboard", unique_id=WEBHOOK_ID, data=data)


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


# --- Adding a phone --------------------------------------------------------


async def test_user_internal(hass: HomeAssistant) -> None:
    """The happy path: a name, the internal URL, and both fields to paste."""
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})

    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Owen's Pixel", CONF_URL_CHOICE: URL_CHOICE_INTERNAL},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Owen's Pixel"
    assert result["data"] == {
        CONF_WEBHOOK_ID: WEBHOOK_ID,
        CONF_SECRET: SECRET,
        CONF_URL_CHOICE: URL_CHOICE_INTERNAL,
    }
    # The user cannot pair without seeing these two.
    assert result["description_placeholders"] == {
        "webhook_url": f"{INTERNAL_URL}/api/webhook/{WEBHOOK_ID}",
        "secret": SECRET,
    }

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == WEBHOOK_ID


async def test_user_external(hass: HomeAssistant) -> None:
    """Choosing external gives the external URL, not the internal one."""
    await async_process_ha_core_config(
        hass, {"internal_url": INTERNAL_URL, "external_url": EXTERNAL_URL}
    )

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Phone", CONF_URL_CHOICE: URL_CHOICE_EXTERNAL},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["description_placeholders"]["webhook_url"] == (
        f"{EXTERNAL_URL}/api/webhook/{WEBHOOK_ID}"
    )


async def test_user_external_not_configured(hass: HomeAssistant) -> None:
    """Say so at once rather than letting the first sync fail."""
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Phone", CONF_URL_CHOICE: URL_CHOICE_EXTERNAL},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_external_url"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_user_empty_name_falls_back(hass: HomeAssistant) -> None:
    """Whitespace is not a name."""
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "   ", CONF_URL_CHOICE: URL_CHOICE_INTERNAL}
    )
    assert result["title"] == "Life Dashboard"


async def test_cloud_not_offered_without_subscription(hass: HomeAssistant) -> None:
    """The cloud option only appears when there is a subscription to hang it on."""
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})

    result = await _start(hass)
    options = result["data_schema"].schema[CONF_URL_CHOICE].config["options"]
    assert options == [URL_CHOICE_INTERNAL, URL_CHOICE_EXTERNAL]


async def test_cloud(hass: HomeAssistant) -> None:
    """With a subscription, the cloudhook URL is what the phone gets."""
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})
    hass.config.components.add("cloud")

    with (
        patch(
            "custom_components.life_dashboard.config_flow._cloud_has_subscription",
            return_value=True,
        ),
        patch(
            "custom_components.life_dashboard.config_flow._async_create_cloudhook",
            AsyncMock(return_value=CLOUDHOOK_URL),
        ) as create,
    ):
        result = await _start(hass)
        options = result["data_schema"].schema[CONF_URL_CHOICE].config["options"]
        assert URL_CHOICE_CLOUD in options

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NAME: "Phone", CONF_URL_CHOICE: URL_CHOICE_CLOUD},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CLOUDHOOK_URL] == CLOUDHOOK_URL
    assert result["description_placeholders"]["webhook_url"] == CLOUDHOOK_URL
    create.assert_awaited_once_with(hass, WEBHOOK_ID)


async def test_cloud_not_connected(hass: HomeAssistant) -> None:
    """A subscription is no guarantee the cloud is reachable right now."""
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})
    hass.config.components.add("cloud")

    with (
        patch(
            "custom_components.life_dashboard.config_flow._cloud_has_subscription",
            return_value=True,
        ),
        patch(
            "custom_components.life_dashboard.config_flow._async_create_cloudhook",
            AsyncMock(side_effect=CloudUnavailable),
        ),
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NAME: "Phone", CONF_URL_CHOICE: URL_CHOICE_CLOUD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cloud_not_connected"}


async def test_two_phones(hass: HomeAssistant) -> None:
    """A household can have several phones, each its own entry and device."""
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})
    _entry().add_to_hass(hass)

    with patch(
        "custom_components.life_dashboard.config_flow.webhook.async_generate_id",
        return_value="c" * 64,
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NAME: "Partner's iPhone", CONF_URL_CHOICE: URL_CHOICE_INTERNAL},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


# --- Reconfigure -----------------------------------------------------------


async def _start_reconfigure(hass: HomeAssistant, entry: MockConfigEntry):
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )


async def test_reconfigure_shows_current_pairing(hass: HomeAssistant) -> None:
    """The form is also the answer to "what was my secret again?"."""
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})
    entry = _entry()
    entry.add_to_hass(hass)

    result = await _start_reconfigure(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"] == {
        "webhook_url": f"{INTERNAL_URL}/api/webhook/{WEBHOOK_ID}",
        "secret": SECRET,
    }
    # No name field here: renaming is what the entry's own rename is for.
    assert CONF_NAME not in result["data_schema"].schema


async def test_reconfigure_keeps_secret_by_default(hass: HomeAssistant) -> None:
    await async_process_ha_core_config(
        hass, {"internal_url": INTERNAL_URL, "external_url": EXTERNAL_URL}
    )
    entry = _entry()
    entry.add_to_hass(hass)

    result = await _start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL_CHOICE: URL_CHOICE_EXTERNAL, CONF_REGENERATE_SECRET: False},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SECRET] == SECRET
    assert entry.data[CONF_URL_CHOICE] == URL_CHOICE_EXTERNAL
    assert result["description_placeholders"]["webhook_url"] == (
        f"{EXTERNAL_URL}/api/webhook/{WEBHOOK_ID}"
    )


async def test_reconfigure_regenerates_secret(hass: HomeAssistant) -> None:
    """A new secret is shown once, because the app needs it pasted twice."""
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})
    entry = _entry()
    entry.add_to_hass(hass)
    new_secret = "d" * 64

    result = await _start_reconfigure(hass, entry)
    with patch(
        "custom_components.life_dashboard.config_flow.secrets.token_hex",
        return_value=new_secret,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL_CHOICE: URL_CHOICE_INTERNAL, CONF_REGENERATE_SECRET: True},
        )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SECRET] == new_secret
    assert result["description_placeholders"]["secret"] == new_secret
    # The address is unchanged, so pairing only needs the new secret.
    assert entry.data[CONF_WEBHOOK_ID] == WEBHOOK_ID


async def test_reconfigure_away_from_cloud_deletes_the_cloudhook(hass: HomeAssistant) -> None:
    """Leaving a cloudhook behind would keep an unused public URL alive."""
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})
    hass.config.components.add("cloud")
    entry = _entry(**{CONF_URL_CHOICE: URL_CHOICE_CLOUD, CONF_CLOUDHOOK_URL: CLOUDHOOK_URL})
    entry.add_to_hass(hass)

    with patch(
        "custom_components.life_dashboard.config_flow._async_delete_cloudhook",
        AsyncMock(),
    ) as delete:
        result = await _start_reconfigure(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL_CHOICE: URL_CHOICE_INTERNAL, CONF_REGENERATE_SECRET: False},
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    delete.assert_awaited_once_with(hass, WEBHOOK_ID)
    assert CONF_CLOUDHOOK_URL not in entry.data


async def test_reconfigure_survives_a_missing_url(hass: HomeAssistant) -> None:
    """Someone can remove the external URL between pairing and reconfiguring.

    The form still has to open, showing the secret, rather than failing with a
    traceback and leaving the user without their pairing details. An internal URL
    always resolves (Home Assistant falls back to the local address), so external
    is the case that can actually go missing.
    """
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})
    entry = _entry(**{CONF_URL_CHOICE: URL_CHOICE_EXTERNAL})
    entry.add_to_hass(hass)

    result = await _start_reconfigure(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["webhook_url"] == ""
    # The secret is the part the user came for.
    assert result["description_placeholders"]["secret"] == SECRET
