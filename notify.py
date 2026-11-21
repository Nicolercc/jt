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
# iMessage to yourself (CEO review D3). Same rule: recipient and text are
# arguments, never part of the script.
OSASCRIPT_IMESSAGE = ("on run argv\n"
                      "  tell application \"Messages\"\n"
                      "    set svc to 1st account whose service type = iMessage\n"
                      "    send (item 2 of argv) to participant (item 1 of argv) of svc\n"
                      "  end tell\n"
                      "end run")


def phone_address() -> str | None:
    """Your own iMessage phone number or Apple ID email, from JT_PHONE in
    ~/.jobtrack/jt.env. Unset means alerts stay on the Mac."""
    value = (os.environ.get("JT_PHONE") or "").strip()
    return value if value and "\n" not in value else None


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
        alert = {"title": "jt can't read your Gmail",
                 "message": SYNC_ALERTS.get(err, SYNC_ALERTS["failed"])}
        if stale and key not in sent:
            out.append({"key": key, "channel": "mac", **alert})
        # The outage alert is the one that must reach you away from the Mac.
        # It names no company, so it's private as is.
        if stale and phone_address() and f"phone:{key}" not in sent:
            out.append({"key": f"phone:{key}", "channel": "phone", **alert})

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


def _run_osascript(script: str, *args: str) -> str | None:
    """Run a fixed AppleScript with arguments. None means it succeeded;
    otherwise a short reason, so a failed send is never mistaken for a sent one."""
    try:
        proc = subprocess.run(["osascript", "-e", script, *args],
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


def send_notification(title: str, message: str) -> str | None:
    """Show one macOS notification banner."""
    return _run_osascript(OSASCRIPT_NOTIFY, title, message)


def send_imessage(to: str, text: str) -> str | None:
    """Send one iMessage from this Mac's Messages account."""
    return _run_osascript(OSASCRIPT_IMESSAGE, to, text)


CHANNELS = ("mac", "phone")
ALERTS_FAILING_HELP = {
    "mac": ("Alerts aren't reaching you: check System Settings → Notifications "
            "→ Script Editor → Allow"),
    "phone": ("Phone alerts aren't reaching you: check that Messages is signed in, and "
              "allow controlling Messages in System Settings → Privacy & Security → Automation"),
}


def record_delivery(conn, delivered: list[dict], errors: dict[str, str]) -> None:
    """Record what was delivered, in one transaction, and track each channel's
    health on its own: a run where a channel delivered nothing and failed
    counts toward its warning; any delivery on that channel resets it."""
    worked = {n.get("channel", "mac") for n in delivered}
    with jt.write_txn(conn):
        _record_notified_in_txn(conn, delivered, jt._utc_iso(datetime.now(timezone.utc)))
        for channel in CHANNELS:
            runs_key, error_key = f"notify_failed_runs:{channel}", f"notify_last_error:{channel}"
            if channel in worked:
                conn.execute("DELETE FROM sync_state WHERE key IN (?, ?)", (runs_key, error_key))
            elif channel in errors:
                runs = int(jt._fresh(conn, runs_key) or 0) + 1
                conn.executemany("INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
                                 [(runs_key, str(runs)), (error_key, errors[channel])])


def alerts_failing(conn) -> dict | None:
    """The first channel that has delivered nothing for 3 runs in a row."""
    for channel in CHANNELS:
        runs = int(jt._fresh(conn, f"notify_failed_runs:{channel}") or 0)
        if runs >= ALERTS_FAILING_AFTER_RUNS:
            return {"channel": channel, "failed_runs": runs,
                    "last_error": jt._fresh(conn, f"notify_last_error:{channel}")}
    return None


def alerts_failing_help(failing: dict) -> str:
    return ALERTS_FAILING_HELP.get(failing.get("channel", "mac"), ALERTS_FAILING_HELP["mac"])


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
    delivered: list[dict] = []
    errors: dict[str, str] = {}
    failed = 0
    for n in items:
        channel = n.get("channel", "mac")
        if channel == "phone":
            to = phone_address()
            error = (send_imessage(to, f"{n['title']}: {n['message']}") if to
                     else "JT_PHONE is not set")
        else:
            error = send_notification(n["title"], n["message"])
        if error is None:
            delivered.append(n)
        else:
            errors[channel] = error
            failed += 1
    record_delivery(conn, delivered, errors)
    print(f"sent {len(delivered)} notification(s)")
    if errors:
        detail = "; ".join(f"{c}: {e}" for c, e in errors.items())
        print(f"error: {failed} notification(s) not delivered ({detail}); "
              "they will be retried on the next run", file=sys.stderr)
        sys.exit(1)
