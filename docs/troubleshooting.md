# Troubleshooting

The app's **Logs** tab shows every delivery attempt with Home Assistant's response, which is usually the fastest way to see what's wrong. The questions below cover what comes up most.

Syncing from the phone:

- [Why does the app log a 401?](#why-does-the-app-log-a-401)
- [Why can't the phone reach Home Assistant?](#why-cant-the-phone-reach-home-assistant)
- [Why does syncing over plain HTTP fail?](#why-does-syncing-over-plain-http-fail)
- [Why are the sensors unknown after a power cut?](#why-are-the-sensors-unknown-after-a-power-cut)
- [Why is the step history empty after a backfill?](#why-is-the-step-history-empty-after-a-backfill)
- [How do I get back an app sensor I deleted?](#how-do-i-get-back-an-app-sensor-i-deleted)

Measurements going to the phone:

- [Why isn't a measurement reaching the phone?](#why-isnt-a-measurement-reaching-the-phone)
- [Why don't refused readings come back with Send history to phone?](#why-dont-refused-readings-come-back-with-send-history-to-phone)
- [Why did a measurement arrive with the wrong time?](#why-did-a-measurement-arrive-with-the-wrong-time)
- [Why is the same weight in Health Connect twice?](#why-is-the-same-weight-in-health-connect-twice)

Getting help:

- [How do I report a bug?](#how-do-i-report-a-bug)
- [Where does the integration keep its data?](#where-does-the-integration-keep-its-data)
- [How do I turn on debug logging?](#how-do-i-turn-on-debug-logging)

## Syncing from the phone

### Why does the app log a 401?

The secret in the app isn't the one this integration has. Open **Reconfigure** on the integration to see the current secret, or scan the code again. Home Assistant's log names the phone each time it refuses a payload for this reason.

### Why can't the phone reach Home Assistant?

The address in the code is one the phone can't reach from where it is. Open **Reconfigure**, put in an address the phone can reach (your home network address at home, your public address elsewhere, or **Use Home Assistant Cloud instead**), and scan the new code. Then remove the old address from the app's webhook list: pairing adds an address, it doesn't replace one. The device and its history stay as they are. [Pairing](pairing.md#which-address-to-use) compares the options.

### Why does syncing over plain HTTP fail?

The Android app refuses `http://` unless **Allow plain HTTP webhooks** is on. That's one switch under **Advanced**, shown on both tabs, and also in the app's own pairing dialog when the scanned address is `http://`. The integration's pairing dialog has no such option.

An iPhone has no switch at all. iOS allows plain HTTP only to an IP address like `http://192.168.1.10:8123`, a `.local` name like `http://homeassistant.local:8123`, or a name without a dot, and the app refuses to pair any other `http://` address. Use **Reconfigure** to put in such an address or an `https://` one, and scan the new code.

### Why are the sensors unknown after a power cut?

Every 15 minutes, and at a clean shutdown, Home Assistant saves the values that sensors restore after a restart. A power cut loses what changed since the last save, and a sensor with nothing to restore reads unknown. It fills in again at the next sync. The statistics aren't affected.

### Why is the step history empty after a backfill?

The Android app is older than 1.17.0, and its backfill carries no daily totals, which the step, distance and calorie history is built from. Update the app and run the backfill again. Home Assistant's log names the window when this happens.

### How do I get back an app sensor I deleted?

A deleted screen time sensor for an app stays deleted, even while the phone keeps sending that app. Removing the phone and adding it again is the only way to get it back. To hide a sensor without losing it, disable it instead. [Per app](entities.md#per-app) has the rest of the rules.

## Measurements going to the phone

### Why isn't a measurement reaching the phone?

Check these in order:

- Only the Android app receives measurements, and it needs version 1.20.0 or newer.
- The type has to be mapped under **Configure** on the integration, and turned on under **Receive** on the app's **Health** tab.
- A repair on the integration entry says when the phone refused a type, for a missing permission or an app too old for it.
- A value reported again within 10 minutes of the last one is the same measurement on purpose, so it doesn't make a second record.
- The phone collects on its next sync or heartbeat, so a quiet phone takes until then.

Home Assistant's log has a warning for every reading the phone refused, with the entity and the reason. [Sending measurements to the phone](sending-to-the-phone.md#which-changes-become-a-reading) explains which changes become a reading.

### Why don't refused readings come back with Send history to phone?

Some refusals are final: a missing permission, an app too old for the type, or a reading older than 30 days while **Accept older measurements** was off. A reading refused for one of these reasons is not offered again. That includes **Send history to phone** and the `queue_history` action. Once you've fixed the cause, new measurements go through as usual. The repair text on the entry suggests the button for the refused readings, but the integration doesn't queue them again.

Before sending older history, turn on **Accept older measurements** in the app, so those readings aren't refused at all. [When the phone refuses a reading](sending-to-the-phone.md#when-the-phone-refuses-a-reading) lists every reason.

### Why did a measurement arrive with the wrong time?

Map a timestamp sensor in the **Measured at (optional)** slot under **Configure**. Without one, the moment the value changed in Home Assistant is used, which is when the scale was seen, not necessarily when you stood on it.

### Why is the same weight in Health Connect twice?

Another app on the phone, such as Zepp, Withings or Omron Connect, writes the same weighing from the scale's own connection. Health Connect keeps both, because they come from different apps. Turn one of the two off. The app warns about this under **Receive** when it sees another app writing the same type.

## Getting help

### How do I report a bug?

**Settings > Devices & services > Life Dashboard**, then the three dots, then **Download diagnostics**, gives a file with:

- which sensors exist and when each was last measured
- how many apps have a screen time sensor, but not which
- how much history the ledger holds per statistic
- which entities go to the phone, and how many readings wait, were written or were refused

The secret, the webhook ID and the cloudhook URL are redacted, and there's no health data in it. Attach it to the [issue](https://github.com/owen282000/life-dashboard-ha/issues/new/choose), with the relevant lines from Home Assistant's log.

For questions and setup help, use [Discussions](https://github.com/owen282000/life-dashboard-ha/discussions).

### Where does the integration keep its data?

Each phone has three files in Home Assistant's `.storage` folder, and unlike the diagnostics file, these do hold health data:

| File | What it holds |
|---|---|
| `life_dashboard.<entry>.history` | The ledger the statistics are built from: day totals, sessions and hourly figures. |
| `life_dashboard.<entry>.writeback` | The values of the readings waiting for the phone. |
| `life_dashboard.<entry>.apps` | The names of the apps that have a sensor and their minutes on the newest day. An app whose sensor you deleted is kept as a hash, not by name. |

They go into Home Assistant backups with the rest of `.storage`. All of a phone's files are deleted when you remove the phone from Home Assistant; its long-term statistics stay in the recorder.

### How do I turn on debug logging?

Add this to `configuration.yaml` and restart Home Assistant:

```yaml
logger:
  default: warning
  logs:
    custom_components.life_dashboard: debug
```

The debug lines say how many updates each payload brought, what was queued for the phone and what the phone wrote, without the values.
