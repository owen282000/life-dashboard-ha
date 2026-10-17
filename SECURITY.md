# Security policy

The Life Dashboard integration takes health and app usage data from your phones into Home
Assistant, and can send measurements back to an Android phone. This page explains
[how a payload is trusted](#how-a-payload-is-trusted), how to
[report a security problem](#reporting-a-vulnerability) and [what is in scope](#scope).

## Supported versions

Only the latest release receives security fixes.

| Version | Supported |
| ------- | --------- |
| Latest release | Yes |
| Older versions | No |

## How a payload is trusted

Every payload the app sends is signed with HMAC-SHA256 over the raw body, using a secret
this integration generated when the phone was paired. The signature is verified before
anything is parsed, in constant time, and a payload that fails is answered with 401 and
logged without its contents. The webhook id itself is a long random path, so the URL is
not guessable either, but the signature is what keeps a leaked URL harmless.

The answer that carries measurements to the phone is signed too, over its exact bytes,
with a key derived from the secret rather than the secret itself, and it names the
signature of the request it answers. The app checks both before it writes anything, so a
captured request can't pass as an answer and an answer can't be replayed for another
request. [How it works](docs/how-it-works.md#security) has the details.

The secret is shown in the pairing dialog and in Reconfigure, and can be rotated there;
after a rotation the app has to be given the new value.

## Reporting a vulnerability

Please do not open a public issue for a security vulnerability.

Report it through [GitHub's private vulnerability reporting](https://github.com/owen282000/life-dashboard-ha/security/advisories/new)
for this repository. If you do not have a GitHub account, email owen282000@hotmail.com
instead, with "Security" in the subject.

Useful in a report: the integration version, the Home Assistant version, the app and its
version, what an attacker needs (a position on the network, the webhook URL, a user
account in Home Assistant), and the steps to reproduce it.

### What happens next

- You get a reply within 7 days, confirming the report and saying whether it is in scope.
- The target for a fix depends on severity: a release within 14 days for a problem that
  exposes health data or secrets, within 30 days for a moderate problem, and in a regular
  release for a minor one. If a fix needs longer, you hear why and when to expect it.
- The fix is published as a
  [GitHub security advisory](https://github.com/owen282000/life-dashboard-ha/security/advisories)
  once the release is out, with a CVE where one applies.
- You are credited in the advisory and the changelog by the name you choose, unless you
  prefer not to be named.

The project has one maintainer and no bug bounty.

## Scope

The integration handles health and app usage data, so reports about any of these are
especially welcome:

- **Signature verification.** A payload accepted with a missing or wrong signature,
  anything parsed or stored before the check, or a check that leaks how much of a
  signature was right.
- **The webhook.** A way to write to the sensors, the statistics or the ledger in
  `.storage` without the secret, a webhook id that can be guessed, or an error that answers
  with more than a status.
- **Sending measurements to the phone.** The answer the integration signs for the Android
  app's **Receive**: an answer that can be forged or replayed, one signed with the secret
  itself, the secret in an answer, or a reading that goes to a phone or a type that was not
  mapped under **Configure**.
- **The pairing code and secret rotation.** The secret leaving the part of the pairing
  link after the `#`, which a browser never sends to a server, the code or the secret shown
  to a user who is not an administrator, or an old secret that still works after
  **Generate a new secret**.
- **Logs and diagnostics.** The secret, the webhook id, the cloudhook URL or health data
  ending up in Home Assistant's log, in **Download diagnostics** or in an error message or
  repair.

Out of scope: problems that need administrator access to Home Assistant or to its
`config/.storage` folder, which hold the secret and the health data by design; Home
Assistant itself and Home Assistant Cloud, which have their own
[security policy](https://www.home-assistant.io/security/); and the phones. The Android
app has its own policy in
[owen282000/life-dashboard-companion-app](https://github.com/owen282000/life-dashboard-companion-app/security/policy),
the iOS app in
[owen282000/life-dashboard-companion-app-ios](https://github.com/owen282000/life-dashboard-companion-app-ios/security/policy),
and the receiver, database and dashboards in
[owen282000/life-dashboard-stack](https://github.com/owen282000/life-dashboard-stack/security/policy).
