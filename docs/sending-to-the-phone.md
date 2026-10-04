# Sending measurements to the phone

This is the other direction. **Configure** on the integration maps one entity per Health Connect type to a phone, such as a scale that Home Assistant sees over Bluetooth, a blood pressure monitor that an integration polls or a height in an input number. The Android app writes every new value to Health Connect.

This is Android only. It needs the Android app 1.20.0 or newer; the iOS app doesn't receive measurements from Home Assistant.

What shows up in Samsung Health or Google Health from there is theirs to decide. Both pick up weight and body fat from Health Connect. Whether Samsung Health shows body composition or blood pressure written by another app varies by version, and Google Health doesn't read blood pressure. The integration and the app can only promise that the reading lands in Health Connect.

- [What can go to the phone](#what-can-go-to-the-phone)
- [Setting it up](#setting-it-up)
- [Which changes become a reading](#which-changes-become-a-reading)
- [The queue](#the-queue)
- [Send history to phone](#send-history-to-phone)
- [Two phones](#two-phones)
- [When the phone refuses a reading](#when-the-phone-refuses-a-reading)

## What can go to the phone

| Type | What the entity needs | Health Connect record |
|---|---|---|
| Weight, lean body mass, bone mass, body water mass | A mass unit, such as kg, g, lb, st or oz | WeightRecord, LeanBodyMassRecord, BoneMassRecord, BodyWaterMassRecord |
| Height | A length unit, such as m, cm, mm, ft or in | HeightRecord |
| Body fat | % | BodyFatRecord |
| Blood pressure | Two entities, systolic and diastolic, with a pressure unit such as mmHg or kPa | BloodPressureRecord, with the body position and cuff location you set |

The entity can be a sensor, a number or an input number. The unit is checked when you save, not the device class, so a scale integration's weight without a device class still works. Each entity can go to one type, and the phone's own sensors aren't offered: sending them back would only echo what the phone sent.

BMI, muscle mass and visceral fat aren't offered. Health Connect has no record for them, and Samsung Health computes BMI itself from weight and height.

## Setting it up

1. **Configure** on the integration: open the section for the type, pick the entity, and save. Each type has a **Measured at (optional)** slot for a timestamp sensor that holds when the measurement was taken, such as BodyMiScale's last measurement time or an Omron's timestamp. One timestamp sensor can time several types, as a scale's does for all of its values. Without one, the moment the value changed in Home Assistant is used, and the app's log says so. For blood pressure you also set the **Body position** and the **Measurement location** stored with every reading.
2. In the app, on the **Health** tab, open **Receive** and turn on the types you want. Each asks for its Health Connect write permission once. The app lists only the types you mapped here.
3. Step on the scale. On its next sync, or its next heartbeat when there's nothing to send, the phone collects the reading and writes it. The app shows what it wrote.

Saving **Configure** reloads the entry. A type you clear there loses the readings still waiting for it, which is also the way out of a queue that got stuck on one type.

## Which changes become a reading

Values are converted to the unit Health Connect wants, from the unit the entity carries at that moment, without rounding.

- **The same value within 10 minutes is the same measurement.** A scale that advertises one weighing for a while, or an integration that polls an unchanged value, produces one record. The window slides with every report, so a value polled all day stays one reading, while the same weight the next morning is a new one.
- **A different value at the same moment is a correction.** It replaces the earlier record in Health Connect instead of adding one.
- **Systolic and diastolic that change within 90 seconds of each other are one record**, timed on the systolic.
- **A timestamp sensor that changes within 90 seconds of the value** gives the reading its time. When it updates a moment after the value, the reading moves to that time, as long as the phone hasn't collected it yet.
- **Not every state is a measurement.** A state that's unknown, unavailable or not a number is skipped, and so is an entity's first state and the state Home Assistant restores at startup. A value outside what the app accepts, such as a 0 from a scale that was set up a moment ago, isn't queued either.

A reading from an input number is marked as entered by hand in Health Connect, and blood pressure as actively measured. When the entity belongs to a device, its manufacturer and model go along, and a weight sensor is marked as coming from a scale.

## The queue

A reading waits until the phone confirms it. A failed delivery, a lost outbox or a reinstalled app costs nothing: each reading has a fixed ID that becomes Health Connect's client record ID, and Health Connect treats a reading it already has as the same record. Each response carries up to 200 readings, and the phone asks again when more are waiting.

A phone that never comes back doesn't grow the queue without end: a reading is kept for 90 days, and an entity keeps 500 readings at most, oldest out first.

A measurement that came in through Home Assistant doesn't appear a second time as the phone's own sensor. The app skips what it wrote itself when it reads Health Connect, and this integration ignores those records too, in the sensors and in the statistics. The weight is already in Home Assistant.

## Send history to phone

**Send history to phone** is a button on the device, shown while at least one entity is mapped. It queues the last 30 days of the mapped entities from the recorder. Use it for measurements from before you set this up, or for one that a restart missed.

The `life_dashboard.queue_history` action does the same with a window of up to 90 days, which is how long the queue keeps a reading, and a choice of types:

```yaml
action: life_dashboard.queue_history
data:
  days: 90
  types:
    - weight
    - blood_pressure
```

| Field | What it does |
|---|---|
| `config_entry` | Which phone to send to. Only needed with more than one phone. |
| `days` | How far back to read, from today: 1 through 90. 30 when left out. |
| `types` | Which types to send: `weight`, `height`, `body_fat`, `lean_body_mass`, `bone_mass`, `body_water_mass`, `blood_pressure`. All mapped types when left out. |

A few things to know before you press it:

- Readings older than 30 days only arrive with **Accept older measurements** on in the app. Turn it on first. The phone refuses an older reading while it's off, and a refused reading isn't offered again (see [below](#when-the-phone-refuses-a-reading)).
- A value that stayed the same is one row in the recorder, so two equal weighings in a row come back as one. For blood pressure, the half that didn't change takes its last known value.
- Pressing twice changes nothing, and a reading the phone already wrote or refused isn't queued again.
- It reads the recorder's states, not long-term statistics, because an hourly mean isn't a measurement. The recorder keeps 10 days of states unless you set `purge_keep_days` higher, so a longer window only finds what the recorder still has. Per entity, the newest 1,000 changes in the window count.

## Two phones

With two phones in the house, each phone has its own mapping. Owen's weight goes to Owen's phone, and nothing goes to the other one unless you map it there.

## When the phone refuses a reading

The phone reports back which readings it wrote and which it didn't, with a reason code and never a value. What happens next depends on the reason:

| Code | Why | What happens |
|---|---|---|
| `permission_denied` | The app has no write permission for the type. | The reading is dropped and a repair appears. |
| `unsupported_type` | The app is too old for the type. | The reading is dropped and a repair appears. |
| `too_old` | Older than 30 days while **Accept older measurements** is off. | The reading is dropped. |
| `out_of_range`, `invalid` | A value the app won't write, or a reading it can't read. | The reading is dropped. |
| `rate_limited`, `hc_unavailable` | Health Connect couldn't take it at that moment. | The reading stays and is offered again at the next sync. |

A dropped reading is not offered again: not at the next sync, not by **Send history to phone** and not by the `queue_history` action. Once you've fixed the cause, by granting the permission, updating the app or turning on **Accept older measurements**, new measurements go through as usual, but the refused ones stay out of Health Connect.

The repair is on the integration entry and names the phone and the type. It goes away once the phone writes a reading of that type again, or when you clear the mapping for that type. Each dropped reading is also logged as a warning with the entity and the code. A code the integration doesn't know, from a newer app, is treated like `rate_limited`: the reading stays and is offered again until the 90 days are up.
