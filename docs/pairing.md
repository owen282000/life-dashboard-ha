# Pairing a phone

Pairing gives the phone two things: the webhook URL to post to, and the secret it signs every payload with. The integration makes both and shows them as a QR code. Add the integration once per phone.

- [Adding a phone](#adding-a-phone)
- [Which address to use](#which-address-to-use)
- [Three ways to use the code](#three-ways-to-use-the-code)
- [What the code carries](#what-the-code-carries)
- [Names and more than one phone](#names-and-more-than-one-phone)
- [Reconfigure](#reconfigure)
- [Removing a phone](#removing-a-phone)

## Adding a phone

Go to **Settings > Devices & services > Add integration** and search for **Life Dashboard**. The dialog asks for two things:

- **Name** becomes the device name. It's "Life Dashboard" unless you change it, so give it a real name when there's more than one phone in the house.
- **Home Assistant address** is filled in with the address your browser is using at that moment, which is usually the right one. It's a plain text field, so change it when it isn't. It needs the scheme and the host, the way your browser's address bar shows it (`http://192.168.1.10:8123`, `https://home.example.com`), without `/api/webhook`.

After **Submit** the dialog shows the QR code in a frame, with three steps beside it:

1. Tap **Scan a pairing code** in the app, or point the phone's camera at the code.
2. Check what it fills in and tap **Pair**.
3. Tap **Sync Now**.

The URL and the signing secret are folded away under **Or paste by hand**, for a phone that can't scan.

<p align="center">
  <img src="screenshots/pairing.png" alt="The pairing dialog: the QR code in a frame on the left, three steps beside it, and a folded section for pasting the URL and the secret by hand" width="520">
</p>

## Which address to use

| Address | What it means for the phone |
|---|---|
| **Your home network address**, like `http://192.168.1.10:8123` | Syncs at home only, and the data never leaves your network. |
| **Your public address**, like `https://home.example.com` | Also syncs away from home. Home Assistant has to be reachable from outside, through your own reverse proxy or Home Assistant Cloud's remote access. |
| **Use Home Assistant Cloud instead** (a checkbox, shown only with an active subscription) | Also syncs away from home, with no port forwarding. The integration creates a cloudhook for you and the address field isn't used. |

Plain `http://` has limits on both phones. The Android app needs a switch for it, and an iPhone only sends plain HTTP to an IP address, a `.local` name or a name without a dot. [How it works](how-it-works.md#plain-http) has the details.

## Three ways to use the code

Scanning, with the camera or in the app, ends in the same confirmation dialog on the phone, which shows what it's about to fill in.

- **The phone's camera.** On Android the code is a link that opens the app directly, verified against the app's signing certificate. Without the app installed, it opens a page that says where to get it. An iPhone's camera always opens that pairing page in Safari, and a button on the page hands the code to the app.
- **The scan button in the app.** On Android it's **Scan a pairing code** on the **Webhook** card of the **Health** and **Screen Time** tabs (a QR icon in the address field once an address is set), and the first choice in the setup wizard. On an iPhone it's **Scan a pairing code** under **Webhook URLs** on the **Health** tab.
- **By hand.** Open **Or paste by hand** in the dialog and paste the URL and the secret into the app's webhook settings: on the **Health** tab, and on Android on the **Screen Time** tab as well.

Tapping **Pair** also sends a test ping (Android app 1.22.0 or newer, iOS app 1.4.0 or newer), so the device appears in Home Assistant right away, with its last-sync sensor.

## What the code carries

The code only carries the webhook URL, the secret and a display name for the receiver. Which data types are synced, and on what schedule, stays a choice on the phone.

The QR code is an ordinary `https://` link to the pairing page, with everything the app needs after the `#`. A browser never sends that part of a link to any server, so the secret doesn't leave the phone when the camera opens the page.

The webhook URL is your address followed by `/api/webhook/` and a long random webhook ID. The secret is 64 hex characters (32 random bytes). Both are made by the integration; there's nothing to make up yourself.

## Names and more than one phone

The name becomes the device name, and the entity IDs follow it. Two phones in the house become "Owen's Pixel" and "Partner's phone" rather than two devices that read the same, with `sensor.owen_s_pixel_steps_today` next to `sensor.partner_s_phone_steps_today`.

Add the integration once per phone. Each phone gets its own webhook ID, its own secret, its own device and its own statistics.

## Reconfigure

**Reconfigure** on the integration entry (**Settings > Devices & services > Life Dashboard**, then the three dots) shows the code and the paste-by-hand fields again. It also lets you change how the phone reaches Home Assistant:

- **Home Assistant address**: a new address for the phone. The webhook ID stays the same, so the device, its sensors and its statistics are untouched. Scan the new code, then remove the old address from the app's webhook list: pairing adds an address, it doesn't replace one.
- **Use Home Assistant Cloud instead**: switch to a cloudhook, or back to an address. Switching away from the cloud deletes the cloudhook.
- **Generate a new secret**: rotates the secret. The phone stops syncing until it has the new one, so scan the new code or paste the secret in the app.

After **Submit** the entry reloads and the dialog shows the new code.

## Removing a phone

Deleting the integration entry removes the device and its sensors, deletes the cloudhook if it had one, and deletes the phone's files in `.storage`: the history ledger, the queue of measurements for the phone and the list of apps with a screen time sensor. The long-term statistics stay in the recorder, as they do for any integration you remove.
