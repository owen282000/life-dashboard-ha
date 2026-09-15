# Life Dashboard for Home Assistant

A Home Assistant integration that receives health and screen time data from the
[Life Dashboard Companion](https://github.com/owen282000/life-dashboard-companion-app)
app for Android, and its [iOS counterpart](https://github.com/owen282000/life-dashboard-companion-ios),
and turns it into sensors. No MQTT broker, no ports to open, no YAML.

**Work in progress.** The repository layout, CI and packaging are in place; the config
flow, the webhook and the sensors are being built. Until the first release, use the
app's MQTT Discovery support, which is documented in
[the app's usage guide](https://github.com/owen282000/life-dashboard-companion-app/blob/main/docs/usage.md).

## How it will work

1. Install from HACS as a custom repository.
2. Add the integration. It shows a webhook URL and a signing secret.
3. Paste both into the Webhook card on the Health tab and on the Screen Time tab of the
   app, then tap Sync now.
4. Sensors appear under one device per phone, with the device class and state class set
   so they land in long-term statistics.

Every payload is signed with HMAC-SHA256 and verified here, so the integration does not
accept data from anything but your phone.

## License

MIT, see [LICENSE](LICENSE).
