# How it works

The phone pushes; nothing in Home Assistant polls. This page covers when data arrives, why a repeat never counts twice, and how the webhook is secured.

- [Syncing](#syncing)
- [Why nothing is double counted](#why-nothing-is-double-counted)
- [Security](#security)

## Syncing

The app reads Health Connect and screen time on Android, or Apple Health on an iPhone, on the schedule you set in it: an interval, or fixed times, which iOS treats as the earliest moment. It then posts a signed payload to this integration's webhook. Health and screen time are separate payloads with their own schedules.

Each value arrives with the moment it describes. The sensors take the newest, and the long-term statistics take every day and every hour, so a backfill of a year lands on the days it happened.

A Home Assistant restart changes nothing on the phone. The sensors come back with their last values, and the next sync fills in whatever happened in the meantime. A delivery that failed while Home Assistant was down waits in the app's outbox and goes out with the next sync.

With [measurements going to the phone](sending-to-the-phone.md), the response to a sync carries the readings that are waiting for it. When there's nothing to sync, the Android app sends a short request of its own, a heartbeat, to collect them. A heartbeat updates no sensor, not even the last-sync time.

## Why nothing is double counted

The app re-sends data on purpose. A delivery that failed goes into an outbox and comes back at the next sync, a large backlog is split over several requests, and a backfill re-sends history with its original timestamps. Health Connect also hands over a record again after the source app edits it.

So every value carries the moment it describes, and each kind of data has a rule that makes a repeat harmless:

- **Sensors** take an update only when it isn't older than what they already hold. An old batch arriving after a restart can't overwrite a newer reading, because each sensor restores the time of its last value.
- **Day totals** (steps, distance, calories) and **screen time** are kept per day, and a newer figure for a day replaces the old one. The day totals come from Health Connect's own deduplicated figures, never summed from raw records: a phone and a watch both record the same walk, and only Health Connect's figure counts it once.
- **Sessions** (sleep, exercise, mindfulness, drinks) are kept by their record ID. A repeat changes nothing, and an edited record replaces its own contribution, even when the edit moves it to another day.
- **Hourly readings** (heart rate, weight and the rest) are kept as a mean, minimum and maximum per hour. This is the one place a repeat can show. A batch sent again counts its readings once more: the minimum and maximum stay as they were, and so does the mean when the batch held the whole hour. A repeat of part of an hour pulls the mean toward that part.

The statistics are rebuilt from this ledger, kept in `.storage`, so re-delivery is harmless and a correction replaces the old figure. Records the app wrote to Health Connect on Home Assistant's behalf are skipped in all of it: they're already in Home Assistant.

## Security

Every payload is signed, and so is every response that carries readings back to the phone. There's no setting to turn signing off.

### Signed payloads

Every payload is signed with HMAC-SHA256 over the raw body, with a secret this integration generated when the phone was paired. The signature is checked in constant time before anything is parsed. A payload that fails gets a 401 and is logged without its contents. A body that isn't a JSON object gets a 400. The app doesn't retry either one right away. It keeps the payload in its outbox: after a 401 it sends it again once the secret is fixed (the iOS app keeps it for up to a week), and after a 400 it tries again for a week and then drops it.

Anything that goes wrong while reading a signed payload is also answered with a 400, never with the silent 200 that Home Assistant would otherwise send for an error inside a webhook. A 200 always means the payload was accepted. A problem preparing the readings for the phone doesn't fail the sync: that response goes out without readings, and the error is logged.

### The webhook ID

The webhook ID is a long random path, so the URL can't be guessed. The signature is what makes a leaked URL harmless: without the secret, nothing posted to it is accepted.

### Signed responses

The response that carries readings to the phone is signed as well, over its exact bytes. It's signed with a key derived from the secret rather than the secret itself, so a captured request can never pass as a response. It's also bound to the request it answers. The app verifies all of that before it writes anything, even over plain HTTP, so a proxy or anyone else on the network can't put a measurement in your health record.

The response never contains the secret. The phone's next request reports which readings it wrote and which it refused, with IDs and codes, never values.

### Internal or external address

With your home network address, nothing leaves your network. With a public address, use HTTPS. Home Assistant Cloud gives the phone an `https://` cloudhook without opening a port.

### Plain HTTP

Over plain HTTP the signature still guarantees that nothing was changed in transit. Anyone on that network can read the payloads from the phone and the measurements going to it, though.

- **Android** refuses `http://` unless **Allow plain HTTP webhooks** is on. It's one switch under **Advanced**, shown on both tabs, and also in the app's own pairing dialog when the scanned address is `http://`.
- **iOS** allows plain HTTP only to an IP address, a `.local` name or a name without a dot, such as `http://192.168.1.10:8123`, `http://homeassistant.local:8123` or `http://homeassistant:8123`. There's no switch: the app refuses to pair any other `http://` address, such as `http://ha.lan:8123`, because iOS would block every request to it. For a public IP address over plain HTTP the app warns that anyone in between can read the data.

[Troubleshooting](troubleshooting.md#why-does-syncing-over-plain-http-fail) has the fixes.

### Rotating the secret

Rotate the secret any time under **Reconfigure** with **Generate a new secret**. The phone stops syncing until it has the new value: scan the new code, or paste the secret in the app.

To report a security problem privately, see [SECURITY.md](../SECURITY.md).
