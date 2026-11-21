"""Notifications: what jt alerts you about, and sending them through macOS.

Moved out of jt.py unchanged (2026-11-21) so jt.py can shrink; see README
§Scope. Tracker logic stays in jt; this module only decides which alerts are
due and shows them.
"""

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import jt


# --------------------------------------------------------------------------
# Notifications (audit 2026-11-13, P0). Gmail sync was silently dead for six
# days, and deadlines only showed if you opened the menu. Each alert is sent
# once (deduplicated in sync_state under "notified:<key>").
# --------------------------------------------------------------------------

NOTIFY_SYNC_AFTER = timedelta(hours=2)
SYNC_ALERTS = {
    "auth_expired": "Your Google sign-in expired, so new job emails aren't arriving. "
                    "Sign in again: cd ~/Developer/jt && .venv/bin/python sync.py",
    "offline": "Gmail sync can't connect. It will retry every hour.",
    "failed": "Gmail sync is failing. Details: ~/.jobtrack/logs/jt-sync.jsonl",
}
# Text goes in as arguments, never pasted into the script, so a subject line
# can't inject AppleScript.
OSASCRIPT_NOTIFY = ("on run argv\n"
                    "  display notification (item 2 of argv) with title (item 1 of argv)\n"
                    "end run")


def _notify_now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def notification_plan(conn, now: datetime) -> list[dict]:
    """Alerts that are due and not yet sent: sync trouble (once a day, only
    after 2 hours without a good sync), tasks due today or overdue (once per
    task per due day), and newly found applications (once each)."""
    tz = now.tzinfo if isinstance(now.tzinfo, ZoneInfo) else jt.viewer_tz()
    private = os.environ.get("JT_NOTIFY_PRIVATE") == "1"
    sent = {r["key"].removeprefix("notified:") for r in
            conn.execute("SELECT key FROM sync_state WHERE key LIKE 'notified:%'")}
    out: list[dict] = []

    err = jt.sync_error_state()
    if err:
        last = jt._fresh(conn, "gmail_synced_at")
        stale = last is None or now - jt._from_utc_iso(last) >= NOTIFY_SYNC_AFTER
        key = f"sync:{now.date().isoformat()}"
        if stale and key not in sent:
            out.append({"key": key, "title": "jt can't read your Gmail",
                        "message": SYNC_ALERTS.get(err, SYNC_ALERTS["failed"])})

    for x in jt.today_entries(conn, now.date(), now, tz)["actions"]:
        r = x["row"]
        key = f"action:{r['id']}:{x['due_day'].isoformat()}"
        if key in sent:
            continue
        label = "Overdue" if x["overdue"] else "Due today"
        if private:
            title, message = f"A task is {label.lower()}", "Open the jt menu."
        else:
            local = jt.action_due_local(r, tz)
            when = f" · {local.strftime('%-I:%M %p')}" if local else ""
            title, message = f"{label}: {r['description']}", f"{r['company']} · {r['role']}{when}"
        out.append({"key": key, "title": title, "message": message})

    for c in conn.execute("""SELECT id, company, role FROM application_candidates
                             WHERE status = 'pending' ORDER BY id"""):
        key = f"candidate:{c['id']}"
        if key in sent:
            continue
        who = " · ".join(v for v in (c["company"], c["role"]) if v)
        message = ("Open the jt menu to add it." if private or not who
                   else f"{who}. Open the jt menu to add it.")
        out.append({"key": key, "title": "New application found", "message": message})
    return out


def record_notified(conn, items: list[dict]) -> None:
    stamp = jt._utc_iso(datetime.now(timezone.utc))
    with jt.write_txn(conn):
        for n in items:
            conn.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
                         (f"notified:{n['key']}", stamp))


def send_notification(title: str, message: str) -> None:
    subprocess.run(["osascript", "-e", OSASCRIPT_NOTIFY, title, message],
                   capture_output=True, timeout=10, check=False)


def cmd_notify(conn, a):
    tz = jt.viewer_tz()
    items = notification_plan(conn, _notify_now(tz))
    if a.dry_run:
        if a.json:
            print(json.dumps(items, indent=2))
        else:
            for n in items:
                print(f"would notify: {n['title']}: {n['message']}")
            if not items:
                print("nothing to notify")
        return
    if os.environ.get("JT_NOTIFY") == "0":
        print("notifications are off (JT_NOTIFY=0)")
        return
    for n in items:
        send_notification(n["title"], n["message"])
    record_notified(conn, items)
    print(f"sent {len(items)} notification(s)")
