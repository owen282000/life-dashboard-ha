"""Test the Life Dashboard config flow."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components import http
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

from custom_components.life_dashboard.config_flow import (
    CONF_REGENERATE_SECRET,
    CONF_USE_CLOUD,
    CloudUnavailable,
    _normalise_base_url,
)
from custom_components.life_dashboard.const import (
    CONF_BASE_URL,
    CONF_CLOUDHOOK_URL,
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    URL_CHOICE_CLOUD,
    URL_CHOICE_EXTERNAL,
    URL_CHOICE_URL,
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


@pytest.fixture
def browser_request():
    """Pretend the flow arrives from a browser on a given origin."""

    def setter(url: str, **headers: str):
        http.current_request.set(SimpleNamespace(url=URL(url), headers=headers))

    yield setter
    # Not reset(): the fixture tears down in another context than it set up in.
    http.current_request.set(None)


def _entry(**overrides) -> MockConfigEntry:
    data = {
        CONF_WEBHOOK_ID: WEBHOOK_ID,
        CONF_SECRET: SECRET,
        CONF_URL_CHOICE: URL_CHOICE_URL,
        CONF_BASE_URL: INTERNAL_URL,
    }
    data.update(overrides)
    return MockConfigEntry(domain=DOMAIN, title="Life Dashboard", unique_id=WEBHOOK_ID, data=data)


def _old_entry(choice: str) -> MockConfigEntry:
    """An entry as 0.4.0 and earlier wrote it: a choice, no address."""
    data = {CONF_WEBHOOK_ID: WEBHOOK_ID, CONF_SECRET: SECRET, CONF_URL_CHOICE: choice}
    return MockConfigEntry(domain=DOMAIN, title="Life Dashboard", unique_id=WEBHOOK_ID, data=data)


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


def _default(result, key: str):
    schema = result["data_schema"].schema
    return next(marker.default() for marker in schema if marker == key)


# --- Adding a phone --------------------------------------------------------


async def test_user_offers_the_address_the_browser_uses(
    hass: HomeAssistant, browser_request
) -> None:
    """What the user sees in the address bar is what the phone gets, until changed."""
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})
    browser_request("https://ha.example.net/api/config/config_entries/flow")

    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert _default(result, CONF_BASE_URL) == "https://ha.example.net"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Owen's Pixel", CONF_BASE_URL: "https://ha.example.net"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Owen's Pixel"
    assert result["data"] == {
        CONF_WEBHOOK_ID: WEBHOOK_ID,
        CONF_SECRET: SECRET,
        CONF_URL_CHOICE: URL_CHOICE_URL,
        CONF_BASE_URL: "https://ha.example.net",
    }
    # The user cannot pair without seeing these two.
    placeholders = result["description_placeholders"]
    assert placeholders["webhook_url"] == f"https://ha.example.net/api/webhook/{WEBHOOK_ID}"
    assert placeholders["secret"] == SECRET
    assert hass.config_entries.async_entries(DOMAIN)[0].unique_id == WEBHOOK_ID


async def test_a_proxy_that_home_assistant_does_not_trust_still_suggests_https(
    hass: HomeAssistant, browser_request
) -> None:
    browser_request(
        "http://ha.example.net/api/config/config_entries/flow", **{"X-Forwarded-Proto": "https"}
    )
    result = await _start(hass)
    assert _default(result, CONF_BASE_URL) == "https://ha.example.net"


async def test_user_falls_back_to_the_configured_urls(hass: HomeAssistant) -> None:
    """Without a request (a flow started from code) the internal URL is the guess."""
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})
    result = await _start(hass)
    assert _default(result, CONF_BASE_URL) == INTERNAL_URL


async def test_user_can_type_another_address(hass: HomeAssistant, browser_request) -> None:
    """The suggestion is a suggestion: the typed address wins, tidied up."""
    browser_request("http://192.168.1.10:8123/")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Phone", CONF_BASE_URL: " https://home.example.com/ "},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BASE_URL] == EXTERNAL_URL
    assert result["description_placeholders"]["webhook_url"] == (
        f"{EXTERNAL_URL}/api/webhook/{WEBHOOK_ID}"
    )


@pytest.mark.parametrize("bad", ["homeassistant.local:8123", "ftp://x", "", "https://"])
async def test_a_half_address_is_refused(hass: HomeAssistant, bad: str) -> None:
    """Say so at once rather than letting the first sync fail."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Phone", CONF_BASE_URL: bad}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_url"}
    assert not hass.config_entries.async_entries(DOMAIN)


