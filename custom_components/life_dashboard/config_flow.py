"""Config flow for Life Dashboard.

Adding the integration asks two things: a name for the phone, and the address the
phone should send to. The address is filled in with the one the browser is using at
that moment, which is right far more often than a choice between "internal" and
"external" that depends on Settings > System > Network being filled in. Everything
else the integration decides: it generates the webhook id and the signing secret, and
shows both so they can be pasted into the app.

Signing is not a checkbox. An option to turn it off would only produce a worse
default, and the user has nothing to do for it either way.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import voluptuous as vol
from homeassistant.components import http, webhook
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError, get_url
from yarl import URL

from .const import (
    CONF_BASE_URL,
    CONF_CLOUDHOOK_URL,
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DEFAULT_NAME,
    DOMAIN,
    URL_CHOICE_CLOUD,
    URL_CHOICE_EXTERNAL,
    URL_CHOICE_URL,
)
from .pairing import by_hand_markup, layout_markup, pairing_url, qr_markup

_LOGGER = logging.getLogger(__name__)

CONF_REGENERATE_SECRET = "regenerate_secret"
CONF_USE_CLOUD = "use_cloud"

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


def _normalise_base_url(value: str) -> str | None:
    """A full http(s) origin, without a trailing slash, or None if it is not one.

    Anything the phone could not use as a base is refused here rather than at the
    first sync: a bare host name, a scheme the app does not speak, a pasted webhook
    URL with a query string.
    """
    try:
        url = URL(value.strip())
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or not url.host:
        return None
    return str(url.with_query(None).with_fragment(None)).rstrip("/")


@callback
def _suggested_base_url(hass: HomeAssistant) -> str:
    """The address to prefill: what the browser is using right now.

    Config flows arrive over the REST API, so the request is the user's own. Behind
    a reverse proxy that Home Assistant has not been told to trust, the scheme comes
    through as http; the proxy's own header is good enough for a suggestion the user
    sees and can change. Without a request, the configured URLs are the next best.
    """
    if (request := http.current_request.get()) is not None:
        origin = URL(str(request.url)).origin()
        if (forwarded := request.headers.get("X-Forwarded-Proto")) in ("http", "https"):
            origin = origin.with_scheme(forwarded)
        return str(origin)
    for allow in ({"allow_external": False}, {"allow_internal": False}):
        try:
            return get_url(hass, allow_cloud=False, **allow)
        except NoURLAvailableError:
            continue
    return ""


def _schema(
    hass: HomeAssistant, *, base_url: str, use_cloud: bool, reconfigure: bool
) -> vol.Schema:
    """Build the form schema. The name is asked when adding, not when reconfiguring."""
    fields: dict[Any, Any] = {}
    if not reconfigure:
        fields[vol.Required(CONF_NAME, default=DEFAULT_NAME)] = str
    fields[vol.Required(CONF_BASE_URL, default=base_url)] = str
    # Cloud is only offered with an active subscription, because without one there
    # is nothing to create a cloudhook on.
    if _cloud_has_subscription(hass):
        fields[vol.Optional(CONF_USE_CLOUD, default=use_cloud)] = bool
    if reconfigure:
        fields[vol.Optional(CONF_REGENERATE_SECRET, default=False)] = bool
    return vol.Schema(fields)


def _webhook_url(base_url: str, webhook_id: str) -> str:
    return f"{base_url}{webhook.async_generate_path(webhook_id)}"


@callback
def _stored_base_url(hass: HomeAssistant, data: dict[str, Any]) -> str:
    """The address an entry sends to, also for entries from before 0.5.0.

    Those hold an internal or external choice instead of an address; resolving it
    through Home Assistant's own URL helper gives the same answer they got then.
    """
    if base := data.get(CONF_BASE_URL):
        return base
    choice = data.get(CONF_URL_CHOICE)
    if choice in (URL_CHOICE_URL, URL_CHOICE_CLOUD):
        return ""
    try:
        return get_url(
            hass,
            allow_internal=choice != URL_CHOICE_EXTERNAL,
            allow_external=choice == URL_CHOICE_EXTERNAL,
            allow_cloud=False,
        )
    except NoURLAvailableError:
        return ""


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


def _pairing_placeholders(url: str, secret: str) -> dict[str, str]:
    """What every pairing dialog shows: the QR, and the two fields to paste by hand.

    An empty URL (reconfigure while the chosen address is not configured) still gets
    a QR; the app refuses it with a clear message, which beats a blank dialog.
    """
    pair_url = pairing_url(url, secret)
    return {
        "webhook_url": url,
        "secret": secret,
        "pair_url": pair_url,
        "qr": qr_markup(pair_url),
        "by_hand": by_hand_markup(url, secret),
        "layout": layout_markup(
            pair_url,
            note="Wrong address for the phone? <b>Reconfigure</b> on this integration gives "
            "a new code for another address.",
        ),
    }


class LifeDashboardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Life Dashboard."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add a phone: name it, confirm the address, hand back the URL and the secret."""
        errors: dict[str, str] = {}
        base_url = _suggested_base_url(self.hass)
        use_cloud = False

        if user_input is not None:
            base_url = user_input[CONF_BASE_URL]
            use_cloud = bool(user_input.get(CONF_USE_CLOUD))
            webhook_id = webhook.async_generate_id()
            secret = secrets.token_hex(SECRET_BYTES)
            data: dict[str, Any] = {CONF_WEBHOOK_ID: webhook_id, CONF_SECRET: secret}

            try:
                if use_cloud:
                    url = await _async_create_cloudhook(self.hass, webhook_id)
                    data[CONF_URL_CHOICE] = URL_CHOICE_CLOUD
                    data[CONF_CLOUDHOOK_URL] = url
                elif (normalised := _normalise_base_url(base_url)) is None:
                    errors["base"] = "invalid_url"
                else:
                    url = _webhook_url(normalised, webhook_id)
                    data[CONF_URL_CHOICE] = URL_CHOICE_URL
                    data[CONF_BASE_URL] = normalised
            except CloudUnavailable:
                errors["base"] = "cloud_not_connected"

            if not errors:
                await self.async_set_unique_id(webhook_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_NAME].strip() or DEFAULT_NAME,
                    data=data,
                    description_placeholders=_pairing_placeholders(url, secret),
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(
                self.hass, base_url=base_url, use_cloud=use_cloud, reconfigure=False
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the URL and secret again, and change the address or the secret."""
        entry = self._get_reconfigure_entry()
        webhook_id = entry.data[CONF_WEBHOOK_ID]
        was_cloud = entry.data.get(CONF_URL_CHOICE) == URL_CHOICE_CLOUD
        errors: dict[str, str] = {}
        base_url = _stored_base_url(self.hass, entry.data) or _suggested_base_url(self.hass)
        use_cloud = was_cloud

        if user_input is not None:
            base_url = user_input[CONF_BASE_URL]
            use_cloud = bool(user_input.get(CONF_USE_CLOUD))
            secret = (
                secrets.token_hex(SECRET_BYTES)
                if user_input.get(CONF_REGENERATE_SECRET)
                else entry.data[CONF_SECRET]
            )
            data = {**entry.data, CONF_SECRET: secret}
            data.pop(CONF_BASE_URL, None)
            data.pop(CONF_CLOUDHOOK_URL, None)

            try:
                if use_cloud:
                    url = await _async_create_cloudhook(self.hass, webhook_id)
                    data[CONF_URL_CHOICE] = URL_CHOICE_CLOUD
                    data[CONF_CLOUDHOOK_URL] = url
                elif (normalised := _normalise_base_url(base_url)) is None:
                    errors["base"] = "invalid_url"
                else:
                    url = _webhook_url(normalised, webhook_id)
                    data[CONF_URL_CHOICE] = URL_CHOICE_URL
                    data[CONF_BASE_URL] = normalised
            except CloudUnavailable:
                errors["base"] = "cloud_not_connected"

            if not errors:
                if was_cloud and not use_cloud:
                    await _async_delete_cloudhook(self.hass, webhook_id)

                # async_update_reload_and_abort takes no description_placeholders, and
                # showing the new URL and secret is the whole point of this step, so
                # save and reload by hand and abort with them in the message.
                self.hass.config_entries.async_update_entry(entry, data=data)
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(
                    reason="reconfigure_successful",
                    description_placeholders=_pairing_placeholders(url, secret),
                )

        if was_cloud:
            current_url = entry.data.get(CONF_CLOUDHOOK_URL, "")
        elif stored := _stored_base_url(self.hass, entry.data):
            current_url = _webhook_url(stored, webhook_id)
        else:
            current_url = ""

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(
                self.hass, base_url=base_url, use_cloud=use_cloud, reconfigure=True
            ),
            errors=errors,
            description_placeholders=_pairing_placeholders(current_url, entry.data[CONF_SECRET]),
        )
