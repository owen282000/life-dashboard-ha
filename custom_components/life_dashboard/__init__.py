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

import voluptuous as vol
from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import CONF_CLOUDHOOK_URL, CONF_SECRET, CONF_WEBHOOK_ID, DOMAIN
from .payload import (
    SIGNATURE_HEADER,
    SensorUpdate,
    parse_payload,
    response_signature_for,
    verify_signature,
    writeback_request,
)
from .statistics import HistoryWriter
from .writeback import BACKFILL_DEFAULT_DAYS, BACKFILL_MAX_DAYS, WritebackManager
from .writeback_queue import WRITEBACK_TYPES

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_QUEUE_HISTORY = "queue_history"
ATTR_CONFIG_ENTRY = "config_entry"
ATTR_DAYS = "days"
ATTR_TYPES = "types"
QUEUE_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
        vol.Optional(ATTR_DAYS, default=BACKFILL_DEFAULT_DAYS): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=BACKFILL_MAX_DAYS)
        ),
        vol.Optional(ATTR_TYPES): vol.All(cv.ensure_list, [vol.In(WRITEBACK_TYPES)]),
    }
)


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
    #: This integration's own version, announced to the phone in every answer.
    version: str = "0"
    #: The long-term statistics writer; None only before setup finished.
    history: HistoryWriter | None = None
    #: The queue of measurements for the phone; None only before setup finished.
    writeback: WritebackManager | None = None

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


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the service; it is one per integration, not one per phone."""

    async def _queue_history(call: ServiceCall) -> None:
        entry = _entry_for_service(hass, call.data.get(ATTR_CONFIG_ENTRY))
        await entry.runtime_data.writeback.async_queue_history(
            days=call.data[ATTR_DAYS], types=call.data.get(ATTR_TYPES)
        )

    hass.services.async_register(
        DOMAIN, SERVICE_QUEUE_HISTORY, _queue_history, schema=QUEUE_HISTORY_SCHEMA
    )
    return True


def _entry_for_service(hass: HomeAssistant, entry_id: str | None) -> LifeDashboardConfigEntry:
    """The phone a service call means: the one named, or the only one there is."""
    if entry_id is not None:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="phone_not_loaded"
            )
        return entry
    loaded = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if len(loaded) != 1:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="choose_a_phone")
    return loaded[0]


async def async_setup_entry(hass: HomeAssistant, entry: LifeDashboardConfigEntry) -> bool:
    """Set up Life Dashboard from a config entry."""
    entry.runtime_data = LifeDashboardData()
    integration = await async_get_integration(hass, DOMAIN)
    entry.runtime_data.version = str(integration.version)
    history = HistoryWriter(hass, entry)
    await history.async_load()
    entry.runtime_data.history = history
    writeback = WritebackManager(hass, entry)
    await writeback.async_load()
    entry.runtime_data.writeback = writeback
    writeback.async_start()

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
    if (history := entry.runtime_data.history) is not None:
        await history.async_flush()
    if (writeback := entry.runtime_data.writeback) is not None:
        await writeback.async_flush()
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

    An accepted POST is answered with a signed JSON body: what this integration is,
    and, for a phone that asked, the measurements waiting for it (writeback.py). The
    refusals carry no body at all.
    """

    async def handle(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        body = await request.read()
        signature = request.headers.get(SIGNATURE_HEADER)

        if not verify_signature(entry.data[CONF_SECRET], body, signature):
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

        runtime = entry.runtime_data
        # One try for everything after the signature: a bug anywhere in here must be
        # a 400, not the silent 200 the app would take as delivered.
        try:
            updates = parse_payload(data, tz=dt_util.get_default_time_zone())
            accepted = [update for update in updates if runtime.apply(update)]

            if (version := data.get("app_version")) and version != runtime.app_version:
                runtime.app_version = version
                _update_sw_version(hass, entry, version)

            for update in accepted:
                async_dispatcher_send(hass, signal_update(entry.entry_id), update)

            # History goes to long-term statistics, where a backfill can land on the
            # days it came from.
            if runtime.history is not None:
                runtime.history.async_apply(data)

            # What the phone confirmed and refused, then what waits for it. Same
            # try: a bug here would otherwise cost the app an ack round as a 200.
            answer = runtime.writeback.async_respond(
                writeback_request(data), in_reply_to=signature or "", now=dt_util.utcnow()
            )
        except Exception:
            _LOGGER.exception("Could not read a payload for %s", entry.title)
            return web.Response(status=400)

        _LOGGER.debug("Took %s of %s updates for %s", len(accepted), len(updates), entry.title)
        return _signed(entry.data[CONF_SECRET], answer)

    return handle


def _signed(secret: str, answer: dict) -> web.Response:
    """A 200 with the answer as its body and the signature over those exact bytes."""
    raw = json.dumps(answer, separators=(",", ":")).encode("utf-8")
    return web.Response(
        status=200,
        body=raw,
        content_type="application/json",
        headers={SIGNATURE_HEADER: response_signature_for(secret, raw)},
    )


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
