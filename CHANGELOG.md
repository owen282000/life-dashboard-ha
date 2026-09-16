# Changelog

All notable changes to this integration are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-09-16

### Added

- Screen time in long-term statistics: minutes on the phone per day, next to the
  step and sleep statistics. Every screen time sync carries the last seven days, so a
  day that was still running when it was sent is corrected by the next sync, and the
  statistics graph card can show a year of phone use once there is a year of syncs.

### Changed

- The README is written as the listing: badges, real screenshots, and a plain
  statement of what works on Android and what iOS still needs.

## [0.3.0] - 2026-09-16

### Added

- History. Every payload now also feeds long-term statistics, so a backfill lands on
  the days it came from instead of on today. Day sums for steps, distance, active and
  total calories, sleep, exercise, mindfulness and hydration; hourly mean, minimum and
  maximum for heart rate, weight, blood pressure and the other measured values. They
  appear under **life_dashboard:** in the statistics graph card and the energy-style
  history views, and are stored per phone so a running sum continues across restarts.
  Step, distance and calorie history for a backfill needs app 1.17.0, which sends the
  day totals for every window; older apps only fill today's.

### Changed

- The sensors no longer carry a state class. The recorder would otherwise compile a
  second history from their states, and for a latest-value sensor that history is
  wrong: "61 bpm, held for an hour". Graphs of the sensors themselves in History are
  unchanged; use the statistics above for anything over days.

## [0.2.1] - 2026-09-16

### Fixed

- The pairing code was dense enough that a phone often could not read it off a screen. The `sources` field is gone from the link: it named both sections, which is what the app already assumes when a code does not say, so it cost four modules to state the default. The symbol is a version smaller, which is what a camera focusing through blur needs

## [0.2.0] - 2026-09-15

### Added

- A QR code in the pairing dialog, in Reconfigure, and after a secret rotation. Scan
  it with the phone's camera: with the app installed it opens the app, without it
  the page explains where to get the app. From version 1.16.0 the app also scans it
  from the Webhook card and from the wizard. The URL and secret stay below the code
  for pasting by hand.
- The code is an ordinary https link with everything sensitive after the `#`, which
  a browser never sends to a server. No QR library is needed: Home Assistant's own
  frontend draws it, in the theme's colours.

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

[0.4.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.4.0
[0.3.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.3.0
[0.2.1]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.2.1
[0.2.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.2.0
[0.1.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.1.0
