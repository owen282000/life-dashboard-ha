# Security Policy

## Supported Versions

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

The secret is shown in the pairing dialog and in Reconfigure, and can be rotated there;
after a rotation the app has to be given the new value.

## Reporting a Vulnerability

Please do not open a public issue for security vulnerabilities.

Use [GitHub's private vulnerability reporting](https://github.com/owen282000/life-dashboard-ha/security/advisories/new)
for this repository. You will get a response as soon as possible, and a fix will be
prioritised based on severity.

Since this integration handles health and app usage data, reports about the following are
especially welcome:

- A way to write to the sensors or the statistics without the secret
- Weaknesses in the signature verification
- Health data ending up in logs, diagnostics or error messages
