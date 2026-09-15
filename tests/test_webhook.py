"""Test the webhook handler: what it accepts, refuses and dispatches.

The status codes matter as much as the parsing. The app treats 401 and 400 as
permanent and logs them without retrying, while a 5xx or a timeout goes into its
outbox and comes back later. Answering wrongly here either loses data silently or
makes the phone retry forever.
"""

import json
from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.life_dashboard import signal_update
from custom_components.life_dashboard.const import (
    CONF_CLOUDHOOK_URL,
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    URL_CHOICE_CLOUD,
    URL_CHOICE_INTERNAL,
)
from custom_components.life_dashboard.payload import (
    KEY_LAST_HEALTH_SYNC,
    SIGNATURE_HEADER,
    signature_for,
)

WEBHOOK_ID = "a" * 64
SECRET = "b" * 64
URL = f"/api/webhook/{WEBHOOK_ID}"


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A config entry, not yet set up."""
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


@pytest.fixture
async def loaded(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    """A config entry that is set up, so its webhook is registered."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
def updates(hass: HomeAssistant, loaded: MockConfigEntry) -> list:
    """Everything the handler dispatches during a test."""
    seen: list = []

    @callback
    def _collect(update) -> None:
        seen.append(update)

    async_dispatcher_connect(hass, signal_update(loaded.entry_id), _collect)
    return seen


async def _post(client, payload, *, secret: str | None = SECRET, raw: bytes | None = None):
    """POST a payload the way the app does, signed unless secret is None."""
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if secret is not None:
        headers[SIGNATURE_HEADER] = signature_for(secret, body)
    return await client.post(URL, data=body, headers=headers)


def _health(**extra) -> dict:
    payload = {
        "timestamp": "2026-09-15T16:55:02Z",
        "app_version": "1.15.0",
        "source": "health_connect",
    }
    payload.update(extra)
    return payload


# --- What gets refused -----------------------------------------------------


