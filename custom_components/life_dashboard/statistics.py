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

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .history import (
    HistoryChange,
    HourBucket,
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
        # One fill of old history at a time: each reads the rows the one before wrote.
        self._old_lock = asyncio.Lock()

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
        if (change.old_days or change.old_hours) and self.available:
            self._entry.async_create_task(
                self._hass,
                self._async_import_old(change.old_days, change.old_hours, tz),
                f"{DOMAIN} old history",
            )

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

    async def _async_import_old(
        self,
        old_days: dict[str, dict[str, float]],
        old_hours: dict[str, dict[str, HourBucket]],
        tz: tzinfo,
    ) -> None:
        """Write the days and hours from before what the ledger keeps (P2-28).

        The ledger no longer holds those, so it cannot tell a day it counted from one it
        never saw. The recorder can: a day with a row there was counted. A day without one
        is new, and every later row's running sum moves up by its amount, which the
        recorder does itself (adjust_statistics) before the new rows are added. The amount
        joins what the ledger carries, so the rows it writes later agree.

        What this cannot see: a second session on an old day that already has a row, or the
        rest of an hour whose row exists. Both are left out rather than counted twice.
        """
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.models import StatisticMeanType
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
            statistics_during_period,
        )

        recorder = get_instance(self._hass)
        async with self._old_lock:
            # The rows earlier payloads queued must be in before they are read back.
            await recorder.async_block_till_done()
            added = 0
            for key, amounts in old_days.items():
                sid = statistic_id(self._entry, key)
                found = await recorder.async_add_executor_job(
                    statistics_during_period,
                    self._hass,
                    datetime.fromtimestamp(0, UTC),
                    None,
                    {sid},
                    "hour",
                    None,
                    {"sum"},
                )
                existing = {row["start"]: row.get("sum") or 0.0 for row in found.get(sid, [])}
                new = {
                    start: amount
                    for iso, amount in amounts.items()
                    if (start := _day_start(date.fromisoformat(iso), tz)).timestamp()
                    not in existing
                }
                if not new:
                    continue

                unit = _unit(key)
                for start, amount in new.items():
                    if any(ts > start.timestamp() for ts in existing):
                        recorder.async_adjust_statistics(
                            sid, start + timedelta(hours=1), amount, unit or ""
                        )
                rows = []
                for start in sorted(new):
                    before = [ts for ts in existing if ts < start.timestamp()]
                    base = existing[max(before)] if before else 0.0
                    total = base + sum(a for s, a in new.items() if s <= start)
                    rows.append({"start": start, "state": new[start], "sum": total})
                async_add_external_statistics(
                    self._hass,
                    self._metadata(key, sum_kind=True, mean_type=StatisticMeanType.NONE),
                    rows,
                )
                # Nothing awaited since the adjustment was queued, so a payload cannot slip
                # in between and write a row that misses the amount.
                self.ledger.carried[key] = self.ledger.carried.get(key, 0.0) + sum(new.values())
                added += len(new)

            for key, buckets in old_hours.items():
                sid = statistic_id(self._entry, key)
                hours = {datetime.fromisoformat(iso): bucket for iso, bucket in buckets.items()}
                starts = sorted(hours)
                found = await recorder.async_add_executor_job(
                    statistics_during_period,
                    self._hass,
                    starts[0],
                    starts[-1] + timedelta(hours=1),
                    {sid},
                    "hour",
                    None,
                    {"mean"},
                )
                taken = {row["start"] for row in found.get(sid, [])}
                rows = [
                    {
                        "start": start,
                        "mean": hours[start].mean,
                        "min": hours[start].minimum,
                        "max": hours[start].maximum,
                    }
                    for start in starts
                    if start.timestamp() not in taken
                ]
                if rows:
                    async_add_external_statistics(
                        self._hass,
                        self._metadata(key, sum_kind=False, mean_type=StatisticMeanType.ARITHMETIC),
                        rows,
                    )
                    added += len(rows)

            if added:
                _LOGGER.info(
                    "Added %d days and hours from before the ledger to the history of %s",
                    added,
                    self._entry.title,
                )
                self._store.async_delay_save(self.ledger.to_dict, SAVE_DELAY_SECONDS)

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


def _day_start(day: date, tz: tzinfo) -> datetime:
    """Where a day's row starts: local midnight on the hour, as day_rows writes it."""
    return datetime.combine(day, time.min, tzinfo=tz).replace(minute=0, second=0, microsecond=0)
