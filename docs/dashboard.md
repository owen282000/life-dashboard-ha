# The Health dashboard

An example dashboard for the integration, built only from Home Assistant's own cards, with a theme in the colors of the Life Dashboard apps. It isn't installed for you: you copy it, put your phone's name in, and remove what your phone doesn't send. No custom cards to install.

<p align="center">
  <img src="screenshots/dashboard-hero.png" alt="The top of the Health dashboard: today's tiles, the last workout with its pace, heart rate and laps, last night's sleep, and recovery with heart rate variability against its baseline, resting heart rate against last week, and VO2 max" width="900">
</p>

The screenshots show demo data from two made-up phones, not a real person's.

## What's on it

The Health view, in [examples/dashboard.yaml](../examples/dashboard.yaml):

- **Today**: steps, distance, active and total calories, workout time and heart rate as tiles, and the last workout with what was recorded during it: pace, heart rate, calories, steps and laps.
- **Recovery**: heart rate variability over the last 7 days against your 60-day baseline, resting heart rate over the last 7 days against the 7 before, heart rate variability per week over a year, and VO2 max.
- **Sleep**: last night on a gauge, the night's breathing and oxygen, the skin temperature against your normal, and sleep per night.
- **Training**: workout time this week, last week, this month and the last 4 weeks, today's workouts, workout time per week, and VO2 max per month.
- **Heart**: resting heart rate on average and per day, and heart rate over the last 48 hours.
- **Activity**: steps this week and this month, steps per day, distance per week from every activity, and active calories per day.
- **Body**: weight and body fat, per week over a year.
- **Sync**: when the phone last sent, and the app version.

The graphs read Home Assistant's long-term statistics, so a day shows up on its own date, also when it came in with a backfill. The graphs look back from 48 hours to a year; change `days_to_show` on a card to look further back. Distance is in km, and sleep and screen time in hours.

<p align="center">
  <img src="screenshots/dashboard-phone.png" alt="The top of the same dashboard on a phone: today's tiles two per row and the last workout" width="300">
</p>

The full page on a wide screen: [screenshots/dashboard-full-dark.png](screenshots/dashboard-full-dark.png).

## Setting it up

Do this on a computer: the raw configuration editor is much easier with a real keyboard and find and replace.

1. **The theme.** Home Assistant reads themes from a `themes` folder in its configuration folder.
   - Get to the files: on Home Assistant OS, install the **File editor** add-on, or use Samba or SSH.
   - Create a folder `themes` next to `configuration.yaml`, and put [examples/themes/life_dashboard.yaml](../examples/themes/life_dashboard.yaml) in it.
   - Add these lines to `configuration.yaml`, or only the `themes:` line if you already have a `frontend:` section:

     ```yaml
     frontend:
       themes: !include_dir_merge_named themes
     ```

   - Restart Home Assistant once. After that, the action `frontend.reload_themes` picks up a change to the file.

   The dashboard sets the theme itself, so your other dashboards keep theirs. The file also has a small theme, **Life Dashboard heart**, that colors the icons of the heart cards red. Don't want the themes? Skip this step and delete the `theme:` lines from the dashboard before you paste it.

2. **The dashboard.** Go to **Settings > Dashboards > Add dashboard > New dashboard from scratch**, give it a name and open it. Choose **Edit dashboard** from the three dots at the top right, then **Raw configuration editor** from the three dots again. Select everything that's there, paste [examples/dashboard.yaml](../examples/dashboard.yaml) in its place, and save.

3. **Your phone.** The file has two placeholders. Replace each with find and replace: in the editor itself (Ctrl+F, or Cmd+F on a Mac), or in a text editor before you paste.
   - **`your_phone`** in the entity IDs. Open your phone under **Settings > Devices & services > Life Dashboard**, click its steps sensor, then the cog. The entity ID reads like `sensor.alex_s_pixel_steps_today`, and the part between `sensor.` and `_steps_today` is what goes in place of `your_phone`.
   - **`<entry>`** in the statistic IDs, such as `life_dashboard:<entry>_steps`. Go to **Developer tools > Statistics** and search for your phone's name and "steps". The statistic ID reads like `life_dashboard:01j..._steps`, and the part between `life_dashboard:` and `_steps` is what goes in place of `<entry>`.

4. **What your phone doesn't send.** Your phone's device page lists the sensors it has. Remove the tiles and graphs for anything that isn't there, because Home Assistant shows a missing entity as an error.

   What a phone sends depends on the watch and its app:
   - **A Samsung Galaxy Watch** through Samsung Health shares no heart rate variability, resting heart rate, skin temperature or breathing with Health Connect, so remove Recovery, the night's breathing and skin temperature, and the resting heart rate cards.
   - **An iPhone** sends no skin temperature and no screen time. Its heart rate variability is SDNN where Android's is RMSSD, so it compares well with its own baseline but not with an Android phone's numbers ([entities.md](entities.md)).

The graphs start with what the phone has sent so far. To fill them with your history, tap **Send history** under **What to send** on the **Health** tab of the Life Dashboard app and pick how far back. The days land on their own dates, as far back as the phone has data.

## Screen time

<p align="center">
  <img src="screenshots/screen-time.png" alt="The screen time view: today, yesterday, the most used app and the number of apps, six apps with their time today as bars, screen time per day, and the apps per day stacked" width="700">
</p>

Screen time comes from the Android app only, so it has its own view in its own file: [examples/dashboard-screen-time.yaml](../examples/dashboard-screen-time.yaml). Paste it as a dashboard of its own, or copy its view under `views:` into the Health dashboard. Replace `your_phone` and `<entry>` the same way.

The six apps are examples. Each app gets a sensor that starts disabled: open the phone's device page, show the disabled entities, and enable the apps you want. An enabled app also gets a statistic with its time per day, `life_dashboard:<entry>_app_<package>`, with the dots of the package name as underscores, and that's what the stacked graph reads. It starts with the next sync, which carries the last seven days. More in [entities.md](entities.md#per-app).

## Two phones

<p align="center">
  <img src="screenshots/two-phones.png" alt="Two phones side by side: each phone's steps, calories, sleep, resting heart rate, weight and last workout, and graphs with both phones in one: resting heart rate per week, steps per day, sleep per night and distance per month" width="900">
</p>

Every phone is a device of its own, an Android phone and an iPhone alike. [examples/dashboard-two-phones.yaml](../examples/dashboard-two-phones.yaml) puts two side by side and draws both in the same graphs. Replace `first_phone`, `second_phone`, `<first_entry>` and `<second_entry>` the same way, and the headings, names and phone icons in the graphs. Paste it as a dashboard of its own, or copy its view into the Health dashboard.

## Notes

- Home Assistant 2026.3 or newer, the same as the integration.
- The header line names the phone and where its data comes from, Health Connect or Apple Health, from the device and the last sync.
