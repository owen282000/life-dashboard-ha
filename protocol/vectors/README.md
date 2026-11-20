# Protocol vectors

Examples of the wire protocol between the Life Dashboard apps and this integration, with their exact bytes: a request and its signature, the answer the integration gives, and what the phone does with an answer. They exist so that the Android app, the iOS app and the integration are tested against the same examples instead of each against its own.

They live here, outside `tests/`, because they are not this repository's tests: they are the contract, and the tests of three repositories read them. The integration is the side that answers, so its repository is where they change, in the same pull request as the code. Nothing in this folder is shipped: Home Assistant and HACS install `custom_components/life_dashboard` only.

## The files

Both are JSON, UTF-8, and carry the same `version`. Every body in them is a JSON string: the bytes on the wire are the UTF-8 encoding of that string. Read the file with a JSON parser, take the string, encode it as UTF-8, and never parse and write a body again before signing or checking it.

| File | For whom | What a case holds |
|---|---|---|
| [exchanges.json](exchanges.json) | The integration, and any other receiver | `secret`, `request.body` and `request.signature` (the `X-Signature` of the request, or `null` when it is left out), and under `expect` the `status` and the answer: `answer` (the answer's text), `answer_signature` (over that text), `body` (what is sent: the answer itself, or the signed body around it when `signed_body` is true) and `x_signature` (the header, over `body`). A refusal has a `status` and an empty `body`. `integration.version` and `integration.now` at the top are the version the answer names and the moment it is issued. |
| [answers.json](answers.json) | The apps | `secret`, `request_signature` (the `X-Signature` the app put on its request), `now` (the phone's clock), `response` (`status`, `content_type`, `headers`, `body`: what reached the phone), and under `expect` whether the phone takes it: `accept` with the `answer` text, or `accept: false` with a `reason` and `update_integration`. `response_key` at the top is the answer key for the secret, in hex. |

The signatures:

```
request   X-Signature = "sha256=" + hex(HMAC-SHA256(key = secret as UTF-8, message = request body))
answer    signature   = "sha256=" + hex(HMAC-SHA256(key = k, message = answer text as UTF-8))
          k           = HMAC-SHA256(key = secret as UTF-8, message = "life-dashboard-response-v1")
```

The reasons in `answers.json`, in the order the phone checks:

| `reason` | When |
|---|---|
| `unsigned` | No signature reached the phone: no `X-Signature` on a plain answer, or a signed body without `signature`. With `update_integration: true` the plain answer's `life_dashboard` block has no `signed_body`: an integration before 1.3.0, so the app asks the user to update it. That block is unsigned and is used for nothing else. |
| `signature` | The signature does not match. For a signed body the one inside decides, whatever the header says. |
| `protocol` | `life_dashboard_signed` or `life_dashboard.writeback` is not 1. |
| `in_reply_to` | The answer names another request. |
| `issued_at` | Issued more than 10 minutes from the phone's clock. |

## Using them in the apps

Copy `exchanges.json` and `answers.json` unchanged into the app's test resources (`app/src/test/resources/protocol/` on Android, a resource folder of the test target on iOS) and note the `version` they came from. A test fails when the `version` in the file is not the one it was written for, so a copy that fell behind is noticed when it is refreshed. Then, per file:

- `exchanges.json`: for every case whose `expect.status` is not 401, the app's own HMAC over `request.body` equals `request.signature`. This pins the request signature and the UTF-8 handling.
- `answers.json`: `response_key.hex` equals the app's derived key, and for every case the app's check of `response` gives `expect`. Pass `response.headers` as they are; most cases have none, which is what Home Assistant Cloud delivers.

The apps keep building their own payloads: the vectors pin what happens to bytes, not which bytes an app writes.

## Changing them

A change to the protocol starts here: add or change a case, raise `version` in both files, and make `tests/test_vectors.py` pass. That test recomputes every signature with `hashlib` alone, runs every exchange through the webhook handler, and runs every answer through the phone's check as `tests/phone.py` writes it out. Then copy the files to the apps.

Version 1 covers the signatures in both directions, the answer, the signed body and the phone's decision on an answer. The rules for `sequence`, late payloads and what a payload does to the sensors are not in it yet.
