"""Diagnostics carry what a bug report needs and none of the health data."""

import json

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.life_dashboard.const import (
    CONF_BASE_URL,
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    URL_CHOICE_URL,
)
from custom_components.life_dashboard.payload import SIGNATURE_HEADER, signature_for
from custom_components.life_dashboard.writeback_queue import epoch_ms

WEBHOOK_ID = "a" * 64
SECRET = "b" * 64


async def test_diagnostics(hass: HomeAssistant, hass_client, hass_client_no_auth) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Owen's Pixel",
        unique_id=WEBHOOK_ID,
        data={
            CONF_WEBHOOK_ID: WEBHOOK_ID,
            CONF_SECRET: SECRET,
            CONF_URL_CHOICE: URL_CHOICE_URL,
            CONF_BASE_URL: "http://homeassistant.local:8123",
        },
        options={"writeback": {"weight": {"entity": "sensor.scale_weight"}}},
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.scale_weight", "80.0", {"unit_of_measurement": "kg"})
    # Before any client exists: the diagnostics views cannot register on a frozen router.
    assert await async_setup_component(hass, "diagnostics", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # A reading waits for the phone: 81.35 kg, measured now.
    hass.states.async_set("sensor.scale_weight", "81.35", {"unit_of_measurement": "kg"})
    await hass.async_block_till_done()
    measured = hass.states.get("sensor.scale_weight").last_changed

    payload = {
        "timestamp": "2026-09-16T10:00:00Z",
        "source": "health_connect",
        "app_version": "1.17.1",
        "heart_rate": [{"bpm": 61, "time": "2026-09-16T09:50:00Z"}],
        "daily_totals": [{"date": "2026-09-16", "steps": 1234}],
    }
    body = json.dumps(payload).encode()
    client = await hass_client_no_auth()
    response = await client.post(
        f"/api/webhook/{WEBHOOK_ID}",
        data=body,
        headers={SIGNATURE_HEADER: signature_for(SECRET, body)},
    )
    assert response.status == 200
    await hass.async_block_till_done()

    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert result["entry"][CONF_SECRET] == "**REDACTED**"
    assert result["entry"][CONF_WEBHOOK_ID] == "**REDACTED**"
    assert result["entry"][CONF_BASE_URL] == "http://homeassistant.local:8123"
    assert result["app_version"] == "1.17.1"
    # Which sensors, and when, but never the value.
    assert result["sensors"]["heart_rate"] == {"measured_at": "2026-09-16T09:50:00+00:00"}
    assert "61" not in json.dumps(result)
    assert result["history"]["days"]["steps"] == {
        "count": 1,
        "first": "2026-09-16",
        "last": "2026-09-16",
    }
    assert "1234" not in json.dumps(result["history"])
    # Which entities go to the phone and how many readings wait, never a value or
    # the moment one was measured.
    assert result["writeback"]["configured"] == {
        "weight": {"entities": {"entity": "sensor.scale_weight"}, "time_entity": None}
    }
    assert result["writeback"]["pending"] == {"weight": 1}
    assert result["writeback"]["delivered"] == {}
    assert result["writeback"]["acked_total"] == 0
    dump = json.dumps(result["writeback"])
    assert "81.35" not in dump
    assert measured.isoformat() not in dump
    assert str(epoch_ms(measured)) not in dump