async def test_unsigned_is_refused(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    """No header means the user never pasted the secret."""
    client = await hass_client_no_auth()
    response = await _post(client, _health(), secret=None)
    assert response.status == HTTPStatus.UNAUTHORIZED


async def test_wrong_secret_is_refused(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    """A mismatch is permanent for the app, which is what we want: fail loudly."""
    client = await hass_client_no_auth()
    response = await _post(client, _health(), secret="c" * 64)
    assert response.status == HTTPStatus.UNAUTHORIZED


async def test_tampered_body_is_refused(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    """The signature covers the body, so a changed byte invalidates it."""
    client = await hass_client_no_auth()
    body = json.dumps(_health()).encode()
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature_for(SECRET, body),
    }
    response = await client.post(URL, data=body + b" ", headers=headers)
    assert response.status == HTTPStatus.UNAUTHORIZED


async def test_not_json_is_refused(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    client = await hass_client_no_auth()
    response = await _post(client, None, raw=b"this is not json")
    assert response.status == HTTPStatus.BAD_REQUEST


async def test_json_array_is_refused(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    """Valid JSON, but not a payload."""
    client = await hass_client_no_auth()
    response = await _post(client, None, raw=b'["not", "an", "object"]')
    assert response.status == HTTPStatus.BAD_REQUEST


async def test_a_parser_failure_is_not_a_success(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """A bug in our own parsing must not read as a delivered payload."""
    client = await hass_client_no_auth()
    with patch(
        "custom_components.life_dashboard.parse_payload",
        side_effect=RuntimeError("boom"),
    ):
        response = await _post(client, _health())
    assert response.status == HTTPStatus.BAD_REQUEST


async def test_get_is_not_allowed(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    """The app only ever POSTs."""
    client = await hass_client_no_auth()
    response = await client.get(URL)
    assert response.status == HTTPStatus.METHOD_NOT_ALLOWED


# --- What gets accepted ----------------------------------------------------


async def test_test_ping_is_accepted(hass: HomeAssistant, hass_client_no_auth, updates) -> None:
    """A green Test in the app has to mean something in Home Assistant."""
    client = await hass_client_no_auth()
    response = await _post(
        client,
        {
            "test": True,
            "message": "Test ping from Life Dashboard Companion",
            "timestamp": "2026-09-15T16:55:02Z",
            "source": "health_connect",
        },
    )
    assert response.status == HTTPStatus.OK
    await hass.async_block_till_done()
    assert [u.key for u in updates] == [KEY_LAST_HEALTH_SYNC]


async def test_health_payload_dispatches(
    hass: HomeAssistant, hass_client_no_auth, loaded, updates
) -> None:
    client = await hass_client_no_auth()
    response = await _post(
        client,
        _health(
            daily_totals=[{"date": "2026-09-15", "steps": 4212}],
            heart_rate=[{"bpm": 61, "time": "2026-09-15T12:00:00Z"}],
        ),
    )
    assert response.status == HTTPStatus.OK
    await hass.async_block_till_done()

    keys = {u.key for u in updates}
    assert keys == {"steps_today", "heart_rate", KEY_LAST_HEALTH_SYNC}
    assert loaded.runtime_data.latest["heart_rate"].value == 61
    assert loaded.runtime_data.app_version == "1.15.0"


# --- The ordering rule -----------------------------------------------------


async def test_an_older_batch_is_ignored(
    hass: HomeAssistant, hass_client_no_auth, loaded, updates
) -> None:
    """Backfill re-sends history with its original timestamps."""
    client = await hass_client_no_auth()
    await _post(client, _health(heart_rate=[{"bpm": 61, "time": "2026-09-15T12:00:00Z"}]))
    await hass.async_block_till_done()
    updates.clear()

    response = await _post(
        client,
        _health(backfill=True, heart_rate=[{"bpm": 80, "time": "2026-09-01T09:00:00Z"}]),
    )
    assert response.status == HTTPStatus.OK
    await hass.async_block_till_done()

    # The old value is neither dispatched nor remembered.
    assert "heart_rate" not in {u.key for u in updates}
    assert loaded.runtime_data.latest["heart_rate"].value == 61


async def test_a_newer_batch_wins(
    hass: HomeAssistant, hass_client_no_auth, loaded, updates
) -> None:
    client = await hass_client_no_auth()
    await _post(client, _health(heart_rate=[{"bpm": 61, "time": "2026-09-15T12:00:00Z"}]))
    await hass.async_block_till_done()
    updates.clear()

    await _post(client, _health(heart_rate=[{"bpm": 58, "time": "2026-09-15T13:00:00Z"}]))
    await hass.async_block_till_done()

    assert loaded.runtime_data.latest["heart_rate"].value == 58
    assert "heart_rate" in {u.key for u in updates}


async def test_redelivery_is_harmless(
    hass: HomeAssistant, hass_client_no_auth, loaded, updates
) -> None:
    """The app re-POSTs a batch from its outbox after a failed delivery."""
    client = await hass_client_no_auth()
    payload = _health(heart_rate=[{"bpm": 61, "time": "2026-09-15T12:00:00Z"}])

    for _ in range(3):
        response = await _post(client, payload)
        assert response.status == HTTPStatus.OK
    await hass.async_block_till_done()

    assert loaded.runtime_data.latest["heart_rate"].value == 61
    # Equal timestamps are accepted, so the value is simply rewritten each time.
    assert len([u for u in updates if u.key == "heart_rate"]) == 3


async def test_a_sync_in_several_passes(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    """A backlog makes the app POST up to eight times per sync."""
    client = await hass_client_no_auth()
    for hour in range(8, 16):
        response = await _post(
            client,
            _health(heart_rate=[{"bpm": 60 + hour, "time": f"2026-09-15T{hour:02d}:00:00Z"}]),
        )
        assert response.status == HTTPStatus.OK
    await hass.async_block_till_done()

    assert loaded.runtime_data.latest["heart_rate"].value == 75


# --- Lifecycle -------------------------------------------------------------


async def test_unload_unregisters_the_webhook(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """After unloading, the URL must no longer reach us."""
    client = await hass_client_no_auth()
    assert (await _post(client, _health())).status == HTTPStatus.OK

    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()
    assert loaded.state is ConfigEntryState.NOT_LOADED

    # Home Assistant answers 200 for an unknown webhook, so as not to give away
    # whether one exists. Nothing of ours runs.
    assert (await _post(client, _health())).status == HTTPStatus.OK


async def test_reload_does_not_clash_with_itself(hass: HomeAssistant, loaded) -> None:
    """Registering a webhook id twice raises, so a reload has to unregister first."""
    await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done()
    assert loaded.state is ConfigEntryState.LOADED


async def test_reload_forgets_nothing_it_should_not(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """A reload starts with an empty ordering memory, and entities reseed it.

    Phase 5 restores the values; this only pins down that the memory is per load.
    """
    client = await hass_client_no_auth()
    await _post(client, _health(heart_rate=[{"bpm": 61, "time": "2026-09-15T12:00:00Z"}]))
    await hass.async_block_till_done()
    assert loaded.runtime_data.latest

    await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done()
    assert loaded.runtime_data.latest == {}


async def test_remove_entry_deletes_the_cloudhook(hass: HomeAssistant) -> None:
    """An entry that goes must not leave a public URL behind."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Owen's Pixel",
        unique_id=WEBHOOK_ID,
        data={
            CONF_WEBHOOK_ID: WEBHOOK_ID,
            CONF_SECRET: SECRET,
            CONF_URL_CHOICE: URL_CHOICE_CLOUD,
            CONF_CLOUDHOOK_URL: "https://hooks.nabu.casa/ABC123",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.config.components.add("cloud")
    delete = AsyncMock()
    with patch.dict(
        "sys.modules",
        {"homeassistant.components.cloud": _FakeCloud(delete)},
    ):
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    delete.assert_awaited_once_with(hass, WEBHOOK_ID)


class _FakeCloud:
    """Stand in for the cloud component, which needs binaries we do not have here."""

    class CloudNotAvailable(Exception):
        """As in the real component."""

    def __init__(self, delete: AsyncMock) -> None:
        self.async_delete_cloudhook = delete
