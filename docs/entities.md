# Sensors and statistics

Each phone is one device, with a sensor for every value it sends and long-term statistics for the past. This page lists all of them, with the attributes, the screen time sensors per app, an example dashboard and a few automations.

- [What you get](#what-you-get)
- [Screen time](#screen-time)
- [A dashboard to start from](#a-dashboard-to-start-from)
- [History](#history)
- [In automations](#in-automations)

## What you get

Sensors appear as data arrives, so you only get the types you actually sync. Each sensor holds the latest value; the past lives in the [statistics](#history).

The tables give the last part of each entity ID. The first part is the device name, so a phone named "Owen's Pixel" gives `sensor.owen_s_pixel_steps_today`.

The device's firmware field shows the app version the phone last reported, so you can see which version each phone runs.

### Day totals

These come from Health Connect's own deduplicated figures, or HealthKit's statistics on an iPhone. Each covers one local day from midnight and carries that day as a `date` attribute. A sensor moves on to the new day with the first sync after midnight.

| Sensor | Unit |
|---|---|
| `steps_today` | steps |
| `distance_today` | m |
| `active_calories_today` | kcal |
| `total_calories_today` | kcal |

### Latest reading

Each holds the newest record, with the time it describes as `measured_at`, the app that wrote it as `source` and the record ID as `uuid`. A series that the app sends in time buckets (an average per 1, 5 or 15 minutes, or per hour) carries `sample_count`, `min`, `max` and `sources` instead.

| Sensor | Unit | | Sensor | Unit |
|---|---|---|---|---|
| `heart_rate` | bpm | | `body_temperature` | °C |
| `resting_heart_rate` | bpm | | `skin_temperature_delta` | °C |
| `heart_rate_variability` | ms | | `basal_body_temperature` | °C |
| `last_sleep_duration` | min | | `respiratory_rate` | breaths/min |
| `weight` | kg | | `last_drink` | L |
| `blood_pressure_systolic` | mmHg | | `body_fat` | % |
| `blood_pressure_diastolic` | mmHg | | `lean_body_mass` | kg |
| `blood_glucose` | mmol/L | | `bone_mass` | kg |
| `oxygen_saturation` | % | | `body_water_mass` | kg |
| `basal_metabolic_rate` | kcal/d | | `vo2_max` | mL/min/kg |
| `height` | m | | | |

### Screen time sensors

Android only. [Screen time](#screen-time) below explains them.

| Sensor | Unit | Attributes |
|---|---|---|
| `screen_time_today` | min | `date`, `app_count`, `top_apps` (the top five with their minutes) |
| `screen_time_yesterday` | min | `date`, `app_count`, `top_apps` |
| `most_used_app_today` | the app's name | `package`, `minutes`, `date` |
| `<app>_screen_time`, one per app, [disabled](#per-app) until you enable it | min | `app`, `package`, `date`, `week_minutes` |

When **Apps to send** in the app leaves some apps out, `screen_time_today` and `screen_time_yesterday` count only the apps that are sent, and carry the total of every app as `all_apps_minutes`.

### Diagnostics

`last_health_sync` and `last_screen_time_sync` are timestamps of the last payload of each kind. `last_health_sync` also carries the app version and how many records the payload held; `last_screen_time_sync` carries the app version and the phone's manufacturer and model.

A **Test ping** in the app updates the one for its tab, which is the quickest way to see that pairing worked. A backfill doesn't; the next regular sync does.

### What gets no sensor

Nine of the 33 Health Connect types are event-like: workouts, meals, mindfulness sessions, sexual activity and the cycle tracking types (period, flow, intermenstrual bleeding, ovulation tests, cervical mucus). A single value can't represent them honestly, so they get no sensor. Exercise and mindfulness do count toward the [day statistics](#history).

For the raw records, add a second webhook URL in the app that points at your own backend or at an automation with a webhook trigger. Neither this integration nor MQTT passes them on.

## Screen time

The Android app reads Android's usage statistics, the same numbers as the Digital Wellbeing screen, and sends them on their own schedule, separate from the health sync. Three sensors come out of it:

| Sensor | What it holds |
|---|---|
| `screen_time_today` | Minutes of foreground use since the day boundary, which you set in the app (a "day" can end at 4 AM if that's when you sleep). |
| `screen_time_yesterday` | The finished total for the day before, for a daily automation that doesn't race the clock. |
| `most_used_app_today` | The app with the most minutes today, with its package name and minutes as attributes. It reads `none` when **Apps to send** leaves out every app used today. The top five with their minutes are the `top_apps` attribute of both minutes sensors. |

Both minutes sensors carry the day they describe as an attribute, and update as the number grows, so a dashboard shows the phone's day as it happens. Every day also goes into long-term statistics, so a year from now the statistics graph still shows your heaviest phone weeks.

Two things people do with it:

```yaml
# Say something when the phone passes three hours in a day.
triggers:
  - trigger: numeric_state
    entity_id: sensor.owen_s_pixel_screen_time_today
    above: 180
actions:
  - action: notify.mobile_app_owen_s_pixel
    data:
      message: >-
        Three hours on the phone today.
        Top app: {{ states('sensor.owen_s_pixel_most_used_app_today') }}.
```

```yaml
# Yesterday's total and its top five apps in a card.
type: markdown
title: Yesterday on the phone
content: >-
  {{ states('sensor.owen_s_pixel_screen_time_yesterday') }} minutes.
  {{ state_attr('sensor.owen_s_pixel_screen_time_yesterday', 'top_apps') }}
```

Screen time is Android only: iOS has no API that lets an app read it and send it anywhere. Which apps count is decided on the phone, so Home Assistant only ever sees what you chose to send.

### Per app

The apps you use also get a sensor each with their minutes today, such as `sensor.owen_s_pixel_youtube_screen_time`. They're created disabled, so the device page stays as it was. Open the device, show the disabled entities, and enable the apps you want a sensor for. Home Assistant reloads the integration about 30 seconds later, and the sensor shows today's minutes right away.

- The sensor belongs to the app's package, not its name, so a renamed app keeps its sensor and entity ID. The new name shows up in the `app` attribute right away, and the entity name follows at the next restart.
- An app that's missing from a newer day reads 0 for that day, even when you leave it out of **Apps to send**: it never keeps yesterday's minutes. `week_minutes` holds its minutes over the days the last sync carried, up to seven.
- An app gets a sensor once it has 5 minutes over those days, so an app opened once for a moment gets none. A phone gets 50 app sensors at most, disabled ones included, the most used apps first. An app keeps its sensor when you stop using it, so automations built on it keep working.
- A sensor you delete stays deleted, even after a restart and while the phone keeps sending the app, and it frees its slot. Removing the phone and adding it again is the only way to get it back.
- An app you leave out of **Apps to send** never reaches Home Assistant and gets no sensor.
- Like `screen_time_today` they have no state class. The recorder keeps their history as states, for its usual 10 days, and they add nothing to long-term statistics.

```yaml
# Turn the TV off after an hour of a game on a school day.
triggers:
  - trigger: numeric_state
    entity_id: sensor.kids_tablet_minecraft_screen_time
    above: 60
conditions:
  - condition: time
    weekday: [mon, tue, wed, thu, fri]
actions:
  - action: media_player.turn_off
    target:
      entity_id: media_player.living_room_tv
```

## A dashboard to start from

[examples/dashboard.yaml](../examples/dashboard.yaml) is a view for one phone in three sections:

- **Today**: tiles for steps, distance, calories, screen time, heart rate and last sleep, a body card, and the two sync times.
- **On the phone**: the most used app, the top apps of the day, and screen time per day.
- **History**: steps per week and heart rate per day.

Paste it into a new dashboard's raw configuration editor. Replace `owen_s_pixel` in the entity IDs with your phone's name, and the three statistic IDs with the ones the picker offers under that name. Drop the tiles and rows for types you don't sync; Home Assistant shows a missing entity as an error.

With the sensor's native unit, `min`, the last sleep tile shows minutes only. To see hours and minutes, set the sensor's unit to `h` in its settings and raise the display precision to four decimals. The frontend rounds down when it splits hours into minutes, so the tile can show a minute less.

<p align="center">
  <img src="screenshots/dashboard.png" alt="The example dashboard in three sections: Today with colored tiles for steps, distance, calories, screen time, heart rate and last sleep plus the two sync times; On the phone with the most used app, the top apps of the day and a bar chart of screen time per day; History with steps per week as bars and heart rate per day as a line with its min and max band" width="900">
</p>

That's the view on a phone that backfilled five months and then went quiet, with the two long graphs widened from 90 and 30 days to a year so the backfill shows.

## History

A sensor can only hold now, so on its own a year of backfilled readings would all land on today. Instead every payload also feeds long-term statistics, which carry their own dates. The sensors themselves have no state class, so the recorder doesn't compile a second set of statistics from them.

| Statistic | What it holds |
|---|---|
| `steps`, `distance`, `active_calories`, `total_calories` | The day's total, from the app's daily totals. |
| `sleep_minutes`, `exercise_minutes`, `mindfulness_minutes` | The day's total, from the sessions that ended that day. |
| `hydration_total` | Liters drunk that day. |
| `screen_time` | Minutes on the phone that day. Every sync carries the last seven days, so a day that was still running when it was sent is corrected by the next one. |
| `heart_rate`, `weight`, `blood_pressure_systolic`, `blood_pressure_diastolic` and every other value in the [latest reading](#latest-reading) table except sleep and the last drink | Mean, minimum and maximum per hour. |

A day without data has no row, rather than a zero. Each statistic's ID is `life_dashboard:<entry>_<statistic>`, where `<entry>` is the phone's config entry ID in lowercase, and its name is the device name followed by the statistic, such as "Owen's Pixel steps".

They show up in the entity picker of the **Statistics graph** card under the phone's name, next to the sensors. The steps chart in the screenshot is this card:

```yaml
type: statistics-graph
title: Steps per week
chart_type: bar
period: week
days_to_show: 90
stat_types:
  - change
entities:
  - life_dashboard:<entry>_steps   # pick "Owen's Pixel steps" from the picker
```

<p align="center">
  <img src="screenshots/statistics.png" alt="Two statistics graph cards fed by the integration: steps per week as bars, and heart rate per day as a line with its min and max band" width="520">
</p>

A backfill from the app fills the statistics for the whole window it covers, and sending the same window again changes nothing. [How it works](how-it-works.md#why-nothing-is-double-counted) explains why. When you remove the phone, its statistics stay in the recorder.

## In automations

The sensors are ordinary sensors. Two that people set up first:

```yaml
# A goal for the day.
triggers:
  - trigger: numeric_state
    entity_id: sensor.owen_s_pixel_steps_today
    above: 10000
actions:
  - action: notify.mobile_app_owen_s_pixel
    data:
      message: "10,000 steps. Done for today."
```

```yaml
# The phone hasn't synced for a day, so something is off with the app or the network.
triggers:
  - trigger: template
    value_template: >-
      {{ now() - (states('sensor.owen_s_pixel_last_health_sync') | as_datetime)
         > timedelta(hours=24) }}
actions:
  - action: notify.mobile_app_owen_s_pixel
    data:
      message: "No health sync from the phone for a day."
```
