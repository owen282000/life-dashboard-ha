"""Test the pairing URL.

The app parses what this builds, so the known-good vector here is also in the app's
PairingLinkTest. Change one and the other must change with it.
"""

from urllib.parse import parse_qs, urlsplit

from custom_components.life_dashboard.pairing import PAIR_PAGE, pairing_url

WEBHOOK_URL = "http://192.168.10.138:8123/api/webhook/" + "a" * 64
SECRET = "b" * 64

# The exact string the app's test uses as well.
KNOWN_GOOD = (
    "https://owen282000.github.io/life-dashboard-companion-app/pair"
    "#v=1"
    "&url=http%3A%2F%2F192.168.10.138%3A8123%2Fapi%2Fwebhook%2F"
    + "a" * 64
    + "&secret="
    + "b" * 64
    + "&name=Home%20Assistant"
    "&sources=health_connect%2Cscreen_time"
)


def test_known_good_vector() -> None:
    assert pairing_url(WEBHOOK_URL, SECRET) == KNOWN_GOOD


def test_everything_is_in_the_fragment() -> None:
    """The secret must never be in a part of the URL a browser sends to a server."""
    parts = urlsplit(pairing_url(WEBHOOK_URL, SECRET))
    assert parts.scheme == "https"
    assert parts.query == ""
    assert SECRET not in parts.path
    assert SECRET in parts.fragment
    assert pairing_url(WEBHOOK_URL, SECRET).startswith(PAIR_PAGE + "#")


def test_round_trip() -> None:
    """parse_qs on the fragment gives back exactly what went in."""
    fragment = urlsplit(pairing_url(WEBHOOK_URL, SECRET, name="My House")).fragment
    values = parse_qs(fragment, strict_parsing=True)
    assert values == {
        "v": ["1"],
        "url": [WEBHOOK_URL],
        "secret": [SECRET],
        "name": ["My House"],
        "sources": ["health_connect,screen_time"],
    }


def test_awkward_characters_survive() -> None:
    """A future secret format may carry + and /, and a cloudhook URL has both."""
    url = "https://hooks.nabu.casa/gAAAAABm+abc/def=="
    secret = "x+y/z=&w"
    fragment = urlsplit(pairing_url(url, secret)).fragment
    values = parse_qs(fragment, strict_parsing=True)
    assert values["url"] == [url]
    assert values["secret"] == [secret]
    # Nothing is left unencoded that the app's splitter could mistake.
    raw = fragment.split("&secret=", 1)[1].split("&", 1)[0]
    assert "+" not in raw and "/" not in raw and "=" not in raw and "&" not in raw
