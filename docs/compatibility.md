# What a version keeps stable

The integration has version numbers from 1.0.0 on, in the form `major.minor.patch`. Within one major version, so for all of 1.x, an update from the Marketplace or HACS keeps everything you built on it working: the phones keep sending without being paired again, your entities and their history stay, your statistics graphs carry on, and your automations, templates and dashboards keep reading what they read. A change to anything on this page is a new major version, with the steps for it in [CHANGELOG.md](../CHANGELOG.md).

A minor version adds things: a sensor, an attribute, a statistic, a setting with a default. A patch version fixes things.

## The entities

- One device per paired phone, and on it the sensors in [entities.md](entities.md), each with its key, unit and device class as listed there. A sensor keeps its unique ID (`<entry ID>_<key>`, and `<entry ID>_screen_time_app_<package>` for an app's screen time), so the entity ID you have, also one you renamed, stays.
- A state keeps its meaning: steps stay steps of the phone's day, a duration stays in hours, and a sensor that reads a day keeps that day in its `date` attribute.
- Attributes may be added; none are renamed, removed or given another unit. Sensors may be added; a sensor that would crowd the device page comes disabled.

## The long-term statistics

- One external statistic per type per phone, `life_dashboard:<entry ID in lowercase>_<key>`, with the keys and units in [entities.md](entities.md#history): day sums for steps, distance (km), energy (kcal), sleep, workouts, mindfulness and screen time (h), water (L) and each enabled app's screen time (h), and hourly mean, minimum and maximum for the measured types.
- A statistic keeps its ID, its unit and whether it is a sum or a mean. A day is a local day in Home Assistant's time zone. Its running sum carries on: an update, a backfill sent again or a correction never makes it jump.
- Removing a phone leaves its statistics in the recorder, and a phone you add later can take them over ([pairing.md](pairing.md#taking-over-the-history-of-a-removed-phone)).
- Statistics may be added.

## The payloads it takes

- `POST /api/webhook/<webhook ID>`, or the Home Assistant Cloud address made for it, with `X-Signature: sha256=<hex HMAC-SHA256 of the body>` keyed with the phone's secret. A 401 when the signature doesn't match, a 400 for a body that isn't a JSON object or can't be read, and a 200 once it is taken.
- The JSON payloads of Life Dashboard Companion for Android from 1.4.0 and for iOS from 1.4.0, as described in the Android app's [payload reference](https://github.com/owen282000/life-dashboard-companion-app/blob/main/docs/webhook.md) and the [iOS payload page](https://github.com/owen282000/life-dashboard-companion-app-ios/blob/main/docs/webhook.md). Later app versions that add fields or types keep working.
- The rules for what counts: a record that arrives twice counts once, a day's totals replace the earlier figure for that day, and a payload's own days land on those days, also when they are older than a year.
- The answer to a signed payload: a JSON object with `life_dashboard` (`version` and the write-back protocol `writeback`: 1) and `writeback` (`in_reply_to`, `issued_at`, `configured`, and `pending` and `more` when the phone asked for readings), with an `X-Signature` header of `sha256=<hex HMAC-SHA256 of the answer>`, keyed with HMAC-SHA256(secret, `life-dashboard-response-v1`). The apps' pairing test checks it. Fields may be added.

## Pairing

- The pairing code keeps the apps' link format (`v=1` with `url`, `secret` and `name`), described in [pairing.md](pairing.md#what-the-code-carries).
- Reconfigure keeps the webhook ID and the secret unless you ask for a new secret, so changing the address never means pairing again.

## Sending measurements to the phone

- Write-back protocol 1, as in [sending-to-the-phone.md](sending-to-the-phone.md): one entity per Health Connect type in the options, the readings queued for the phone, kept for 90 days and up to 500 per entity.
- The action `life_dashboard.queue_history` with its fields `config_entry`, `days` (1 to 90, 30 by default) and `types`.
- A mapping you made stays valid. Types may be added.

## Home Assistant versions

Home Assistant 2026.3 or newer. A minor version may ask for a newer Home Assistant when it needs one, as Home Assistant moves on; the changelog says so, and the Marketplace and HACS don't offer the update to an older one.
