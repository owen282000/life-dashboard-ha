# Contributing

Thanks for your interest in improving Life Dashboard for Home Assistant.

## Getting started

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
```

The tests run against the Home Assistant version pinned in `requirements_test.txt`
through `pytest-homeassistant-custom-component`; nothing needs a running Home Assistant.
For a live check, `scripts/sync-to-dev-ha.sh` copies the integration into a Home Assistant
in Docker and restarts it (see the script for the two variables it takes).

## Development guidelines

- **Run the tests and ruff** before opening a PR:

  ```sh
  .venv/bin/python -m pytest
  .venv/bin/ruff check custom_components tests
  .venv/bin/ruff format --check custom_components tests
  ```

  CI also runs hassfest and the HACS validation, so a manifest key out of order or HTML in
  `strings.json` fails there.
- **The payload format belongs to the apps.** What this integration accepts is documented
  in the Android app's [webhook reference](https://github.com/owen282000/life-dashboard-companion-app/blob/main/docs/webhook.md)
  and shared with the iOS app. Parse defensively, never require a field the apps mark
  optional, and never send anything back the app does not expect.
- **User-facing text lives in `strings.json`**, mirrored in `translations/en.json`; the
  two files stay identical. Translations are welcome as `translations/<locale>.json`.
- **Keep pure logic free of Home Assistant imports.** `payload.py`, `history.py` and
  `pairing.py` are plain Python with plain tests; the Home Assistant glue sits in
  `__init__.py`, `config_flow.py`, `sensor.py` and `statistics.py`. Follow that split.
- **Commit messages** follow the style in the history: `feat:`, `fix:`, `docs:`, `ci:`,
  `build:`, `test:`, `refactor:`, with a subject that says what changed for a user.

## Releasing

Versions are strict semver (`X.Y.Z`) and live in three places that must agree: the
`version` in `manifest.json`, the section in `CHANGELOG.md`, and the git tag, which is the
version without a prefix. A GitHub release on the tag is what HACS offers to users.

## Opening a pull request

1. Create a feature branch
2. Make your changes, with tests where it makes sense
3. Make sure the tests and ruff pass
4. Open a PR describing what changed and why

Small, focused PRs are much easier to review than big ones. When in doubt, open an issue
first to discuss the direction.
