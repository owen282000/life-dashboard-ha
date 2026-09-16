# Getting help

GitHub shows this page when you open a new issue. Most questions have a faster answer than
waiting for a reply.

## Setting it up

The [README](../README.md) covers installing through HACS, pairing a phone by QR code or
by hand, and what every sensor and statistic holds. The app side is documented in the
[companion app's usage guide](https://github.com/owen282000/life-dashboard-companion-app/blob/main/docs/usage.md).

## Something is not working

The [troubleshooting section](../README.md#troubleshooting) answers what comes up most: a
401 in the app's log (the secret differs), a URL the phone cannot reach (set your addresses
under Settings > System > Network), plain HTTP being refused, and sensors that are unknown
after a power cut. The Logs tab in the app shows every delivery attempt with Home
Assistant's answer.

## Still stuck, or want to ask something

[Discussions](https://github.com/owen282000/life-dashboard-ha/discussions) is the place for
questions, setup help and ideas. The answer stays findable for whoever asks next.

## Reporting a bug

If something is genuinely broken, open an
[issue](https://github.com/owen282000/life-dashboard-ha/issues/new/choose). The template
asks for your Home Assistant version, the app and its version, the diagnostics file
(three dots on the integration > Download diagnostics; no health data in it) and the
relevant log lines, because those usually determine the cause.

Security problems go through
[private advisories](https://github.com/owen282000/life-dashboard-ha/security/advisories/new)
instead, never a public issue. See [SECURITY.md](../SECURITY.md).
