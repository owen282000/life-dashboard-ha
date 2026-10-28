"""Writing the ledger into Home Assistant's long-term statistics.

The thin layer between history.py, which decides what the rows say, and the recorder,
which stores them. One external statistic per data type per entry, fed from every
payload: a backfill fills the past, a normal sync extends the present.

External ids (`life_dashboard:<entry>_<key>`) rather than the sensors' own, because the
recorder compiles a sensor's statistics from its states itself, and two writers to one
running sum disagree at the seam. The sensors stay what they are, now; these are the
history.
"""

from __future__ import annotations

import logging
from datetime import date, tzinfo
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .history import (
    HistoryChange,
    Ledger,
    apply_payload,
    day_rows,
    earliest_day,
    hour_rows,
    keep_from,
    prune,
)
from .payload import SENSOR_SPECS

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# 1.2 added the workouts, 1.3 what a prune carries (kept_from and carried, written only
# once something was pruned), 1.4 the records of the workouts' details (activity, and
# source, laps and sets in a workout). Minor versions, because each change only adds keys:
# an older ledger reads as it is, and a downgrade to an earlier release still reads a
# newer one.
STORAGE_MINOR_VERSION = 4
SAVE_DELAY_SECONDS = 5

# Units and names for the day sums, which have no sensor to borrow from.
_DAY_UNITS: dict[str, str] = {
    "steps": "steps",
    "distance": "m",
    "active_calories": "kcal",
    "total_calories": "kcal",
    "sleep_minutes": "min",
    "exercise_minutes": "min",
    "mindfulness_minutes": "min",
    "hydration_total": "L",
    "screen_time": "min",
}


def statistic_id(entry: ConfigEntry, key: str) -> str:
    """The recorder accepts [0-9a-z_] only, and an entry id is an uppercase ULID."""
    return f"{DOMAIN}:{entry.entry_id.lower()}_{key}"


class _LedgerStore(Store[dict[str, Any]]):
    """The ledger's file, which brings a ledger of an earlier minor version up to date."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        if old_major_version == STORAGE_VERSION and old_minor_version < STORAGE_MINOR_VERSION:
            # Nothing of the days, sessions and hours changes; the workouts start empty,
            # because the sessions kept for the statistics hold no type and no start. A
            # ledger that was never pruned has nothing carried, which is what 1.3 says too.
            # The records for the details start empty as well: the workouts kept so far
            # read without details until their records come again.
            return {
                **old_data,
                "workouts": old_data.get("workouts") or {},
                "activity": old_data.get("activity") or {},
            }
        raise NotImplementedError


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return _LedgerStore(
        hass,
        STORAGE_VERSION,
        f"{DOMAIN}.{entry_id}.history",
        minor_version=STORAGE_MINOR_VERSION,
    )


async def async_remove_store(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the ledger with the entry. The statistics it wrote stay in the recorder."""
    await _store(hass, entry.entry_id).async_remove()


def _unit(key: str) -> str | None:
    if key in _DAY_UNITS:
        return _DAY_UNITS[key]
    spec = SENSOR_SPECS.get(key)
    return spec.unit if spec else None


def _name(entry: ConfigEntry, key: str) -> str:
    return f"{entry.title} {key.replace('_', ' ')}"


class HistoryWriter:
    """Keeps the ledger for one entry and imports what a payload changed."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._store = _store(hass, entry.entry_id)
        self.ledger = Ledger()
        # The day of the last prune, so the whole ledger is walked once a day at most.
        self._pruned_on: date | None = None

    async def async_load(self) -> None:
        self.ledger = Ledger.from_dict(await self._store.async_load())

    async def async_flush(self) -> None:
        await self._store.async_save(self.ledger.to_dict())

    @property
    def available(self) -> bool:
        return "recorder" in self._hass.config.components

    @callback
    def async_apply(self, data: dict[str, Any]) -> HistoryChange:
        """Fold a payload into the ledger, import the rows it changed, and prune it."""
        tz = dt_util.get_default_time_zone()
        change = apply_payload(self.ledger, data, tz=tz)
        if not change.is_empty:
            self._import(change, data, tz)

        pruned = self._prune(tz)

        if pruned or not change.is_empty or change.workouts:
            self._store.async_delay_save(self.ledger.to_dict, SAVE_DELAY_SECONDS)
        return change

    def _prune(self, tz: tzinfo) -> bool:
        """Let the oldest days go, once a day, and say whether anything went.

        After the rows are written, so a payload's own days are in the recorder before
        anything is let go. Those rows stay there; the ledger only forgets what it would
        need to write them again. Anything older that comes in later the same day is let go
        the next day, which is safe: it was never carried, so it counts once.
        """
        today = dt_util.now(tz).date()
        if today == self._pruned_on:
            return False
        self._pruned_on = today
        before = keep_from(self.ledger, today=today, tz=tz)
        return before is not None and prune(self.ledger, before=before, tz=tz)

    def _import(self, change: HistoryChange, data: dict[str, Any], tz: tzinfo) -> None:
        """Write the statistics rows a payload changed."""
        if not self.available:
            _LOGGER.debug(
                "Recorder not loaded; history for %s kept in the ledger only", self._entry.title
            )
            return

        # Imported inside the function: recorder is an after_dependency and may be absent.
        from homeassistant.components.recorder.models import StatisticMeanType
        from homeassistant.components.recorder.statistics import async_add_external_statistics

        for key, since in change.day_keys.items():
            rows = day_rows(self.ledger, key, since, tz=tz)
            if rows:
                async_add_external_statistics(
                    self._hass,
                    self._metadata(key, sum_kind=True, mean_type=StatisticMeanType.NONE),
                    rows,
                )

        for key, since in change.hour_keys.items():
            rows = hour_rows(self.ledger, key, since)
            if rows:
                async_add_external_statistics(
                    self._hass,
                    self._metadata(key, sum_kind=False, mean_type=StatisticMeanType.ARITHMETIC),
                    rows,
                )

        if data.get("backfill") is True and not data.get("daily_totals"):
            _LOGGER.warning(
                "Backfill window from %s carries no daily_totals; its step, distance and"
                " calorie history stays empty (the Android app sends them from 1.17.0,"
                " the iOS app from 1.4.0)",
                earliest_day(change),
            )

    def _metadata(self, key: str, *, sum_kind: bool, mean_type: Any) -> dict[str, Any]:
        return {
            "statistic_id": statistic_id(self._entry, key),
            "source": DOMAIN,
            "name": _name(self._entry, key),
            "unit_of_measurement": _unit(key),
            "unit_class": None,
            "has_sum": sum_kind,
            "mean_type": mean_type,
        }
