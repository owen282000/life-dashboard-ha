"""Test the webhook handler: what it accepts, refuses and dispatches.

The status codes matter as much as the parsing. The app treats 401 and 400 as
permanent and logs them without retrying, while a 5xx or a timeout goes into its
outbox and comes back later. Answering wrongly here either loses data silently or
makes the phone retry forever.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime
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


async def test_an_iphone_payload_is_taken_and_answered(
    hass: HomeAssistant, hass_client_no_auth, loaded, updates
) -> None:
    """The bytes the iOS app sends, and what it gets back.

    Swift's JSONSerialization with sorted keys writes compact JSON, escapes every / as
    \\/ and leaves other characters raw. The signature covers exactly those bytes, so
    nothing here may re-serialize before checking it.
    """
    payload = {
        "timestamp": "2026-09-15T16:55:02Z",
        "app_version": "1.4.0",
        "source": "healthkit_ios",
        "daily_totals": [
            {"date": "2026-09-14", "steps": 8100},
            {"date": "2026-09-15", "steps": 4212, "distance_meters": 3150.25},
        ],
        "heart_rate": [
            {
                "bpm": 61,
                "time": "2026-09-15T12:00:00Z",
                "source": "Owen\u2019s Apple Watch / iPhone",
            }
        ],
        "blood_pressure": [{"systolic": 124.0, "time": "2026-09-15T10:00:00Z", "uuid": "bp-1"}],
        "sleep": [
            {
                "uuid": "0F1E2D3C-4B5A-5968-8776-A5B4C3D2E1F0",
                "session_end_time": "2026-09-15T05:30:00Z",
                "duration_seconds": 27000,
                "stages": [],
            }
        ],
    }
    body = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        .replace("/", "\\/")
        .encode()
    )
    assert b"\\/" in body
    request_signature = signature_for(SECRET, body)

    client = await hass_client_no_auth()
    response = await client.post(
        URL,
        data=body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: request_signature},
    )
    assert response.status == HTTPStatus.OK
    raw = await response.read()
    await hass.async_block_till_done()

    assert response.headers[SIGNATURE_HEADER] == _reference_signature(SECRET, raw)
    answer = json.loads(raw)
    assert answer["writeback"]["in_reply_to"] == request_signature
    assert "pending" not in answer["writeback"]

    by_key = {u.key: u for u in updates}
    assert by_key["steps_today"].value == 4212
    assert by_key["distance_today"].value == 3150.25
    assert by_key["blood_pressure_systolic"].value == 124.0
    assert "blood_pressure_diastolic" not in by_key
    assert by_key["sleep_duration"].value == 450
    assert by_key[KEY_LAST_HEALTH_SYNC].attributes["source"] == "healthkit_ios"
    assert loaded.runtime_data.app_version == "1.4.0"


# --- The answer ------------------------------------------------------------


def _reference_signature(secret: str, body: bytes) -> str:
    """The answer signature as the app computes it, written out independently."""
    key = hmac.new(secret.encode(), b"life-dashboard-response-v1", hashlib.sha256).digest()
    return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


def _manifest_version() -> str:
    with open("custom_components/life_dashboard/manifest.json") as handle:
        return json.load(handle)["version"]


async def test_an_old_app_gets_the_announcement(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """A request without a writeback block is answered with who we are, nothing more.

    The app before 1.20 never reads the body; a newer one learns from it that this
    integration can send measurements, and only then asks for them.
    """
    client = await hass_client_no_auth()
    response = await _post(
        client, _health(heart_rate=[{"bpm": 61, "time": "2026-09-15T12:00:00Z"}])
    )
    assert response.status == HTTPStatus.OK
    assert response.content_type == "application/json"

    answer = await response.json()
    assert answer["life_dashboard"] == {"version": _manifest_version(), "writeback": 1}
    assert answer["writeback"]["configured"] == []
    assert "pending" not in answer["writeback"]
    assert "more" not in answer["writeback"]


async def test_the_answer_is_signed_with_the_derived_key(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """Signed over the exact bytes sent, bound to the request it answers, and dated."""
    client = await hass_client_no_auth()
    body = json.dumps(_health()).encode()
    request_signature = signature_for(SECRET, body)
    before = datetime.now(UTC).replace(microsecond=0)
    response = await client.post(
        URL,
        data=body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: request_signature},
    )
    raw = await response.read()

    assert response.headers[SIGNATURE_HEADER] == _reference_signature(SECRET, raw)
    # Not the request key: the phone verifies with the derived one only.
    assert response.headers[SIGNATURE_HEADER] != signature_for(SECRET, raw)

    answer = json.loads(raw)
    assert answer["writeback"]["in_reply_to"] == request_signature
    issued = datetime.strptime(answer["writeback"]["issued_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert before <= issued.replace(tzinfo=UTC) <= datetime.now(UTC)


async def test_a_test_ping_announces_too(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    """The green Test in the app is also when it learns what it can receive."""
    client = await hass_client_no_auth()
    response = await _post(
        client, {"test": True, "timestamp": "2026-09-15T16:55:02Z", "source": "health_connect"}
    )
    assert response.status == HTTPStatus.OK
    answer = await response.json()
    assert answer["life_dashboard"]["writeback"] == 1
    assert answer["writeback"]["in_reply_to"].startswith("sha256=")


async def test_refusals_carry_no_body(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    client = await hass_client_no_auth()
    unsigned = await _post(client, _health(), secret=None)
    assert unsigned.status == HTTPStatus.UNAUTHORIZED
    assert await unsigned.read() == b""
    assert SIGNATURE_HEADER not in unsigned.headers
    broken = await _post(client, None, raw=b"not json")
    assert broken.status == HTTPStatus.BAD_REQUEST
    assert await broken.read() == b""


async def test_a_heartbeat_moves_no_sensor(
    hass: HomeAssistant, hass_client_no_auth, loaded, updates
) -> None:
    """The minimal body the app posts when it only wants to collect readings."""
    client = await hass_client_no_auth()
    response = await _post(
        client,
        {
            "timestamp": "2026-09-27T06:35:00Z",
            "app_version": "1.20.0",
            "source": "health_connect",
            "writeback": {"protocol": 1, "types": ["weight"], "history": False},
        },
    )
    assert response.status == HTTPStatus.OK
    await hass.async_block_till_done()
    assert updates == []
    assert (await response.json())["writeback"]["configured"] == []


async def test_a_history_failure_is_not_a_success(
    hass: HomeAssistant, hass_client_no_auth, loaded
) -> None:
    """Everything after the signature shares one try: a bug anywhere is a 400."""
    client = await hass_client_no_auth()
    with patch(
        "custom_components.life_dashboard.statistics.HistoryWriter.async_apply",
        side_effect=RuntimeError("boom"),
    ):
        response = await _post(client, _health())
    assert response.status == HTTPStatus.BAD_REQUEST


# --- The backfill ----------------------------------------------------------


async def test_a_backfill_leaves_the_last_sync_alone(
    hass: HomeAssistant, hass_client_no_auth, loaded, updates
) -> None:
    """Every chunk of a backfill used to move the last sync, a logbook line per POST."""
    client = await hass_client_no_auth()
    await _post(client, _health(heart_rate=[{"bpm": 61, "time": "2026-09-15T12:00:00Z"}]))
    await hass.async_block_till_done()
    entity_id = "sensor.owen_s_pixel_last_health_sync"
    before = hass.states.get(entity_id)
    assert before.state == "2026-09-15T16:55:02+00:00"
    updates.clear()

    for minute in range(3):
        response = await _post(
            client,
            _health(
                timestamp=f"2026-09-15T17:0{minute}:00Z",
                backfill=True,
                window_start="2026-03-01T00:00:00Z",
                window_end="2026-03-04T00:00:00Z",
                daily_totals=[{"date": "2026-03-02", "steps": 9000 + minute}],
                heart_rate=[{"bpm": 80, "time": "2026-03-02T09:00:00Z"}],
            ),
        )
        assert response.status == HTTPStatus.OK
    await hass.async_block_till_done()

    assert KEY_LAST_HEALTH_SYNC not in {u.key for u in updates}
    after = hass.states.get(entity_id)
    assert after.state == before.state
    assert after.last_updated == before.last_updated
    assert loaded.runtime_data.latest[KEY_LAST_HEALTH_SYNC].measured_at == datetime(
        2026, 9, 15, 16, 55, 2, tzinfo=UTC
    )

    # The chunks still reach the history, the last one winning for its day.
    assert loaded.runtime_data.history.ledger.days["steps"]["2026-03-02"] == 9002.0

    # The next regular sync moves it again.
    await _post(client, _health(timestamp="2026-09-15T17:10:00Z"))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "2026-09-15T17:10:00+00:00"


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


async def test_a_reload_keeps_its_place(hass: HomeAssistant, hass_client_no_auth, loaded) -> None:
    """A reload gets a fresh memory, and the entities put the values back.

    That is what stops an old batch arriving straight after a reload from
    overwriting a newer reading.
    """
    client = await hass_client_no_auth()
    await _post(client, _health(heart_rate=[{"bpm": 61, "time": "2026-09-15T12:00:00Z"}]))
    await hass.async_block_till_done()
    assert loaded.runtime_data.latest["heart_rate"].value == 61

    await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done()

    restored = loaded.runtime_data.latest["heart_rate"]
    assert restored.value == 61
    assert restored.measured_at.isoformat() == "2026-09-15T12:00:00+00:00"

    # And an older batch is still refused after the reload.
    await _post(
        client,
        _health(backfill=True, heart_rate=[{"bpm": 95, "time": "2026-09-01T10:00:00Z"}]),
    )
    await hass.async_block_till_done()
    assert loaded.runtime_data.latest["heart_rate"].value == 61


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
