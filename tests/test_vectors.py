"""The protocol vectors in protocol/vectors, checked from both ends.

The vectors are the shared examples of the wire protocol: the Android and iOS apps run
the same files in their own tests (protocol/vectors/README.md says how). Here the
integration has to make exactly the bytes exchanges.json expects, and the phone's side
as tests/phone.py writes it out has to decide what answers.json expects. Every signature
in the files is also recomputed with hashlib alone, so a vector can't be wrong together
with the code it checks.
"""

import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.life_dashboard.const import (
    CONF_SECRET,
    CONF_URL_CHOICE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    URL_CHOICE_INTERNAL,
)
from custom_components.life_dashboard.payload import (
    RESPONSE_KEY_LABEL,
    SIGNATURE_HEADER,
    response_key,
    signature_for,
)

from .phone import answer_signature, check_answer

VECTORS = Path(__file__).parent.parent / "protocol" / "vectors"
EXCHANGES = json.loads((VECTORS / "exchanges.json").read_text(encoding="utf-8"))
ANSWERS = json.loads((VECTORS / "answers.json").read_text(encoding="utf-8"))

WEBHOOK_ID = "c" * 64
URL = f"/api/webhook/{WEBHOOK_ID}"


def _named(cases: list[dict]) -> list:
    return [pytest.param(case, id=case["name"]) for case in cases]


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _request_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_the_files_are_one_set() -> None:
    assert EXCHANGES["version"] == ANSWERS["version"] == 1
    for vectors in (EXCHANGES, ANSWERS):
        assert vectors["response_key_label"].encode() == RESPONSE_KEY_LABEL
        names = [case["name"] for case in vectors["cases"]]
        assert len(names) == len(set(names))


def test_the_answer_key() -> None:
    known = ANSWERS["response_key"]
    key = hmac.new(
        known["secret"].encode(), ANSWERS["response_key_label"].encode(), hashlib.sha256
    ).digest()
    assert key.hex() == known["hex"]
    assert response_key(known["secret"]) == key


@pytest.mark.parametrize("case", _named(EXCHANGES["cases"]))
def test_the_signatures_in_an_exchange(case: dict) -> None:
    """The request signature, and both signatures of the answer, from hashlib alone."""
    body = case["request"]["body"].encode("utf-8")
    expect = case["expect"]
    if expect["status"] == 401:
        assert case["request"]["signature"] != _request_signature(case["secret"], body)
        return
    assert case["request"]["signature"] == _request_signature(case["secret"], body)
    assert signature_for(case["secret"], body) == case["request"]["signature"]
    if expect["status"] != 200:
        return
    assert expect["answer_signature"] == answer_signature(case["secret"], expect["answer"].encode())
    assert expect["x_signature"] == answer_signature(case["secret"], expect["body"].encode())
    # The body is ASCII in both forms, so no hop can re-encode it into other bytes.
    assert expect["body"].isascii()
    if expect["signed_body"]:
        assert json.loads(expect["body"]) == {
            "life_dashboard_signed": 1,
            "answer": expect["answer"],
            "signature": expect["answer_signature"],
        }
    else:
        assert expect["body"] == expect["answer"]


@pytest.mark.parametrize("case", _named(EXCHANGES["cases"]))
async def test_the_integration_answers_an_exchange(
    hass: HomeAssistant, hass_client_no_auth, freezer, case: dict
) -> None:
    """The handler takes or refuses the request and answers with exactly these bytes."""
    freezer.move_to(_instant(EXCHANGES["integration"]["now"]))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Owen's Pixel",
        unique_id=WEBHOOK_ID,
        data={
            CONF_WEBHOOK_ID: WEBHOOK_ID,
            CONF_SECRET: case["secret"],
            CONF_URL_CHOICE: URL_CHOICE_INTERNAL,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # The answer names the integration's version; the vectors were written for this one.
    entry.runtime_data.version = EXCHANGES["integration"]["version"]

    headers = {"Content-Type": "application/json; charset=utf-8"}
    if case["request"]["signature"] is not None:
        headers[SIGNATURE_HEADER] = case["request"]["signature"]
    client = await hass_client_no_auth()
    response = await client.post(URL, data=case["request"]["body"].encode("utf-8"), headers=headers)
    sent = await response.read()
    await hass.async_block_till_done()

    expect = case["expect"]
    assert response.status == expect["status"]
    assert sent == expect["body"].encode("utf-8")
    if expect["status"] != 200:
        assert SIGNATURE_HEADER not in response.headers
        return
    assert response.content_type == expect["content_type"]
    assert response.headers[SIGNATURE_HEADER] == expect["x_signature"]

    # And the phone takes it, with the header and without.
    for kept in ({SIGNATURE_HEADER: response.headers[SIGNATURE_HEADER]}, {}):
        verdict = check_answer(
            case["secret"],
            request_signature=case["request"]["signature"],
            body=sent,
            headers=kept,
            now=_instant(EXCHANGES["integration"]["now"]),
        )
        assert verdict.accept is bool(kept or expect["signed_body"])
        if verdict.accept:
            assert verdict.text == expect["answer"]


@pytest.mark.parametrize("case", _named(ANSWERS["cases"]))
def test_the_phone_decides_on_an_answer(case: dict) -> None:
    response = case["response"]
    assert response["status"] == 200
    verdict = check_answer(
        case["secret"],
        request_signature=case["request_signature"],
        body=response["body"].encode("utf-8"),
        headers=response["headers"],
        now=_instant(case["now"]),
    )
    expect = case["expect"]
    assert verdict.accept is expect["accept"]
    if expect["accept"]:
        assert verdict.text == expect["answer"]
        assert verdict.answer == json.loads(expect["answer"])
    else:
        assert verdict.reason == expect["reason"]
        assert verdict.update_integration is expect["update_integration"]
        assert verdict.text is None
