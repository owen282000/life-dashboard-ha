"""Diagnostics for Life Dashboard: what a bug report needs, without the health data.

Settings > Devices & services > Life Dashboard > three dots > Download diagnostics.
The secret and the webhook id are redacted; sensor values are not included at all,
only which sensors exist and when each was last measured, and how much history the
ledger holds per statistic.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_CLOUDHOOK_URL, CONF_SECRET, CONF_WEBHOOK_ID

TO_REDACT = {CONF_SECRET, CONF_WEBHOOK_ID, CONF_CLOUDHOOK_URL}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    ledger = runtime.history.ledger if runtime.history else None

    def span(keys: dict[str, Any]) -> dict[str, Any]:
        ordered = sorted(keys)
        return (
            {"count": len(ordered), "first": ordered[0], "last": ordered[-1]}
            if ordered
            else {"count": 0}
        )

    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "app_version": runtime.app_version,
        "sensors": {
            key: {"measured_at": update.measured_at.isoformat()}
            for key, update in sorted(runtime.latest.items())
        },
        "history": {
            "recorder_available": bool(runtime.history and runtime.history.available),
            "days": {key: span(days) for key, days in sorted(ledger.days.items())}
            if ledger
            else {},
            "sessions": {key: len(records) for key, records in sorted(ledger.sessions.items())}
            if ledger
            else {},
            "hours": {key: span(hours) for key, hours in sorted(ledger.hours.items())}
            if ledger
            else {},
        },
    }
