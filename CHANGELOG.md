# Changelog

All notable changes to this integration are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-15

First release. Installable from HACS as a custom repository.

### Added

- Pairing through a config flow: name the phone, pick internal, external or Home
  Assistant Cloud, and get a webhook URL and an HMAC secret to paste into the app.
  Reconfigure shows them again, changes the address, or rotates the secret.
- Signature verification on every request. HMAC-SHA256 over the raw body, the same
  scheme the Android and iOS apps already use, so an unsigned or mis-signed payload
  is refused with 401 and the app reports it as a permanent error rather than
  retrying.
- 30 sensors, created as the data arrives so you only get the types you sync: four
  day totals resetting at local midnight, 21 latest readings, three screen time
  sensors, and two diagnostic timestamps. All under one device per phone, with
  device and state classes set so they land in long-term statistics.
- Values survive a restart, and the moment each value describes is restored with
  it, so a backfill arriving right after startup cannot overwrite a newer reading.
- iPhone support through the same flow. The iOS app sends no day totals and has no
  screen time API, so those sensors stay absent; everything else is identical.

### Notes

- Needs Home Assistant 2026.3 or newer.
- The app also publishes to MQTT with Discovery, using the same sensor names. Pick
  one of the two, or you get two devices holding the same numbers.

[0.1.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.1.0
