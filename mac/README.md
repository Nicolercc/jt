# Today menu-bar panel

Requires macOS 13 or later, Swift command-line tools, and Python 3.11 or later.
From the repository root:

```sh
swiftc -parse-as-library mac/JToday.swift -o /tmp/jt-today
demo_dir="$(mktemp -d)"
export JT_DB="$demo_dir/jobs.db"
export JT_REPO="$PWD"
export JT_PYTHON="$(command -v python3)"
"$JT_PYTHON" jt.py add "Northstar Labs" "Frontend Engineer" --lane swe --on today
"$JT_PYTHON" jt.py action add 1 "Send interview availability" --due today
/tmp/jt-today
```

Open the tray icon and confirm the action is shown. Mark it handled and
check that it disappears, then run `"$JT_PYTHON" jt.py action show 1 --json`
in another terminal with the same `JT_DB`. Expect `state: done`. Quit the
panel before removing the disposable database.

This branch is based on the action lookup from PR #18. The panel keeps the
last loaded snapshot when refresh fails. After an ambiguous completion
result, refresh checks the specific action before enabling another attempt.
Verify keyboard navigation, VoiceOver, menu-bar placement, Gmail links,
and refresh failure on a Mac before merging.
