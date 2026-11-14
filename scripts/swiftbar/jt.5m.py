#!/usr/bin/env python3
# <xbar.title>jt</xbar.title>
# <xbar.desc>What needs you in your job search, from `jt today --json`.</xbar.desc>
# <swiftbar.refreshOnOpen>true</swiftbar.refreshOnOpen>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
"""
jt menu-bar panel for SwiftBar: the M5 test-week prototype (docs/DESIGN.md).

It reads only `jt today --json` (DESIGN D5) and writes only through
`jt action ... --json`, so it holds no job-search logic of its own.

The menu-bar title never shows company names (privacy on a shared screen).
Every menu open and click is appended to ~/.jobtrack/usage.log, which is the
evidence for the M6 go/no-go decision.

Settings (environment, or edit the defaults below):
    JT_PYTHON   python with jt's deps   (default ~/Developer/jt/.venv/bin/python)
    JT_REPO     jt checkout             (default ~/Developer/jt)
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
JT_PYTHON = os.environ.get("JT_PYTHON", str(HOME / "Developer/jt/.venv/bin/python"))
JT_REPO = os.environ.get("JT_REPO", str(HOME / "Developer/jt"))
USAGE_LOG = Path(os.environ.get("JT_USAGE_LOG", str(HOME / ".jobtrack/usage.log")))
CACHE = Path(os.environ.get("JT_PANEL_CACHE", str(HOME / ".jobtrack/panel-cache.json")))
MAX_ACTIONS = 3
SNOOZE_HOUR = "09:00"
SELF = str(Path(__file__).resolve())


# ---------------------------------------------------------------- jt calls

def jt(*args: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run([JT_PYTHON, str(Path(JT_REPO) / "jt.py"), *args],
                          capture_output=True, text=True, timeout=timeout)


def load_snapshot() -> tuple[dict | None, str | None]:
    try:
        r = jt("today", "--json")
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"could not run jt: {e}"
    if r.returncode != 0:
        lines = (r.stderr or r.stdout).strip().splitlines()
        return None, lines[-1] if lines else "jt failed"
    try:
        snap = json.loads(r.stdout)
    except ValueError:
        return None, "jt today --json returned something that isn't JSON"
    if snap.get("schema_version") != 1:
        return None, f"unsupported jt schema_version {snap.get('schema_version')!r}; update this plugin"
    return snap, None


# ---------------------------------------------------------------- usage log

def log_usage(event: str, **fields) -> None:
    try:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
        with USAGE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError:
        pass  # logging must never break the panel


# ---------------------------------------------------------------- rendering

def clean(text: object, limit: int = 60) -> str:
    """SwiftBar splits on '|' and treats newlines as new items."""
    s = " ".join(str(text or "").replace("|", "/").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def ago(iso: str | None, now: datetime) -> str:
    if not iso:
        return "never"
    then = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    mins = int((now - then).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} days ago"


def due_label(due: dict, now: datetime | None = None) -> str:
    local = now or datetime.now().astimezone()
    timed = due["kind"] != "date"
    when = datetime.fromisoformat(due["value"].replace("Z", "+00:00"))
    if timed:
        when = when.astimezone(local.tzinfo)
    offset = (when.date() - local.date()).days
    day = "Today" if offset == 0 else when.strftime("%a" if 0 < offset < 7 else "%b %-d")
    return f"{day} {when.strftime('%-I:%M %p').replace(':00 ', ' ')}" if timed else day


def read_cache() -> dict | None:
    try:
        value = json.loads(CACHE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("schema_version") == 1 else None
    except (OSError, ValueError):
        return None


def save_cache(snapshot: dict) -> None:
    temp = None
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=CACHE.parent, delete=False, mode="w", encoding="utf-8") as out:
            temp = Path(out.name)
            json.dump(snapshot, out)
        temp.replace(CACHE)
    except OSError:
        pass
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def launch_review() -> int:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".command", delete=False, encoding="utf-8") as out:
        path = Path(out.name)
        out.write("#!/bin/sh\nrm -f -- " + shlex.quote(str(path)) + "\nexec " +
                  shlex.join([JT_PYTHON, str(Path(JT_REPO) / "jt.py"), "review"]) + "\n")
    try:
        path.chmod(0o700)
        result = subprocess.run(["open", "-a", "Terminal", str(path)])
        if result.returncode == 0:
            return 0
        return result.returncode
    except OSError:
        path.unlink(missing_ok=True)
        return 1
    finally:
        if 'result' in locals() and result.returncode != 0:
            path.unlink(missing_ok=True)


SYNC_PROBLEMS = {
    "offline": "Offline: Gmail sync couldn't connect",
    "auth_expired": "Gmail sign-in expired: run jt-sync once in Terminal",
    "failed": "Last Gmail sync failed: see ~/.jobtrack/logs/jt-sync.jsonl",
}


def action(cmd: str, *params: str, refresh: bool = True) -> str:
    parts = [f'bash="{SELF}"', f"param1={cmd}"]
    parts += [f'param{i}="{p}"' for i, p in enumerate(params, 2)]
    parts += ["terminal=false", f"refresh={'true' if refresh else 'false'}"]
    return " ".join(parts)


def render(snap: dict | None, error: str | None, now: datetime | None = None) -> list[str]:
    now = now or datetime.now().astimezone()
    if snap is None:
        return ["jt ⚠", "---", f"{clean(error, 90)} | color=red", "Refresh | refresh=true",
                "Quit jt panel | " + action("quit", refresh=False)]

    cached = error is not None
    badge = snap["badge"]
    fresh = snap["freshness"]
    title = "jt"
    if badge["due"]:
        title += f" {badge['due']}"
    decisions = badge["reviews"] + badge.get("candidates", 0)
    if decisions:
        title += f" · {decisions}?"
    if fresh["sync_error"]:
        title += " ⚠"
    lines = [title, "---"]

    if cached:
        lines.append(f"Showing data from {ago(snap.get('generated_at'), now)}: couldn't read the tracker")
        lines.append("---")
    if fresh["sync_error"]:
        lines.append(f"{SYNC_PROBLEMS.get(fresh['sync_error'], 'Gmail sync problem')} | color=red")
        lines.append("---")

    failure = snap.get("failure")
    if failure and _recent(failure.get("ts"), now, FAILURE_TTL_MINUTES):
        lines.append(f"⚠ {clean(failure['message'], 100)} | color=red")
        if not cached and failure["args"][0] in RETRYABLE_CLICKS:
            lines.append("Retry | " + action(*failure["args"]))
    actions = snap["actions"]
    if actions:
        lines.append("Due | size=12")
        for a in actions[:MAX_ACTIONS]:
            mark = "⚠ overdue · " if a["overdue"] else ""
            lines.append(f"{clean(a['description'], 48)} | size=14" + (" color=gray" if cached else ""))
            lines.append(f"{mark}{clean(a['company'], 30)} · {clean(a['role'], 30)} · {due_label(a['due'], now)} | size=11 color=gray")
            if not cached:
                lines.append(f"--Done | {action('done', str(a['action_id']))}")
                lines.append(f"--Snooze until tomorrow 9 AM | {action('snooze', str(a['action_id']), tomorrow_at(SNOOZE_HOUR, now.date()))}")
            if a.get("gmail_url"):
                lines.append(f"--Open email | {action('open', a['gmail_url'], refresh=False)}")
            if a.get("reason"):
                lines.append(f"--Why: {clean(a['reason'], 70)} | color=gray")
        if len(actions) > MAX_ACTIONS:
            lines.append(f"+{len(actions) - MAX_ACTIONS} more: run `jt today` | color=gray")
    else:
        freshness = ("sync needs attention" if fresh["sync_error"] else
                     f"synced {ago(fresh['gmail_synced_at'], now)}")
        lines.append(f"Nothing due · {freshness} | color=gray")

    review = snap["review"]
    if review["pending"]:
        lines.append("---")
        lines.append(f"Needs your decision ({review['pending']}) | size=12")
        items = review.get("items") or []
        for it in items[:MAX_REVIEW_ITEMS]:
            title = clean(it.get("subject") or "(no subject)", 48)
            lines.append(f"{title} | size=14 refresh=true")
            lines.append(f"{clean(it.get('from') or '-', 30)} · {clean(it['summary'], 70)} | size=11 color=gray")
            if not cached:
                for c in it.get("choices", []):
                    if c.get("new_app"):
                        lines.append(f"--{clean(c['label'], 70)}")
                        for lane in c.get("lanes") or []:
                            lines.append(f"----{LANE_NAMES.get(lane, lane)} | "
                                         + action("add-app", str(it["review_id"]), lane))
                        continue
                    stage = [c["stage"]] if c.get("stage") else []
                    lines.append(f"--✓ {clean(c['label'], 70)} | "
                                 + action("approve", str(it["review_id"]), str(c["app_id"]), *stage))
                lines.append("--Dismiss: not needed | " + action("dismiss-review", str(it["review_id"])))
            if it.get("gmail_url"):
                lines.append(f"--Open email | {action('open', it['gmail_url'], refresh=False)}")
        if len(items) > MAX_REVIEW_ITEMS or review["pending"] > len(items):
            more = review["pending"] - min(len(items), MAX_REVIEW_ITEMS)
            lines.append(f"+{more} more in Terminal | {action('review', refresh=False)}")

    found = snap.get("candidates") or []
    if found:
        lines.append("---")
        lines.append(f"New applications found ({len(found)}) | size=12")
        for c in found[:MAX_REVIEW_ITEMS]:
            who = " · ".join(v for v in (c.get("company"), c.get("role")) if v) or "Unknown company"
            lines.append(f"{clean(who, 60)} | size=14 refresh=true")
            lines.append(f"found in your email · {clean(c.get('applied_on') or '-', 12)} | size=11 color=gray")
            if not cached:
                cid = str(c["candidate_id"])
                if c.get("can_add"):
                    lines.append("--Add as an application")
                    for lane, name in LANE_NAMES.items():
                        lines.append(f"----{name} | " + action("accept-candidate", cid, lane))
                else:
                    lines.append("--Missing company or role: add it with jt candidates | color=gray")
                lines.append("--Not mine | " + action("reject-candidate", cid))

    if snap["suggestions"]:
        lines.append("---")
        lines.append("Suggestions | size=12")
        for s in snap["suggestions"][:MAX_ACTIONS]:
            if s["kind"] == "CLOSE OUT":
                lines.append(f"Close out?: {clean(s['company'], 30)} · {clean(s.get('role'), 30)} | size=13 refresh=true")
                lines.append(f"--{clean(s['reason'], 70)} | color=gray")
                if not cached:
                    app_id = str(s["application_id"])
                    for c in s.get("close_choices") or []:
                        lines.append(f"--{clean(c['label'], 40)} | " + action("close", app_id, c["kind"]))
                    lines.append("--Keep it open (hide 14 days) | " + action("dismiss-suggestion", app_id, s["kind"]))
                continue
            lines.append(f"{clean(s['kind'].title(), 12)}: {clean(s['company'], 30)} · {clean(s.get('role'), 30)} · optional | size=13 refresh=true")
            lines.append(f"--{clean(s['reason'], 70)} | color=gray")
            if not cached:
                app_id = str(s["application_id"])
                lines.append("--Remind me tomorrow | " + action(
                    "remind", app_id, s["kind"], (now.date() + timedelta(days=1)).isoformat()))
                lines.append("--Dismiss | " + action("dismiss-suggestion", app_id, s["kind"]))

        extra = len(snap["suggestions"]) - MAX_ACTIONS
        if extra > 0:
            lines.append(f"+{extra} more to close out or follow up (jt stale) | color=gray")

    lines.append("---")
    lines.append(f"Gmail synced {ago(fresh['gmail_synced_at'], now)} · "
                 f"classified {ago(fresh['classified_at'], now)} | size=11 color=gray")
    version = snap.get("version") or {}
    if version:
        lines.append(f"jt {clean(version.get('commit'), 12)} · schema {version.get('schema')}"
                     " | size=10 color=gray")
    lines.append("Refresh | refresh=true")
    lines.append("Quit jt panel | " + action("quit", refresh=False))
    return lines


# ---------------------------------------------------------------- clicks

FAILURE_TTL_MINUTES = 15
RETRYABLE_CLICKS = {"done", "snooze", "remind", "dismiss-suggestion"}
MAX_REVIEW_ITEMS = 8
LANE_NAMES = {"swe": "Software (swe)", "ops": "Ops", "comms": "Comms"}


def _recent(ts: str | None, now: datetime, minutes: int) -> bool:
    """True if ts is within `minutes` of now. Unknown age counts as old, so a
    banner without a timestamp can never stick around forever."""
    if not ts:
        return False
    try:
        then = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return timedelta(0) <= now - then <= timedelta(minutes=minutes)


def failure_message(snapshot: dict, cmd: str, rest: list[str], error: str | None) -> str:
    detail = f": {error}" if error else ". Try again"
    if cmd == "done":
        row: dict = next((a for a in snapshot.get("actions", []) if str(a["action_id"]) == rest[0]), {})
        return f"Couldn't mark '{clean(row.get('description') or 'that task', 40)}' done{detail}"
    if cmd == "approve":
        return f"Couldn't record that update{detail}"
    if cmd == "dismiss-review":
        return f"Couldn't dismiss that review item{detail}"
    return f"Couldn't do that{detail}"


def tomorrow_at(hhmm: str, today: date | None = None) -> str:
    return f"{((today or date.today()) + timedelta(days=1)).isoformat()} {hhmm}"


CLICK_ARITY = {"done": 1, "snooze": 1, "remind": 3, "dismiss-suggestion": 2, "close": 2,
               "accept-candidate": 2, "reject-candidate": 1, "add-app": 2, "approve": 2,
               "dismiss-review": 1, "open": 1}


def handle_click(argv: list[str]) -> int:
    cmd, rest = argv[0], argv[1:]
    if len(rest) < CLICK_ARITY.get(cmd, 0):
        log_usage("click_failed", action=cmd, detail="missing arguments")
        return 2
    log_usage("click", action=cmd, target=rest[0] if rest and cmd != "open" else None)
    if cmd == "review":
        return launch_review()
    if cmd == "open":
        return subprocess.run(["open", rest[0]]).returncode
    if cmd == "quit":
        # No AppleScript: it needs the "control another app" permission. A plain
        # quit signal needs none. This quits SwiftBar, which hosts the panel.
        return subprocess.run(["killall", "SwiftBar"]).returncode
    if cmd == "done":
        args = ["action", "done", rest[0]]
    elif cmd == "snooze":
        if len(rest) == 1:
            rest.append(tomorrow_at(SNOOZE_HOUR))
        args = ["action", "snooze", rest[0], "--until", rest[1]]
    elif cmd == "remind":
        args = ["action", "add", rest[0], rest[1].capitalize(), "--due", rest[2],
                "--reason", "Scheduled from SwiftBar suggestion"]
    elif cmd == "dismiss-suggestion":
        args = ["action", cmd, *rest]
    elif cmd == "add-app":
        args = ["review", "add-app", rest[0], "--lane", rest[1]]
    elif cmd == "close":
        args = ["log", rest[0], rest[1], "--note", "closed from the menu bar"]
    elif cmd == "accept-candidate":
        args = ["candidates", "accept", rest[0], "--lane", rest[1]]
    elif cmd == "reject-candidate":
        args = ["candidates", "reject", rest[0], "--note", "not mine (menu bar)"]
    elif cmd == "approve":
        args = ["review", "approve", rest[0], "--app-id", rest[1]]
        if len(rest) > 2:
            args += ["--expected-stage", rest[2]]
    elif cmd == "dismiss-review":
        args = ["review", "dismiss", rest[0], "--note", "dismissed from the menu bar"]
    else:
        return 2
    reply: dict = {}
    try:
        result = jt(*args, "--json")
        reply = json.loads(result.stdout)
        ok = (result.returncode == 0 and reply.get("ok") is True) or (
            cmd == "done" and reply.get("error") == f"action #{rest[0]} is already done"
        )
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        ok = False
    snapshot = read_cache()
    if snapshot is not None:
        if ok:
            snapshot.pop("failure", None)
        else:
            snapshot["failure"] = {"message": failure_message(snapshot, cmd, rest, reply.get("error")),
                                   "args": [cmd, *rest],
                                   "ts": datetime.now(timezone.utc).isoformat()}
        save_cache(snapshot)
    if not ok:
        log_usage("click_failed", action=cmd)
    return 0 if ok else 1


def main() -> None:
    if len(sys.argv) > 1:
        sys.exit(handle_click(sys.argv[1:]))
    if os.environ.get("SWIFTBAR_PLUGIN_REFRESH_REASON") == "MenuOpen":
        log_usage("open")
    snap, error = load_snapshot()
    if snap is not None:
        previous = read_cache() or {}
        if "failure" in previous:
            snap["failure"] = previous["failure"]
        save_cache(snap)
    else:
        snap = read_cache()
    print("\n".join(render(snap, error)))


if __name__ == "__main__":
    main()
