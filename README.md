# Life Dashboard for Home Assistant

A Home Assistant integration that receives health and screen time data from the
[Life Dashboard Companion](https://github.com/owen282000/life-dashboard-companion-app)
app for Android, and from its
[iOS counterpart](https://github.com/owen282000/life-dashboard-companion-ios), and turns
it into sensors. No MQTT broker, no ports to open, no YAML.

Every payload is signed with HMAC-SHA256 and verified here, so nothing but your own
phone can write to these sensors.

## Install

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=owen282000&repository=life-dashboard-ha&category=integration)

That button opens HACS on your own Home Assistant with this repository already filled
in. Download **Life Dashboard**, restart Home Assistant, then go to **Settings >
Devices & services > Add integration** and search for **Life Dashboard**.

By hand instead: in HACS, three-dot menu, **Custom repositories**, paste
`https://github.com/owen282000/life-dashboard-ha` and pick category **Integration**.

Needs Home Assistant 2026.3 or newer.

## Pair a phone

The dialog asks for a name and for the address the phone should send to, then shows a
webhook URL and a signing secret.

| Address | When to use it |
|---|---|
| Internal URL | Syncs at home only, and the data never leaves your network. |
| External URL | Also syncs away from home. Home Assistant has to be reachable from outside. |
| Home Assistant Cloud | Also syncs away from home, with no port forwarding. Needs a subscription. |

Paste the URL and the secret into the **Webhook** card on the **Health** tab of the
app, and into the same card on the **Screen Time** tab. Then tap **Test**, and **Sync
now**.

The name becomes the device name, so with two phones in the house you get "Owen's
Pixel" and "Partner's iPhone" rather than two devices that read the same. Add the
integration once per phone.

To see the URL and the secret again, or to change the address or rotate the secret,
use **Reconfigure** on the integration.

## What you get

Sensors appear as the data arrives, so you only get the types you actually sync. Each
one holds the latest value; the past lives in the statistics described under
[History](#history).

**Day totals**, from Health Connect's own deduplicated figures, resetting at local
midnight: steps, distance, active calories, total calories.

**Latest reading** of heart rate, resting heart rate, heart rate variability, last
sleep duration, weight, blood pressure (systolic and diastolic), blood glucose, oxygen
saturation, body temperature, skin temperature delta, basal body temperature,
respiratory rate, last drink, body fat, lean body mass, bone mass, body water mass,
basal metabolic rate, VO2 max and height. Each one carries the source app and the
record id as attributes.

**Screen time**: minutes today, minutes yesterday, and the most used app today with
the top five in an attribute.

**Diagnostics**: last health sync and last screen time sync, as timestamps. A Test
ping in the app moves these, which is the quickest way to tell that pairing worked.

Event-like data (workouts, meals, mindfulness sessions, cycle tracking) gets no
sensor, because a single value cannot represent it honestly. It is in the webhook
payload, so an automation can still use it.

### On an iPhone

The iOS app pairs exactly the same way. It sends no day totals, so the four "today"
sensors stay absent, and iOS has no screen time API that allows exporting, so those
three sensors are Android only. Everything else behaves identically.

## History

A sensor can only hold now, so on its own a year of backfilled readings would all land
on today. Instead every payload also feeds long-term statistics, which carry their own
dates:

| Statistic | What it holds |
|---|---|
| steps, distance, active calories, total calories | The day's total, from the app's daily totals |
| sleep minutes, exercise minutes, mindfulness minutes | The day's total, from the sessions that ended that day |
| hydration total | Litres drunk that day |
| heart rate, weight, blood pressure and the other measured values | Mean, minimum and maximum per hour |

Find them in the **Statistics graph** card, or anywhere else that lists statistics,
under the name of the phone. They are separate from the sensors: the sensor shows the
latest reading, the statistic shows how it went.

A backfill from the app fills them for the whole window it covers, and sending the
same window again changes nothing. Step, distance and calorie history needs app 1.17.0
or newer, which sends the day totals for every backfilled day; older versions only
send today's, and the log says so when a backfill arrives without them.

## Why nothing is double counted

The app re-sends data on purpose: a delivery that failed goes into an outbox and comes
back at the next sync, a large backlog is split over several requests, and a backfill
run re-sends history with its original timestamps. Health Connect also hands over a
record again after the source app edits it.

So every value carries the moment it describes, and a sensor takes an update only when
it is not older than what it already holds. Re-delivery is therefore harmless, and an
old batch arriving after a restart cannot overwrite a newer reading.

## MQTT or this integration

The app also publishes to MQTT with Home Assistant Discovery, and the sensor names
here match those. Pick one: running both gives you two devices holding the same
numbers. MQTT keeps working exactly as before, and is the better choice if you already
have a broker and want the raw records.

## Troubleshooting

**The app logs a 401.** The secret in the app is not the one this integration has.
Open Reconfigure to see the current secret, and paste it into both tabs.

**The app cannot reach the URL.** Set your addresses under **Settings > System >
Network**, then open Reconfigure to get the corrected URL. Home Assistant otherwise
guesses, and in a container that guess can be an address only the container can reach.

**The form says no internal or external URL is set.** Same place: **Settings > System
> Network**.

**Syncing over plain HTTP fails.** The app refuses `http://` unless you enable
**Allow plain HTTP webhooks** on the tab you are configuring.

**Sensors are "unknown" after a power cut.** Home Assistant only keeps sensor values
across a restart it shut down cleanly. They fill in again at the next sync.

## Development

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
```

`scripts/sync-to-dev-ha.sh` copies the integration into a local Home Assistant in
Docker and restarts it.

## License

MIT, see [LICENSE](LICENSE).