def test_normalising_an_address() -> None:
    assert _normalise_base_url("https://home.example.com/") == "https://home.example.com"
    assert _normalise_base_url("http://192.168.1.10:8123") == "http://192.168.1.10:8123"
    # A pasted webhook URL loses its query and fragment but keeps its path; the
    # webhook path is appended, and the result is visibly wrong in the dialog.
    assert _normalise_base_url("https://x.example/?a=1#b") == "https://x.example"
    assert _normalise_base_url("home.example.com") is None


async def test_user_empty_name_falls_back(hass: HomeAssistant) -> None:
    """Whitespace is not a name."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "   ", CONF_BASE_URL: INTERNAL_URL}
    )
    assert result["title"] == "Life Dashboard"


async def test_cloud_not_offered_without_subscription(hass: HomeAssistant) -> None:
    """The cloud option only appears when there is a subscription to hang it on."""
    result = await _start(hass)
    assert CONF_USE_CLOUD not in result["data_schema"].schema


async def test_cloud(hass: HomeAssistant) -> None:
    """With a subscription, the cloudhook URL is what the phone gets."""
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
        assert CONF_USE_CLOUD in result["data_schema"].schema
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NAME: "Phone", CONF_BASE_URL: INTERNAL_URL, CONF_USE_CLOUD: True},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL_CHOICE] == URL_CHOICE_CLOUD
    assert result["data"][CONF_CLOUDHOOK_URL] == CLOUDHOOK_URL
    assert CONF_BASE_URL not in result["data"]
    assert result["description_placeholders"]["webhook_url"] == CLOUDHOOK_URL
    create.assert_awaited_once_with(hass, WEBHOOK_ID)


async def test_cloud_not_connected(hass: HomeAssistant) -> None:
    """A subscription is no guarantee the cloud is reachable right now."""
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
            {CONF_NAME: "Phone", CONF_BASE_URL: INTERNAL_URL, CONF_USE_CLOUD: True},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cloud_not_connected"}


async def test_two_phones(hass: HomeAssistant) -> None:
    """A household can have several phones, each its own entry and device."""
    _entry().add_to_hass(hass)

    with patch(
        "custom_components.life_dashboard.config_flow.webhook.async_generate_id",
        return_value="c" * 64,
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NAME: "Partner's iPhone", CONF_BASE_URL: INTERNAL_URL},
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
    entry = _entry()
    entry.add_to_hass(hass)

    result = await _start_reconfigure(hass, entry)
    assert result["type"] is FlowResultType.FORM
    placeholders = result["description_placeholders"]
    assert placeholders["webhook_url"] == f"{INTERNAL_URL}/api/webhook/{WEBHOOK_ID}"
    assert placeholders["secret"] == SECRET
    # The stored address, not the browser's, is what the form starts from.
    assert _default(result, CONF_BASE_URL) == INTERNAL_URL
    # No name field here: renaming is what the entry's own rename is for.
    assert CONF_NAME not in result["data_schema"].schema


async def test_reconfigure_changes_the_address_and_keeps_the_secret(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await _start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_BASE_URL: EXTERNAL_URL, CONF_REGENERATE_SECRET: False},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SECRET] == SECRET
    assert entry.data[CONF_BASE_URL] == EXTERNAL_URL
    assert result["description_placeholders"]["webhook_url"] == (
        f"{EXTERNAL_URL}/api/webhook/{WEBHOOK_ID}"
    )


async def test_reconfigure_regenerates_secret(hass: HomeAssistant) -> None:
    """A new secret is shown once, because the app needs it pasted twice."""
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
            {CONF_BASE_URL: INTERNAL_URL, CONF_REGENERATE_SECRET: True},
        )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SECRET] == new_secret
    assert result["description_placeholders"]["secret"] == new_secret
    # The address is unchanged, so pairing only needs the new secret.
    assert entry.data[CONF_WEBHOOK_ID] == WEBHOOK_ID


async def test_reconfigure_refuses_a_half_address(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    result = await _start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_URL: "home.example.com", CONF_REGENERATE_SECRET: False}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_url"}
    assert entry.data[CONF_BASE_URL] == INTERNAL_URL


async def test_reconfigure_away_from_cloud_deletes_the_cloudhook(hass: HomeAssistant) -> None:
    """Leaving a cloudhook behind would keep an unused public URL alive."""
    hass.config.components.add("cloud")
    entry = _entry(**{CONF_URL_CHOICE: URL_CHOICE_CLOUD, CONF_CLOUDHOOK_URL: CLOUDHOOK_URL})
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.life_dashboard.config_flow._cloud_has_subscription",
            return_value=True,
        ),
        patch(
            "custom_components.life_dashboard.config_flow._async_delete_cloudhook",
            AsyncMock(),
        ) as delete,
    ):
        result = await _start_reconfigure(hass, entry)
        assert _default(result, CONF_USE_CLOUD) is True
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_BASE_URL: INTERNAL_URL, CONF_USE_CLOUD: False, CONF_REGENERATE_SECRET: False},
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    delete.assert_awaited_once_with(hass, WEBHOOK_ID)
    assert CONF_CLOUDHOOK_URL not in entry.data
    assert entry.data[CONF_URL_CHOICE] == URL_CHOICE_URL


async def test_reconfigure_an_entry_from_before_the_address_field(hass: HomeAssistant) -> None:
    """Entries from 0.4.0 and earlier hold internal or external, not an address.

    The form resolves that choice the way those versions did, so the user sees the
    address the phone is actually using and can keep or change it.
    """
    await async_process_ha_core_config(
        hass, {"internal_url": INTERNAL_URL, "external_url": EXTERNAL_URL}
    )
    entry = _old_entry(URL_CHOICE_EXTERNAL)
    entry.add_to_hass(hass)

    result = await _start_reconfigure(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert _default(result, CONF_BASE_URL) == EXTERNAL_URL
    assert result["description_placeholders"]["webhook_url"] == (
        f"{EXTERNAL_URL}/api/webhook/{WEBHOOK_ID}"
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_URL: EXTERNAL_URL, CONF_REGENERATE_SECRET: False}
    )
    await hass.async_block_till_done()
    assert entry.data[CONF_URL_CHOICE] == URL_CHOICE_URL
    assert entry.data[CONF_BASE_URL] == EXTERNAL_URL


async def test_reconfigure_survives_a_missing_url(hass: HomeAssistant, browser_request) -> None:
    """An old external entry whose external URL was removed still opens.

    The form has to show the secret rather than fail with a traceback, and the
    address field falls back to what the browser is using.
    """
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})
    browser_request("http://homeassistant.local:8123/")
    entry = _old_entry(URL_CHOICE_EXTERNAL)
    entry.add_to_hass(hass)

    result = await _start_reconfigure(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["webhook_url"] == ""
    assert result["description_placeholders"]["secret"] == SECRET
    assert _default(result, CONF_BASE_URL) == INTERNAL_URL


# --- The QR ----------------------------------------------------------------


async def test_every_pairing_dialog_carries_the_qr_url(hass: HomeAssistant) -> None:
    """Wherever the secret is shown, the QR's URL is shown with it."""
    from custom_components.life_dashboard.pairing import pairing_url

    # Adding a phone.
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Phone", CONF_BASE_URL: INTERNAL_URL}
    )
    placeholders = result["description_placeholders"]
    expected_url = f"{INTERNAL_URL}/api/webhook/{WEBHOOK_ID}"
    assert placeholders["pair_url"] == pairing_url(expected_url, SECRET)
    # The element the frontend renders, carrying that same URL.
    assert placeholders["qr"].startswith('<ha-qr-code data="' + placeholders["pair_url"] + '"')
    assert placeholders["qr"].endswith("</ha-qr-code>")

    # The reconfigure form.
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    result = await _start_reconfigure(hass, entry)
    assert result["description_placeholders"]["pair_url"] == pairing_url(expected_url, SECRET)

    # The reconfigure result, with a rotated secret in the new code.
    new_secret = "d" * 64
    with patch(
        "custom_components.life_dashboard.config_flow.secrets.token_hex",
        return_value=new_secret,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_BASE_URL: INTERNAL_URL, CONF_REGENERATE_SECRET: True},
        )
    await hass.async_block_till_done()
    assert result["description_placeholders"]["pair_url"] == pairing_url(expected_url, new_secret)


def test_the_qr_element_is_in_every_pairing_text() -> None:
    """The three texts that show the secret also show the code."""
    with open("custom_components/life_dashboard/strings.json") as handle:
        config = json.load(handle)["config"]
    for text in (
        config["create_entry"]["default"],
        config["step"]["reconfigure"]["description"],
        config["abort"]["reconfigure_successful"],
    ):
        assert "{qr}" in text
        # The secret sits behind the fold, which is HTML and so also a placeholder.
        assert "{by_hand}" in text
        # hassfest refuses HTML in strings.json; the elements live in the placeholders.
        assert "<" not in text


async def test_the_fold_carries_the_url_and_the_secret(hass: HomeAssistant) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Phone", CONF_BASE_URL: INTERNAL_URL}
    )
    fold = result["description_placeholders"]["by_hand"]
    assert fold.startswith("<details><summary>")
    assert f"{INTERNAL_URL}/api/webhook/{WEBHOOK_ID}" in fold
    assert SECRET in fold
