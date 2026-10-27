# Changelog

All notable changes to this integration are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- The README puts the stores first: the Android app on Google Play, with its badge next to the
  App Store one, and the iPhone app on the App Store. The Android app stays free from GitHub, on
  F-Droid or through Obtainium.

## [0.11.0] - 2026-10-22

### Added

- A speed sensor in m/s, with hourly mean, minimum and maximum in the long-term statistics, from
  the `speed` samples that Android app 1.29.0 will send from Health Connect
  ([owen282000/life-dashboard-companion-app#180](https://github.com/owen282000/life-dashboard-companion-app/issues/180)).
  It works like heart rate, also when the app sends speed as 1, 5 or 15 minute averages. Until
  then a payload with speed was answered 200 and the speed left out.
  The sensor appears the first time a phone sends speed, so nothing changes before that app
  version.

### Changed

- The README says the iPhone app is on the App Store, with the App Store badge where HACS shows
  it, and has the same header and How it works section as the other three Life Dashboard parts.

## [0.10.4] - 2026-10-19

### Fixed

- A sensor's state no longer carries the tail of a 32-bit float. An app that stores a reading
  that way sends 126.2 kg as 126.199996948242, and the weight sensor showed exactly that
  ([owen282000/life-dashboard-companion-app#165](https://github.com/owen282000/life-dashboard-companion-app/issues/165)).
  A decimal value is now rounded to two decimals more than the sensor displays, grams for a
  weight, which keeps everything the phone measured. Whole numbers such as steps and heart
  rate stay as they are, and the long-term statistics are unchanged.

### Changed

- The example dashboard has a weight graph in its History section: the mean per day over a
  year, from the `weight` statistic.
- Troubleshooting explains why a sensor's history puts a whole backfill on today: a sensor
  holds only its newest value, and its history shows when each one arrived. The readings on
  their own dates are in the long-term statistics, with a **Statistics graph** card for weight
  as the example. The README says the same in one line.

## [0.10.3] - 2026-10-19

### Changed

- The README shows the four Life Dashboard parts again as cards, the Android app, the iPhone
  app, this integration and the stack, in a short **Part of Life Dashboard** section that also
  renders in HACS.

## [0.10.2] - 2026-10-19

### Changed

- The integration is in the default HACS list, so it installs by searching for **Life Dashboard**
  in HACS, without a custom repository.
- The README renders in HACS: images and links by absolute URL, no `<picture>` elements. It says
  up front that the Life Dashboard Companion app on the phone is needed, and that it is not the
  Home Assistant Companion app; which watches and apps work through Health Connect, Fitbit
  included; reaching Home Assistant from away, a VPN included; and what does and doesn't leave
  your Home Assistant. Half as long, with the Quick start before the details.
- **Add a phone** says the next step needs the Life Dashboard Companion app, that Android wants
  the IP address rather than `homeassistant.local`, and that a VPN to your home network keeps
  the home address working everywhere. In English and German.

## [0.10.1] - 2026-10-15

### Fixed

- The ledger behind a phone's statistics no longer grows without end. It keeps the 400 days
  before the newest one, more than a backfill of a year plus a week in the app's outbox, so
  a backfill sent again still replaces itself instead of counting twice. Older days stay in
  the statistics, and the ledger keeps their sum, so the days after them still add up to the
  right total; a figure that arrives for one of those older days is ignored rather than
  counted again. Diagnostics show from which day the ledger keeps, never what it carries.
  The file is now stored as version 1.3, which an earlier release still reads. After days
  were let go, though, an earlier release leaves their sum out of the totals it writes, so
  those fall back by what the days held, and once it has saved the ledger that sum is gone,
  also after upgrading again.
- **Options** and the repair for a missing write permission name the app's **Health** tab,
  which they called the Health Connect tab.
- The pairing dialog says **Sync Now**, as the apps do.

## [0.10.0] - 2026-10-14

### Added

- Workout sensors. **Last workout** holds the kind of the newest workout, such as running,
  walking, strength training or yoga, with its `start`, `end`, `duration_minutes`, the
  `type_code` the phone sent and the app that wrote it as `source`. **Exercise today** and
  **Exercise yesterday** hold the minutes of the day, with every session of that day as the
  `sessions` attribute: its kind, start, end and minutes. Time covered by more than one session,
  such as the same run written by the phone and the watch, counts once, and a workout deleted
  on the phone, or replaced by an edit on an iPhone, no longer counts. The Android app sends the kind as
  a Health Connect exercise type number and the iOS app as a name; both end up as one of
  62 kinds with a name in English and in German, and a type this integration doesn't know
  reads as other, with the number still in `type_code`. A workout that crosses midnight
  counts for the part on each day, so a run from 23:30 to 00:30 is half an hour on both.
  The day sensors move on at midnight without waiting for a sync. Like the other sensors,
  they come with the first workout the phone sends. Contributed by @ak111pro in #13, a first contribution to the project.
- **Shortest workout (minutes)** under **Configure**, 0 by default. Workouts that are
  shorter are left out of the three sensors, which is how you keep the short walks the
  phone notices by itself out of them. Changing it takes effect at once for the two day
  sensors, because the sessions are kept whole; Last workout follows it from the next workout
  on. The `exercise_minutes` statistic doesn't follow it and still
  counts every session, on the day it ended.
- The ledger keeps the sessions of yesterday and today (their kind, start, end and length,
  under their record ID, so a repeat or an edit replaces itself) for the day sensors. It is
  stored as version 1.2: a ledger written by 0.9.0 is taken over with everything in it and
  starts with no sessions, and a release before this one still reads the file.
- Diagnostics count the kept workouts, never what they were.

### Changed

- **Configure** is titled Options now, as it holds more than the measurements for the phone.

## [0.9.0] - 2026-10-04

### Added

- A screen time sensor per app, with the app's minutes today, named after the app (such
  as YouTube screen time). They are created disabled, so the device page stays as it was:
  enable the apps you want a sensor for. A new app gets its sensor at the next sync,
  without a reload, and an enabled sensor shows today's minutes as soon as Home Assistant
  has reloaded. The sensor belongs to the app's package, so a renamed app keeps its
  sensor. An app that is missing from a newer day, unused or left out with the app filter
  since, reads 0 for that day instead of keeping the day before, and an app the filter
  leaves out never gets one. An app needs 5 minutes over the days a sync carries before it
  gets a sensor, and a phone gets 50 app sensors at most, the most used apps first. A
  sensor you delete stays deleted, also after a restart, and frees its slot.

### Fixed

- Removing a phone now deletes what was stored for it: the ledger behind its statistics
  and the queue of measurements for the phone went nowhere before. The long-term
  statistics themselves stay in the recorder. Phones removed earlier left theirs in
  `config/.storage` as `life_dashboard.<id>.history` and `life_dashboard.<id>.writeback`,
  which can be deleted by hand.

## [0.8.0] - 2026-10-04

### Added

- The screen time sensors follow the app's new app filter (Android app 1.23.0, issue #63 of the
  app): with some apps left out, or only a few sent, Screen Time Today, Screen Time Yesterday
  and the screen time statistics count the apps that are sent (`filtered_screen_time_minutes`),
  and the day sensors carry the real total of every app as the attribute `all_apps_minutes`.
  The top app is the most used one that is sent, and "none" when the filter leaves no app of
  today, so a sensor never keeps naming an app that was just left out. Without a filter
  nothing changes.

## [0.7.2] - 2026-09-30

### Fixed

- A backfill no longer moves **Last health sync**. The app sends a backfill as one POST
  per chunk, a second or two apart, and every chunk set the timestamp again, so a year
  of backfill filled the logbook and the recorder with a line per chunk. The chunks
  still fill the day totals and the long-term statistics as before; the next regular
  sync moves the timestamp.

## [0.7.1] - 2026-09-30

### Added

- The iOS app through this integration
  ([#1](https://github.com/owen282000/life-dashboard-ha/issues/1)), as 0.1.0 promised:
  health sensors, day totals from HealthKit's statistics, history in long-term statistics
  and pairing by QR code, with the iOS app 1.4.0 or newer. It signs the way the Android
  app does, so nothing changed in what this integration accepts. An iPhone has no screen
  time to send and does not receive measurements from Home Assistant.

### Changed

- The pairing dialog's paste-by-hand text, the new-secret hint and the Configure text no
  longer assume the Android app; the warning for a backfill without day totals names the
  version of both apps.

## [0.7.0] - 2026-09-26

### Added

- Measurements from Home Assistant to the phone
  ([app #62](https://github.com/owen282000/life-dashboard-companion-app/issues/62)).
  **Configure** on the integration
  maps an entity per Health Connect type (weight, height, body fat, lean body mass,
  bone mass, body water mass, blood pressure) to a phone; the app writes them to
  Health Connect once **Receive** is on there, per type, with the app 1.20 or newer.
  Every accepted payload is now answered with a signed JSON body that carries the
  readings waiting for that phone; the phone confirms them in its next request.
  Readings are identified by entity and measured moment, so a lost delivery or a
  reinstalled app costs nothing and a corrected value replaces the earlier one.
- A **Send history to phone** button on the device and a `life_dashboard.queue_history`
  service, which read the recorder's past states of the mapped entities and queue
  them the same way.
- A repair on the entry when the phone refuses a type for a missing permission or
  because the app is too old, gone as soon as that type is written again.
- Diagnostics list the mapped entities and how many readings wait or were written,
  never a value.

### Changed

- Records the app wrote to Health Connect on Home Assistant's behalf come back at
  the next sync with the app's own package as source. They no longer feed a sensor or
  the history: the weight that went out through Home Assistant is already in Home
  Assistant.
- The answer to a payload is a JSON body with `Content-Type: application/json`,
  signed in `X-Signature` with a key derived from the secret. Apps before 1.20 never
  read it and see no difference.

## [0.6.0] - 2026-09-16

### Added

- Diagnostics: *Download diagnostics* on the integration gives a bug report the
  sensors that exist, when each last updated and how much history the ledger holds,
  with the secret and the webhook id redacted and no health data in it. The bug
  report template asks for it.
- `examples/dashboard.yaml`, a view for one phone to start from.

### Changed

- README: an entity table with units and attributes, how data arrives, a debug logging
  snippet, and a correction: the raw records (workouts, meals, cycle tracking) are not
  fired as events by this integration; the plain webhook route or MQTT carries them.

## [0.5.3] - 2026-09-16

### Changed

- The pairing dialog puts the code on the left in its own frame and the three steps
  beside it, with the pasting fallback folded away underneath. Everything fits without
  scrolling on a laptop; on a phone the steps move below the code.

## [0.5.2] - 2026-09-16

### Changed

- "Or paste by hand" in the pairing dialog is folded away until you open it, so the
  dialog is the code and three steps, and the URL and the secret are there for
  whoever cannot scan.

## [0.5.1] - 2026-09-16

### Changed

- The pairing dialog reads as three steps: scan, check and pair, sync. Pasting by hand
  moved under the code with the URL and the secret in their own blocks, and the note
  about an unreachable address is a footnote instead of a paragraph.

## [0.5.0] - 2026-09-16

### Changed

- The pairing dialog no longer asks to choose between the internal and the external
  URL. It shows the address your browser is using at that moment in a text field, and
  you change it when the phone should use another one. The choice depended on
  Settings > System > Network being filled in, which it often is not, and produced "No
  external URL is set" on a Home Assistant that was plainly reachable at the address
  in the browser's own address bar. Home Assistant Cloud stays a checkbox, shown with
  a subscription. Existing pairings keep working; Reconfigure shows the address they
  resolved to and lets you keep or change it.

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

[0.11.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.11.0
[0.10.4]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.10.4
[0.10.3]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.10.3
[0.10.2]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.10.2
[0.10.1]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.10.1
[0.10.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.10.0
[0.9.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.9.0
[0.8.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.8.0
[0.7.2]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.7.2
[0.7.1]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.7.1
[0.7.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.7.0
[0.6.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.6.0
[0.5.3]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.5.3
[0.5.2]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.5.2
[0.5.1]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.5.1
[0.5.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.5.0
[0.4.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.4.0
[0.3.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.3.0
[0.2.1]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.2.1
[0.2.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.2.0
[0.1.0]: https://github.com/owen282000/life-dashboard-ha/releases/tag/0.1.0
