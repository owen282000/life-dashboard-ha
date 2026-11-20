"""The phone's side of an answer, written out independently of the integration.

What the apps do with a response to a POST, in the order docs/sending-to-the-phone.md
gives, using nothing but hashlib and json: none of the integration's own helpers, so a
mistake in those can't hide behind itself. The tests of the answer and the protocol
vectors in protocol/vectors both go through here.
"""

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

MAX_BYTES = 262144
MAX_READINGS = 200
MAX_AGE = timedelta(minutes=10)


@dataclass
class Verdict:
    """Whether the phone takes an answer, and why not."""

    accept: bool
    reason: str | None = None
    # The answer as the text that was signed, and parsed; only when accepted.
    text: str | None = None
    answer: dict | None = None
    # An unsigned plain answer whose announcement lacks signed_body: an integration
    # before 1.3.0 behind a route that drops the header. Good for a message, nothing else.
    update_integration: bool = False


def answer_signature(secret: str, body: bytes) -> str:
    key = hmac.new(secret.encode(), b"life-dashboard-response-v1", hashlib.sha256).digest()
    return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


def _matches(secret: str, body: bytes, signature: object) -> bool:
    return isinstance(signature, str) and hmac.compare_digest(
        answer_signature(secret, body), signature
    )


def _object(body: bytes) -> dict | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def check_answer(
    secret: str,
    *,
    request_signature: str,
    body: bytes,
    headers: Mapping[str, str],
    now: datetime,
) -> Verdict:
    """Decide on a 200 response. Stops at the first fault."""
    if len(body) > MAX_BYTES:
        return Verdict(False, "too_large")

    outer = _object(body)
    if outer is not None and "life_dashboard_signed" in outer:
        # The signed body: the signature inside decides, over the answer's text as it
        # stands. The header, if it arrived at all, is not looked at.
        if outer["life_dashboard_signed"] != 1:
            return Verdict(False, "protocol")
        text = outer.get("answer")
        if not isinstance(text, str) or "signature" not in outer:
            return Verdict(False, "unsigned")
        if not _matches(secret, text.encode("utf-8"), outer["signature"]):
            return Verdict(False, "signature")
        answer = _object(text.encode("utf-8"))
    else:
        header = headers.get("X-Signature")
        if not header:
            announced = outer.get("life_dashboard") if outer is not None else None
            return Verdict(
                False,
                "unsigned",
                update_integration=isinstance(announced, dict) and "signed_body" not in announced,
            )
        if not _matches(secret, body, header):
            return Verdict(False, "signature")
        text, answer = body.decode("utf-8"), outer

    # Only now is anything in the answer believed.
    announced = answer.get("life_dashboard") if answer is not None else None
    if not isinstance(announced, dict) or announced.get("writeback") != 1:
        return Verdict(False, "protocol")
    block = answer.get("writeback")
    if not isinstance(block, dict) or block.get("in_reply_to") != request_signature:
        return Verdict(False, "in_reply_to")
    try:
        issued = datetime.strptime(block.get("issued_at"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return Verdict(False, "issued_at")
    if abs(now - issued) > MAX_AGE:
        return Verdict(False, "issued_at")
    if len(block.get("pending") or []) > MAX_READINGS:
        return Verdict(False, "too_many")
    return Verdict(True, text=text, answer=answer)
