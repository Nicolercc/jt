"""Notifications: what jt alerts you about, and sending them through macOS.

Moved out of jt.py unchanged (2026-11-21) so jt.py can shrink; see README
§Scope. Tracker logic stays in jt; this module only decides which alerts are
due and shows them.
"""

import json
import os
import subprocess
import sys
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
        _record_notified_in_txn(conn, items, stamp)


def _record_notified_in_txn(conn, items: list[dict], stamp: str) -> None:
    for n in items:
        conn.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
                     (f"notified:{n['key']}", stamp))


NOTIFY_TIMEOUT_SECONDS = 10
# Consecutive `jt notify` runs in which nothing could be delivered before the
# menu and `jt today` say alerts aren't reaching you (CEO review F2).
ALERTS_FAILING_AFTER_RUNS = 3
ALERTS_FAILING_HELP = ("Alerts aren't reaching you: check System Settings → Notifications "
                       "→ Script Editor → Allow")


def send_notification(title: str, message: str) -> str | None:
    """Show one macOS notification. Returns None when osascript accepted it,
    otherwise a short reason, so a blocked alert is never mistaken for a sent one."""
    try:
        proc = subprocess.run(["osascript", "-e", OSASCRIPT_NOTIFY, title, message],
                              capture_output=True, text=True,
                              timeout=NOTIFY_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return f"osascript timed out after {NOTIFY_TIMEOUT_SECONDS}s"
    except FileNotFoundError as e:
        return f"osascript not found: {e}"
    except OSError as e:
        return f"osascript failed to start: {e}"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return f"osascript exited {proc.returncode}" + (f": {detail[-1]}" if detail else "")
    return None


def record_delivery(conn, delivered: list[dict], last_error: str | None) -> None:
    """Record what reached the screen, in one transaction. A run where nothing
    could be delivered counts toward the failing-alerts warning; any delivery
    resets it."""
    with jt.write_txn(conn):
        _record_notified_in_txn(conn, delivered, jt._utc_iso(datetime.now(timezone.utc)))
        if delivered:
            conn.execute("DELETE FROM sync_state WHERE key IN "
                         "('notify_failed_runs', 'notify_last_error')")
        elif last_error:
            runs = int(jt._fresh(conn, "notify_failed_runs") or 0) + 1
            conn.executemany("INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
                             [("notify_failed_runs", str(runs)),
                              ("notify_last_error", last_error)])


def alerts_failing(conn) -> dict | None:
    runs = int(jt._fresh(conn, "notify_failed_runs") or 0)
    if runs < ALERTS_FAILING_AFTER_RUNS:
        return None
    return {"failed_runs": runs, "last_error": jt._fresh(conn, "notify_last_error")}


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
    delivered, errors = [], []
    for n in items:
        error = send_notification(n["title"], n["message"])
        if error is None:
            delivered.append(n)
        else:
            errors.append(error)
    record_delivery(conn, delivered, errors[-1] if errors else None)
    print(f"sent {len(delivered)} notification(s)")
    if errors:
        print(f"error: {len(errors)} notification(s) not delivered ({errors[-1]}); "
              "they will be retried on the next run", file=sys.stderr)
        sys.exit(1)
