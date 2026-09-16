## What does this PR do?

<!-- A short description of the change and the motivation behind it. -->

## Checklist

- [ ] Tests pass (`.venv/bin/python -m pytest`) and ruff is clean
      (`ruff check custom_components tests && ruff format --check custom_components tests`)
- [ ] User-facing text lives in `strings.json` and `translations/en.json`, which stay
      identical, not in code
- [ ] An entry under `## [Unreleased]` in `CHANGELOG.md`, unless this changes nothing a
      user would notice
- [ ] Payload parsing still accepts every field documented in the app's
      [webhook reference](https://github.com/owen282000/life-dashboard-companion-app/blob/main/docs/webhook.md);
      the format is shared with the iOS app
- [ ] No secrets or health data in logs
- [ ] README updated if behaviour or configuration changed
