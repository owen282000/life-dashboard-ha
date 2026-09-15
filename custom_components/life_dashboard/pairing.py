"""The pairing URL a QR code carries.

The QR is an ordinary https URL, because that is the one thing a phone camera knows
what to do with. Everything the phone needs is in the fragment, after the #, which
a browser never sends to any server (RFC 3986, section 3.5). On a phone with the app
installed, an Android App Link opens the app straight from the camera; without the
app, the page at PAIR_PAGE explains where to get it.

The format is shared with the app, which parses it, and with any receiver that wants
to be pairable the same way. Keep the two sides' test vectors identical.

This module imports nothing from Home Assistant.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote, urlencode

PAIR_PAGE: Final = "https://owen282000.github.io/life-dashboard-companion-app/pair"
PAIR_VERSION: Final = "1"
DEFAULT_NAME: Final = "Home Assistant"

# What this integration accepts. The app offers the user only these sections.
SOURCES: Final = ("health_connect", "screen_time")


def pairing_url(webhook_url: str, secret: str, *, name: str = DEFAULT_NAME) -> str:
    """Build the URL the QR carries.

    Every value is fully percent-encoded (nothing left "safe"), so the app can split
    the fragment on & and = before decoding and never has to guess whether a / or a
    , inside a value was meant literally.
    """
    fragment = urlencode(
        {
            "v": PAIR_VERSION,
            "url": webhook_url,
            "secret": secret,
            "name": name,
            "sources": ",".join(SOURCES),
        },
        quote_via=quote,
        safe="",
    )
    return f"{PAIR_PAGE}#{fragment}"


def qr_markup(pair_url: str) -> str:
    """The QR as the frontend renders it inside a dialog description.

    hassfest refuses HTML inside strings.json, so the element travels as a
    description placeholder instead, the way core's TOTP setup ships its <svg>. The
    frontend's markdown sanitizer whitelists ha-qr-code with exactly these
    attributes and draws it on a canvas in the theme's colours, so it stays
    scannable in dark mode. A frontend that does not know the element simply shows
    nothing there, and the URL and secret below it still work.
    """
    return f'<ha-qr-code data="{pair_url}" scale="5" error-correction-level="medium"></ha-qr-code>'
