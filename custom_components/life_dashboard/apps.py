"""Which apps have a screen time sensor of their own, and what they last read.

A per-app sensor is created disabled, so most of them are never added to Home Assistant
and never restore a state of their own. What they need across a restart therefore lives
here instead, in one small store per entry: the label of every app that has a sensor,
so an entity rebuilt before the phone syncs keeps its name, and the newest day's table,
so a sensor the user has just enabled shows today's minutes as soon as the entry has
reloaded rather than at the next sync.

Two bounds keep the entity list from growing without end. An app needs a few minutes
over the days a payload carries before it gets a sensor, which leaves out what was only
opened once, and a phone gets a fixed number of app sensors at most. An app keeps its
sensor once it has one, also when it is no longer used: removing it would break the
automations and dashboards built on it.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .payload import KEY_SCREEN_TIME_APPS, SensorUpdate, parse_instant

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SAVE_DELAY_SECONDS = 5

#: Minutes over the days of one payload (up to seven) before an app gets a sensor.
MIN_WEEK_MINUTES: Final = 5
#: App sensors per phone at most, disabled ones included.
MAX_APP_SENSORS: Final = 50


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.apps")


async def async_remove_store(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the store with the entry: it names the apps on the phone."""
    await _store(hass, entry.entry_id).async_remove()


class AppRoster:
    """The apps of one phone that have a sensor, and the newest day's table."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._store = _store(hass, entry.entry_id)
        #: Package to label, for every app that has a sensor, in the order they came.
        self.labels: dict[str, str] = {}
        #: The newest table, as the update it arrived in; None before the first one.
        self.latest: SensorUpdate | None = None
        self._cap_logged = False

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        labels = stored.get("labels")
        if isinstance(labels, dict):
            self.labels = {
                package: label
                for package, label in labels.items()
                if isinstance(package, str) and isinstance(label, str)
            }
        table = stored.get("table")
        if isinstance(table, dict) and isinstance(table.get("apps"), dict):
            try:
                measured_at = parse_instant(table.get("measured_at"))
            except ValueError:
                return
            self.latest = SensorUpdate(
                key=KEY_SCREEN_TIME_APPS,
                value=str(table.get("date")),
                measured_at=measured_at,
                attributes={"date": table.get("date"), "apps": table["apps"]},
            )

    async def async_flush(self) -> None:
        await self._store.async_save(self._to_dict())

    def _to_dict(self) -> dict[str, Any]:
        table = None
        if self.latest is not None:
            table = {
                "date": self.latest.attributes.get("date"),
                "measured_at": self.latest.measured_at.isoformat(),
                "apps": self.latest.attributes.get("apps", {}),
            }
        return {"labels": self.labels, "table": table}

    @callback
    def async_take(self, update: SensorUpdate) -> list[str]:
        """Keep a newer table, and return the packages that get a sensor from it.

        The caller has already applied the ordering rule, so the table is not older than
        the one held. A label that changed is taken over for the next time the entity is
        built; the sensor shows the new one in its own attributes straight away.
        """
        apps: dict[str, dict[str, Any]] = update.attributes.get("apps", {})
        self.latest = update

        for package, row in apps.items():
            if package in self.labels and row.get("name"):
                self.labels[package] = row["name"]

        candidates = sorted(
            (
                (package, row)
                for package, row in apps.items()
                if package not in self.labels and row.get("week_minutes", 0) >= MIN_WEEK_MINUTES
            ),
            # The most used first, so a full roster keeps the apps that matter most.
            key=lambda item: (-item[1].get("week_minutes", 0), item[0]),
        )
        room = max(MAX_APP_SENSORS - len(self.labels), 0)
        added = [package for package, _ in candidates[:room]]
        for package in added:
            self.labels[package] = apps[package].get("name") or package

        if len(candidates) > room and not self._cap_logged:
            self._cap_logged = True
            _LOGGER.info(
                "%s has %s app screen time sensors, the most there can be; %s more apps get none",
                self._entry.title,
                MAX_APP_SENSORS,
                len(candidates) - room,
            )

        self._store.async_delay_save(self._to_dict, SAVE_DELAY_SECONDS)
        return added
