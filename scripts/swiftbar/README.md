# jt menu-bar panel (SwiftBar prototype)

The M5 test-week prototype from `docs/DESIGN.md`. It reads `jt today --json`
and writes through `jt action ... --json`; it has no job-search logic of its own.

## What it shows

- **Menu-bar title:** `jt`, then the number of due actions, `· N?` for pending
  reviews, and `⚠` if the last Gmail sync failed. Never company names.
- **Due:** up to 3 actions, each with **Done**, **Snooze until tomorrow 09:00**,
  **Open email** (when the action came from an email), and why it exists.
- **Reviews:** how many are waiting, and the oldest reason. Click to open
  `jt review` in Terminal.
- **Suggestions:** up to 3 follow-up nudges.
- **Freshness:** when Gmail last synced and the classifier last ran.

## Install (one time)

```bash
brew install --cask swiftbar
```

Open SwiftBar and choose a plugin folder (for example `~/SwiftBar`), then:

```bash
ln -s ~/Developer/jt/scripts/swiftbar/jt.5m.py ~/SwiftBar/jt.5m.py
```

`5m` in the name means it refreshes every 5 minutes; it also refreshes
whenever you open the menu. It uses `~/Developer/jt` and its `.venv` by
default; set `JT_REPO` / `JT_PYTHON` to change that.

## The test week

Every menu open and click is appended to `~/.jobtrack/usage.log` (JSON lines):
`open`, `click` (done / snooze / open / review), and `click_failed`. After a
week, that log answers the M6 question: did you reach for it, and did you act
from it? Nothing in the log leaves your Mac.
