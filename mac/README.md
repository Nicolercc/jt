# Today menu-bar panel

Requires macOS 13 or later, Swift command-line tools, and Python 3.11 or later.
From the repository root:

```sh
swiftc -parse-as-library mac/*.swift -o /tmp/jt-today
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

## Review walkthrough

With the same disposable database, add a second Northstar Labs application
and seed a synthetic proposal before starting the panel:

```sh
"$JT_PYTHON" jt.py add "Northstar Labs" "Full Stack Engineer" --lane swe --on today
"$JT_PYTHON" - <<'PY'
import json
from datetime import date
import jt

with jt.connect() as conn:
    conn.execute(
        "INSERT INTO raw_messages (gmail_msg_id, subject, body) VALUES (?, ?, ?)",
        ("demo", "Recruiter screen", "Let's schedule a call for the Frontend Engineer role."),
    )
    conn.execute(
        "INSERT INTO review_queue (gmail_msg_id, proposed_json, reason) VALUES (?, ?, ?)",
        ("demo", json.dumps(dict(company="Northstar Labs", kind="screen",
                                 occurred_on=date.today().isoformat(),
                                 evidence="Frontend Engineer role")), "ambiguous role"),
    )
PY
```

Open Review update. Confirm starts disabled. Read the evidence, select
Frontend Engineer, then try Leave for later. Reopening should show both roles
with no selection. Confirm Frontend Engineer and check that the result names
the role and that only one event was recorded. Repeat with VoiceOver and
keyboard navigation. The synthetic source ID does not point to a live Gmail
message.
