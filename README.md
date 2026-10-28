<p align="center">
  <img src="https://raw.githubusercontent.com/owen282000/life-dashboard-ha/main/docs/readme-icon.png" alt="Life Dashboard icon" width="128" height="128">
</p>

<h1 align="center">Life Dashboard for Home Assistant</h1>

<p align="center">
  <b>Steps, sleep, heart rate, weight and workouts from your phone as Home Assistant sensors, with a year of history.</b><br>
  Screen time per app on Android. Paired with a QR code: no broker, no YAML.
</p>

<p align="center">
  <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=owen282000&repository=life-dashboard-ha&category=integration"><img src="https://img.shields.io/badge/HACS-Default-41BDF5" alt="In the default HACS list"></a>
  <a href="https://github.com/owen282000/life-dashboard-ha/releases/latest"><img src="https://img.shields.io/github/v/release/owen282000/life-dashboard-ha?label=Release&color=30b77e" alt="Latest release"></a>
  <a href="https://www.home-assistant.io/"><img src="https://img.shields.io/badge/Home%20Assistant-2026.3%2B-41BDF5" alt="Home Assistant 2026.3 or newer"></a>
  <a href="https://github.com/owen282000/life-dashboard-ha/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue" alt="MIT license"></a>
</p>

<p align="center">
  <a href="https://github.com/owen282000/life-dashboard-ha#quick-start"><b>Quick&nbsp;start</b></a>
  &nbsp;·&nbsp;
  <a href="https://github.com/owen282000/life-dashboard-ha/blob/main/docs/pairing.md">Pairing</a>
  &nbsp;·&nbsp;
  <a href="https://github.com/owen282000/life-dashboard-ha/blob/main/docs/entities.md">Sensors</a>
  &nbsp;·&nbsp;
  <a href="https://owen282000.com/life-dashboard/home-assistant/">Website</a>
</p>

<p align="center">
  <sub>Life Dashboard: <a href="https://github.com/owen282000/life-dashboard-companion-app">Android app</a> &nbsp;·&nbsp; <a href="https://github.com/owen282000/life-dashboard-companion-app-ios">iPhone app</a> &nbsp;·&nbsp; <b>Home Assistant integration</b> &nbsp;·&nbsp; <a href="https://github.com/owen282000/life-dashboard-stack">Grafana stack</a></sub>
</p>

