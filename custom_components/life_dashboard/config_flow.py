"""Config flow for Life Dashboard.

Adding the integration asks two things: a name for the phone, and which address the
phone should send to. Everything else the integration decides: it generates the
webhook id and the signing secret, and shows both so they can be pasted into the app.

Signing is not a checkbox. An option to turn it off would only produce a worse
default, and the user has nothing to do for it either way.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import voluptuous as vol
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_CLOUDHOOK_URL,
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DEFAULT_NAME,
    DOMAIN,
    URL_CHOICE_CLOUD,
    URL_CHOICE_EXTERNAL,
    URL_CHOICE_INTERNAL,
)

_LOGGER = logging.getLogger(__name__)

CONF_REGENERATE_SECRET = "regenerate_secret"

# The app generates 32 random bytes as 64 hex characters; matching that keeps the
# two fields the user pastes recognisably alike.
SECRET_BYTES = 32


@callback
def _cloud_has_subscription(hass: HomeAssistant) -> bool:
    """Whether Home Assistant Cloud can serve a cloudhook right now.

    The import is local on purpose: cloud is an after_dependency, so it can be
    absent, and on a minimal installation importing it fails outright.
    """
    if "cloud" not in hass.config.components:
        return False
    try:
        from homeassistant.components import cloud
    except ImportError:
        return False
    return cloud.async_active_subscription(hass)


@callback
def _url_choices(hass: HomeAssistant) -> list[str]:
    """The addresses the user can pick from.

    Cloud is only offered with an active subscription, because without one there is
    nothing to create a cloudhook on.
    """
    choices = [URL_CHOICE_INTERNAL, URL_CHOICE_EXTERNAL]
    if _cloud_has_subscription(hass):
        choices.append(URL_CHOICE_CLOUD)
    return choices


def _schema(hass: HomeAssistant, *, default_choice: str, reconfigure: bool) -> vol.Schema:
    """Build the form schema. The name is asked when adding, not when reconfiguring."""
    fields: dict[Any, Any] = {}
    if not reconfigure:
        fields[vol.Required(CONF_NAME, default=DEFAULT_NAME)] = str
    fields[vol.Required(CONF_URL_CHOICE, default=default_choice)] = SelectSelector(
        SelectSelectorConfig(
            options=_url_choices(hass),
            translation_key="url_choice",
            mode=SelectSelectorMode.LIST,
        )
    )
    if reconfigure:
        fields[vol.Optional(CONF_REGENERATE_SECRET, default=False)] = bool
    return vol.Schema(fields)


def _plain_url(hass: HomeAssistant, choice: str, webhook_id: str) -> str:
    """The internal or external webhook URL.

    Home Assistant's own async_generate_url never returns a cloud URL, so the cloud
    case is a cloudhook and handled separately.
    """
    url = get_url(
        hass,
        allow_internal=choice == URL_CHOICE_INTERNAL,
        allow_external=choice == URL_CHOICE_EXTERNAL,
        allow_cloud=False,
    )
    return f"{url}{webhook.async_generate_path(webhook_id)}"


class CloudUnavailable(Exception):
    """Home Assistant Cloud could not give us a cloudhook.

    Raised in place of the cloud component's own exception, so the flow's except
    clauses never need to import that component. It is an after_dependency: it can
    be absent, and on a minimal installation importing it fails outright.
    """


async def _async_create_cloudhook(hass: HomeAssistant, webhook_id: str) -> str:
    """Create the cloudhook, or reuse the one this webhook id already has."""
    try:
        from homeassistant.components import cloud
    except ImportError as err:
        raise CloudUnavailable from err

    try:
        return await cloud.async_get_or_create_cloudhook(hass, webhook_id)
    except cloud.CloudNotAvailable as err:
        raise CloudUnavailable from err


async def _async_delete_cloudhook(hass: HomeAssistant, webhook_id: str) -> None:
    """Drop a cloudhook we no longer use, without a fuss if it is already gone."""
    if "cloud" not in hass.config.components:
        return
    try:
        from homeassistant.components import cloud

        await cloud.async_delete_cloudhook(hass, webhook_id)
    except ImportError:
        return
    except (cloud.CloudNotAvailable, ValueError):
        _LOGGER.debug("Could not delete the cloudhook for %s", webhook_id)


class LifeDashboardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Life Dashboard."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add a phone: name it, pick an address, hand back the URL and the secret."""
        errors: dict[str, str] = {}
        choice = URL_CHOICE_INTERNAL

        if user_input is not None:
            choice = user_input[CONF_URL_CHOICE]
            webhook_id = webhook.async_generate_id()
            secret = secrets.token_hex(SECRET_BYTES)
            data: dict[str, Any] = {
                CONF_WEBHOOK_ID: webhook_id,
                CONF_SECRET: secret,
                CONF_URL_CHOICE: choice,
            }

            try:
                if choice == URL_CHOICE_CLOUD:
                    url = await _async_create_cloudhook(self.hass, webhook_id)
                    data[CONF_CLOUDHOOK_URL] = url
                else:
                    url = _plain_url(self.hass, choice, webhook_id)
            except CloudUnavailable:
                errors["base"] = "cloud_not_connected"
            except NoURLAvailableError:
                errors["base"] = f"no_{choice}_url"
            else:
                await self.async_set_unique_id(webhook_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_NAME].strip() or DEFAULT_NAME,
                    data=data,
                    description_placeholders={"webhook_url": url, "secret": secret},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(self.hass, default_choice=choice, reconfigure=False),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the URL and secret again, and change the address or the secret."""
        entry = self._get_reconfigure_entry()
        webhook_id = entry.data[CONF_WEBHOOK_ID]
        old_choice = entry.data[CONF_URL_CHOICE]
        errors: dict[str, str] = {}

        if user_input is not None:
            choice = user_input[CONF_URL_CHOICE]
            secret = (
                secrets.token_hex(SECRET_BYTES)
                if user_input.get(CONF_REGENERATE_SECRET)
                else entry.data[CONF_SECRET]
            )
            data = {**entry.data, CONF_SECRET: secret, CONF_URL_CHOICE: choice}

            try:
                if choice == URL_CHOICE_CLOUD:
                    url = await _async_create_cloudhook(self.hass, webhook_id)
                    data[CONF_CLOUDHOOK_URL] = url
                else:
                    url = _plain_url(self.hass, choice, webhook_id)
                    data.pop(CONF_CLOUDHOOK_URL, None)
            except CloudUnavailable:
                errors["base"] = "cloud_not_connected"
            except NoURLAvailableError:
                errors["base"] = f"no_{choice}_url"
            else:
                if old_choice == URL_CHOICE_CLOUD and choice != URL_CHOICE_CLOUD:
                    await _async_delete_cloudhook(self.hass, webhook_id)

                # async_update_reload_and_abort takes no description_placeholders, and
                # showing the new URL and secret is the whole point of this step, so
                # save and reload by hand and abort with them in the message.
                self.hass.config_entries.async_update_entry(entry, data=data)
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(
                    reason="reconfigure_successful",
                    description_placeholders={"webhook_url": url, "secret": secret},
                )

        try:
            current_url = (
                entry.data.get(CONF_CLOUDHOOK_URL, "")
                if old_choice == URL_CHOICE_CLOUD
                else _plain_url(self.hass, old_choice, webhook_id)
            )
        except NoURLAvailableError:
            current_url = ""

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(self.hass, default_choice=old_choice, reconfigure=True),
            errors=errors,
            description_placeholders={
                "webhook_url": current_url,
                "secret": entry.data[CONF_SECRET],
            },
        )
