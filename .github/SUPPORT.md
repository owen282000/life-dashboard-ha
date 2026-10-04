# Getting help

GitHub shows this page when you open a new issue. Most questions have a faster answer than waiting for a reply.

## Setting it up

The [README](../README.md) covers installing through HACS. The [docs](../docs/README.md) cover pairing a phone by QR code or by hand, what every sensor and statistic holds, and sending measurements to the phone. The app side is documented in the [Android app's usage guide](https://github.com/owen282000/life-dashboard-companion-app/blob/main/docs/usage.md) and the [iOS app's README](https://github.com/owen282000/life-dashboard-companion-app-ios).

## Something is not working

The [troubleshooting page](../docs/troubleshooting.md) answers what comes up most: a 401 in the app's log (the secret differs), an address the phone cannot reach (put in another one with Reconfigure on the integration), plain HTTP being refused, sensors that are unknown after a power cut, and measurements that do not reach the phone. The Logs tab in the app shows every delivery attempt with Home Assistant's response.

## Still stuck, or want to ask something

[Discussions](https://github.com/owen282000/life-dashboard-ha/discussions) is the place for questions, setup help and ideas. The answer stays findable for whoever asks next.

## Reporting a bug

If something is genuinely broken, open an [issue](https://github.com/owen282000/life-dashboard-ha/issues/new/choose). The template asks for your Home Assistant version, the app and its version, the diagnostics file (three dots on the integration > Download diagnostics; no health data in it) and the relevant log lines, because those usually determine the cause.

Security problems go through [private advisories](https://github.com/owen282000/life-dashboard-ha/security/advisories/new) instead, never a public issue. See [SECURITY.md](../SECURITY.md).
