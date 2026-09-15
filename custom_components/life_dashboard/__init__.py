"""The Life Dashboard integration.

Receives webhook payloads from the Life Dashboard Companion apps, verifies the
signature, and dispatches the sensor updates a payload justifies.

The one rule worth knowing: an update is applied only when it is not older than the
value a sensor already holds. The app re-sends a batch after a failed delivery, from
its outbox at the next sync, and wholesale during a backfill, so the same record
arrives repeatedly with its original timestamps. Ordering on the moment a value
describes makes all of that harmless without keeping a record of uuids.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import CONF_CLOUDHOOK_URL, CONF_SECRET, CONF_WEBHOOK_ID, DOMAIN
from .payload import SIGNATURE_HEADER, SensorUpdate, parse_payload, verify_signature

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


def signal_update(entry_id: str) -> str:
    """The dispatcher signal carrying sensor updates for one entry."""
    return f"{DOMAIN}_update_{entry_id}"


@dataclass
class LifeDashboardData:
    """What a loaded entry keeps in memory.

    latest is the ordering rule's memory. Entities seed it from their restored state
    when they are added, so a restart does not reopen the door to old batches.
    """

    latest: dict[str, SensorUpdate] = field(default_factory=dict)
    app_version: str | None = None

    def apply(self, update: SensorUpdate) -> bool:
        """Record an update, unless it describes a moment we are already past.

        Equal timestamps are accepted: a re-delivered batch carries the same value,
        and refusing it would only make the first delivery special.
        """
        current = self.latest.get(update.key)
        if current is not None and update.measured_at < current.measured_at:
            return False
        self.latest[update.key] = update
        return True


type LifeDashboardConfigEntry = ConfigEntry[LifeDashboardData]


async def async_setup_entry(hass: HomeAssistant, entry: LifeDashboardConfigEntry) -> bool:
    """Set up Life Dashboard from a config entry."""
    entry.runtime_data = LifeDashboardData()

    webhook.async_register(
        hass,
        DOMAIN,
        entry.title,
        entry.data[CONF_WEBHOOK_ID],
        _make_handler(entry),
        allowed_methods=["POST"],
    )
    # Registering the same id twice raises, so a reload has to unregister first.
    entry.async_on_unload(lambda: webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID]))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LifeDashboardConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the cloudhook when the entry goes, so no public URL stays alive."""
    if not entry.data.get(CONF_CLOUDHOOK_URL) or "cloud" not in hass.config.components:
        return
    with contextlib.suppress(ImportError):
        from homeassistant.components import cloud

        with contextlib.suppress(cloud.CloudNotAvailable, ValueError):
            await cloud.async_delete_cloudhook(hass, entry.data[CONF_WEBHOOK_ID])


def _make_handler(entry: LifeDashboardConfigEntry):
    """Build the webhook handler for one entry.

    Home Assistant turns an exception inside a handler into a silent HTTP 200, which
    the app would read as a successful delivery and never retry. So everything is
    caught here and answered with a status the app understands: 401 and 400 are
    permanent errors it logs without retrying, 200 means accepted.
    """

    async def handle(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        body = await request.read()

        if not verify_signature(
            entry.data[CONF_SECRET], body, request.headers.get(SIGNATURE_HEADER)
        ):
            _LOGGER.warning(
                "Refused a payload for %s: the signature is missing or does not match. "
                "Check that the secret in the app is the one this integration shows",
                entry.title,
            )
            return web.Response(status=401)

        try:
            data = json.loads(body)
        except ValueError:
            _LOGGER.warning("Refused a payload for %s: the body is not JSON", entry.title)
            return web.Response(status=400)

        if not isinstance(data, dict):
            _LOGGER.warning("Refused a payload for %s: the body is not a JSON object", entry.title)
            return web.Response(status=400)

        try:
            updates = parse_payload(data, tz=dt_util.get_default_time_zone())
        except Exception:
            # A parser bug must not masquerade as a successful delivery.
            _LOGGER.exception("Could not read a payload for %s", entry.title)
            return web.Response(status=400)

        runtime = entry.runtime_data
        accepted = [update for update in updates if runtime.apply(update)]

        if (version := data.get("app_version")) and version != runtime.app_version:
            runtime.app_version = version
            _update_sw_version(hass, entry, version)

        for update in accepted:
            async_dispatcher_send(hass, signal_update(entry.entry_id), update)

        _LOGGER.debug("Took %s of %s updates for %s", len(accepted), len(updates), entry.title)
        return web.Response(status=200)

    return handle


def _update_sw_version(hass: HomeAssistant, entry: LifeDashboardConfigEntry, version: str) -> None:
    """Keep the device's firmware field on the app version the phone reports.

    Looked up through the entry rather than by identifier: identifiers are no longer
    unique across entries, and this entry owns exactly one device anyway. Before the
    first entity exists there is no device yet, and none is created here, because the
    entities built from this very payload read the version out of runtime_data.
    """
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        registry.async_update_device(device.id, sw_version=version)
