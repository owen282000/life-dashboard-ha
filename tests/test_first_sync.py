"""A phone that has not sent anything a day after it was added gets a repair.

Sensors are created on the first value, so the entity registry tells whether a phone ever
synced, also after a restart. The repair says where to get the app and how to show the
QR code again, and goes by itself with the first values.
"""

import json
from datetime import timedelta
from http import HTTPStatus

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.life_dashboard.const import (
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    TROUBLESHOOTING_URL,
    URL_CHOICE_INTERNAL,
)
from custom_components.life_dashboard.payload import SIGNATURE_HEADER, signature_for

WEBHOOK_ID = "a" * 64
SECRET = "b" * 64
URL = f"/api/webhook/{WEBHOOK_ID}"


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Owen's Pixel",
        unique_id=WEBHOOK_ID,
        data={
            CONF_WEBHOOK_ID: WEBHOOK_ID,
            CONF_SECRET: SECRET,
            CONF_URL_CHOICE: URL_CHOICE_INTERNAL,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, entry: MockConfigEntry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"no_data_{entry.entry_id}")


async def _post_steps(hass: HomeAssistant, client) -> None:
    body = json.dumps(
        {
            "timestamp": "2026-09-15T18:00:00Z",
            "app_version": "1.34.0",
            "source": "health_connect",
            "daily_totals": [{"date": "2026-09-15", "steps": 8421}],
        }
    ).encode()
    response = await client.post(
        URL,
        data=body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature_for(SECRET, body)},
    )
    assert response.status == HTTPStatus.OK
    await hass.async_block_till_done()


async def test_a_phone_that_sends_nothing_gets_a_repair_after_a_day(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    await _setup(hass, entry)
    assert _issue(hass, entry) is None

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=23))
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=25))
    await hass.async_block_till_done()
    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.translation_key == "no_data"
    assert issue.severity == ir.IssueSeverity.WARNING
    placeholders = issue.translation_placeholders
    assert placeholders["phone"] == "Owen's Pixel"
    assert placeholders["help"] == TROUBLESHOOTING_URL
    assert "utm_source%3Dha-repair" in placeholders["play"]
    assert "ct=ha-repair" in placeholders["app_store"]


async def test_the_first_values_take_the_repair_away(
    hass: HomeAssistant, hass_client_no_auth, freezer
) -> None:
    """A phone added two days ago that never synced: the repair is there at startup."""
    freezer.move_to(dt_util.utcnow() - timedelta(days=2))
    entry = _entry(hass)
    freezer.move_to(dt_util.utcnow() + timedelta(days=2))
    await _setup(hass, entry)
    assert _issue(hass, entry) is not None

    await _post_steps(hass, await hass_client_no_auth())
    assert _issue(hass, entry) is None


async def test_a_phone_with_sensors_never_gets_one(hass: HomeAssistant, freezer) -> None:
    """Sensors in the registry mean it synced before, also when nothing came lately."""
    freezer.move_to(dt_util.utcnow() - timedelta(days=30))
    entry = _entry(hass)
    freezer.move_to(dt_util.utcnow() + timedelta(days=30))
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{WEBHOOK_ID}_steps_today", config_entry=entry
    )
    await _setup(hass, entry)
    assert _issue(hass, entry) is None


async def test_a_sync_before_the_day_is_over_means_no_repair(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = _entry(hass)
    await _setup(hass, entry)
    await _post_steps(hass, await hass_client_no_auth())

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=25))
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None


async def test_removing_the_phone_removes_the_repair(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(dt_util.utcnow() - timedelta(days=2))
    entry = _entry(hass)
    freezer.move_to(dt_util.utcnow() + timedelta(days=2))
    await _setup(hass, entry)
    assert _issue(hass, entry) is not None

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None


@pytest.mark.parametrize("path", ["strings.json", "translations/en.json", "translations/de.json"])
def test_every_language_has_the_repair_and_the_store_links(path: str) -> None:
    with open(f"custom_components/life_dashboard/{path}") as handle:
        strings = json.load(handle)
    issue = strings["issues"]["no_data"]
    for placeholder in ("{phone}", "{play}", "{app_store}", "{help}"):
        assert placeholder in issue["description"]
    user = strings["config"]["step"]["user"]["description"]
    assert "{play}" in user and "{app_store}" in user


async def test_the_pairing_dialog_links_to_both_stores(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    placeholders = result["description_placeholders"]
    assert "utm_source%3Dha-config-flow" in placeholders["play"]
    assert "ct=ha-config-flow" in placeholders["app_store"]