> **You need the Life Dashboard Companion app on your phone.** It's a separate app, not the Home Assistant Companion app: it reads Health Connect (Android) or Apple Health (iPhone) and sends the data to this integration. Without it, this integration has nothing to show.
>
> - **Android:** on [Google Play](https://play.google.com/store/apps/details?id=com.owen282000.lifedashboard&referrer=utm_source%3Dgh-ha-readme%26utm_medium%3Dreadme) as a one-time purchase: the easy way, with automatic updates, and it supports development. The same app stays free [from GitHub](https://github.com/owen282000/life-dashboard-companion-app#install), on [F-Droid](https://f-droid.org/packages/com.owen282000.lifedashboard/) or through Obtainium (open source, MIT).
> - **iPhone:** on the [App Store](https://apps.apple.com/app/apple-store/id6824075761?pt=128699800&ct=gh-ha-readme&mt=8) as a one-time purchase, or [build it yourself with Xcode](https://github.com/owen282000/life-dashboard-companion-app-ios#build-and-install) on a Mac, which costs nothing extra if you already have an Apple Developer Program membership.
>
> <a href="https://play.google.com/store/apps/details?id=com.owen282000.lifedashboard&referrer=utm_source%3Dgh-ha-readme%26utm_medium%3Dreadme"><img src="https://play.google.com/intl/en_us/badges/static/images/badges/en_badge_web_generic.png" alt="Get it on Google Play" height="60" align="middle"></a> <a href="https://apps.apple.com/app/apple-store/id6824075761?pt=128699800&ct=gh-ha-readme&mt=8"><img src="https://raw.githubusercontent.com/owen282000/life-dashboard-ha/main/docs/badges/app-store.png" alt="Download on the App Store" height="40" align="middle"></a>

**Works with your watch or scale.** Samsung Health and Galaxy Watch, Fitbit and Pixel Watch, Garmin, Oura, Withings: if its app syncs to Health Connect, the data shows up here. That includes Fitbit after Google switches off the Fitbit Web API on 30 October 2026. On an iPhone, anything in Apple Health.

<p align="center">
  <img src="https://raw.githubusercontent.com/owen282000/life-dashboard-ha/main/docs/readme-hero.png" alt="The integration's example dashboard: today's steps, distance, calories, screen time, heart rate and sleep, weight, resting heart rate and blood pressure, the top apps of the day, and charts of screen time and history" width="900">
</p>

## What you get

- **Sensors for today.** Steps, distance and calories as day totals, and the latest heart rate, sleep, weight, blood pressure, glucose and more, each with the app that recorded it.
- **A year of history.** Every day goes into long-term statistics on its own date, so a backfill from the phone fills a year of the **Statistics graph** card. That card shows it, not the sensor's own history.
- **Workouts.** The last workout and the minutes of today and yesterday, with every session's kind, start and end, and what was recorded during it: calories, distance, pace or speed, heart rate, steps, laps and sets. A shortest workout setting keeps the walks the phone notices by itself out.
- **Screen time per app** (Android). Minutes today and yesterday, the most used app and the top five, and a sensor per app if you want one.
- **Back into Health Connect** (Android). Map a scale or blood pressure entity, and the phone writes each new reading into Health Connect, where Samsung Health and Google Health pick it up.
- **An example dashboard** to start from: [examples/dashboard.yaml](https://github.com/owen282000/life-dashboard-ha/blob/main/examples/dashboard.yaml).

## How it works

The app on the phone reads Health Connect or Apple Health and sends each sync to a webhook this integration sets up when you add the phone. The integration checks the HMAC signature, updates the sensors, and writes every day into long-term statistics on its own date, so a backfill from the phone fills the history graphs too. [How it works](https://github.com/owen282000/life-dashboard-ha/blob/main/docs/how-it-works.md) has the timing and why nothing is counted twice.

## Quick start

About ten minutes, on the phone and in Home Assistant 2026.3 or newer with [HACS](https://hacs.xyz/docs/use/).

1. **On the phone, first.** Install the Life Dashboard Companion app (see above). In your watch's app, switch on syncing to Health Connect: in Samsung Health it's **Settings > Health Connect**, and Fitbit, Garmin Connect and others have a similar setting. Open Life Dashboard and allow it to read Health Connect, including in the background (on an iPhone: Apple Health).
2. **Install the integration.** In HACS, search for **Life Dashboard**, download it and restart Home Assistant.

   [![Open Life Dashboard in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=owen282000&repository=life-dashboard-ha&category=integration)

3. **Add a phone.** Go to **Settings > Devices & services > Add integration**, search for **Life Dashboard** and give the phone a name. The address is the one the phone sends to; see [the next section](https://github.com/owen282000/life-dashboard-ha#reaching-home-assistant-from-the-phone) if you're not sure.
4. **Scan the code** in the app, with **Scan a pairing code**, or with the phone's camera. The app shows what it will fill in and asks you to confirm.
5. **Sync.** Tap **Sync Now**. The phone shows up as a device under **Settings > Devices & services > Life Dashboard**, with a sensor for every type it sent. To fill a year of history, run a backfill in the app.

If the sensors stay empty, the phone's [troubleshooting page](https://owen282000.com/life-dashboard/troubleshooting/) goes through it step by step.

## Reaching Home Assistant from the phone

The phone sends its data to the address in the pairing code, so that address has to work from the phone.

- **Only at home:** your home network address, such as `http://192.168.1.10:8123` (Settings > System > Network). `homeassistant.local` often doesn't work on Android. Syncs away from home wait until you're back.
- **Also away from home:** a VPN to your home network (WireGuard, Tailscale), with the home address kept as it is; your own `https://` address through a reverse proxy; or Home Assistant Cloud, which the dialog offers when you have a subscription.

[Pairing](https://github.com/owen282000/life-dashboard-ha/blob/main/docs/pairing.md) has the details, a second phone and changing the address later.

## Already use the Home Assistant Companion app?

Keep it: it handles presence, notifications and device sensors, and this works next to it. Its health sensors show today's value and keep history only from the day you switch them on. This integration adds up to a year of backfilled history, each day on its own date, workouts with every session, screen time per app on Android, and readings written back into Health Connect.

## Part of Life Dashboard

Four projects, and you only install the parts you need: the app on each phone, then where the data goes. An Android phone and an iPhone send the same payload, so they can share one Home Assistant.

<p align="center">
  <a href="https://github.com/owen282000/life-dashboard-companion-app"><img src="https://raw.githubusercontent.com/owen282000/life-dashboard-ha/main/docs/family/android.png" alt="Android app: Health Connect and screen time" width="400"></a>
  <a href="https://github.com/owen282000/life-dashboard-companion-app-ios"><img src="https://raw.githubusercontent.com/owen282000/life-dashboard-ha/main/docs/family/ios.png" alt="iPhone app: Apple Health" width="400"></a>
  <img src="https://raw.githubusercontent.com/owen282000/life-dashboard-ha/main/docs/family/ha-here.png" alt="Home Assistant integration: sensors and a year of history (this repository)" width="400">
  <a href="https://github.com/owen282000/life-dashboard-stack"><img src="https://raw.githubusercontent.com/owen282000/life-dashboard-ha/main/docs/family/stack.png" alt="Grafana stack: Postgres and Grafana dashboards" width="400"></a>
</p>

## Ask your own AI

The sensors show today, and the statistics keep a year. To ask an AI about that history, such as how your sleep changed in the weeks you walked more, the [Life Dashboard stack](https://github.com/owen282000/life-dashboard-stack#ask-your-own-ai-mcp) stores every record in Postgres and has an MCP server with eight read-only tools for Claude, a local model in Ollama or any other MCP client. The integration itself contains no AI. The app can send to Home Assistant and the stack at the same time.

## Privacy and security

- **Your data goes from the phone to your Home Assistant, nowhere else.** The integration makes no requests of its own and has no telemetry or analytics. There's no server of mine in the path, and nothing to switch off if I stop.
- **Signed, always.** Every payload is signed with HMAC-SHA256 and a secret this integration created, and checked before anything is read. A leaked URL is useless without the secret. The QR code contains that secret, so treat it like a password. The signature proves who sent the data but doesn't encrypt it: use `https://` or a VPN when the data leaves your home network.
- **The QR code** is a link to a static pairing page on GitHub Pages, with the address and secret after the `#`. Browsers never send that part to any server.
- **Backed up with Home Assistant.** Sensors and statistics live in Home Assistant's own database, so your regular Home Assistant backups include them.
- **Diagnostics** on the integration give a file for bug reports with the secret redacted and no health data in it.

[How it works](https://github.com/owen282000/life-dashboard-ha/blob/main/docs/how-it-works.md) explains the timing, why nothing is counted twice, and the security design. To report a vulnerability privately, see [SECURITY.md](https://github.com/owen282000/life-dashboard-ha/blob/main/SECURITY.md).

## Documentation

- [Sensors and statistics](https://github.com/owen282000/life-dashboard-ha/blob/main/docs/entities.md): every entity, screen time per app, automations
- [Sending measurements to the phone](https://github.com/owen282000/life-dashboard-ha/blob/main/docs/sending-to-the-phone.md): scales and blood pressure monitors into Health Connect
- [Troubleshooting](https://github.com/owen282000/life-dashboard-ha/blob/main/docs/troubleshooting.md): the questions that come up most, diagnostics and debug logging
- [Changelog](https://github.com/owen282000/life-dashboard-ha/blob/main/CHANGELOG.md)
- Prefer MQTT? Both apps can publish with MQTT Discovery instead, for current values only. Or keep a year of history in Postgres and Grafana with the [Life Dashboard stack](https://github.com/owen282000/life-dashboard-stack).

## Help and contributing

Questions go to [Discussions](https://github.com/owen282000/life-dashboard-ha/discussions), bugs to [issues](https://github.com/owen282000/life-dashboard-ha/issues/new/choose). The integration is Python, and [CONTRIBUTING.md](https://github.com/owen282000/life-dashboard-ha/blob/main/CONTRIBUTING.md) has the developer setup and the tests. Pull requests are welcome.

## Support the project

Stars, shares and good bug reports all help. If you want to chip in for the evenings that go into it, there's [Ko-fi](https://ko-fi.com/owen282000). The integration stays free and open source (MIT) either way.
