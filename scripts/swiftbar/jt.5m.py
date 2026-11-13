#!/usr/bin/env python3
# <xbar.title>jt</xbar.title>
# <xbar.desc>What needs you in your job search, from `jt today --json`.</xbar.desc>
# <swiftbar.refreshOnOpen>true</swiftbar.refreshOnOpen>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>
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
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
JT_PYTHON = os.environ.get("JT_PYTHON", str(HOME / "Developer/jt/.venv/bin/python"))
JT_REPO = os.environ.get("JT_REPO", str(HOME / "Developer/jt"))
USAGE_LOG = Path(os.environ.get("JT_USAGE_LOG", str(HOME / ".jobtrack/usage.log")))
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


def due_label(due: dict) -> str:
    if due["kind"] == "date":
        d = date.fromisoformat(due["value"])
        today = date.today()
        if d == today:
            return "today"
        return d.strftime("%b %-d")
    when = datetime.strptime(due["value"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return when.astimezone().strftime("%-I:%M %p")


SYNC_PROBLEMS = {
    "offline": "Offline: Gmail sync couldn't connect",
    "auth_expired": "Gmail sign-in expired: run jt-sync once in Terminal",
    "failed": "Last Gmail sync failed: see ~/.jobtrack/logs/jt-sync.jsonl",
}


def action(cmd: str, *params: str, refresh: bool = True) -> str:
    parts = [f"bash={SELF}", f"param1={cmd}"]
    parts += [f'param{i}="{p}"' for i, p in enumerate(params, 2)]
    parts += ["terminal=false", f"refresh={'true' if refresh else 'false'}"]
    return " ".join(parts)


def render(snap: dict | None, error: str | None, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    if snap is None:
        return ["jt ⚠", "---", f"{clean(error, 90)} | color=red", "Refresh | refresh=true"]

    badge = snap["badge"]
    fresh = snap["freshness"]
    title = "jt"
    if badge["due"]:
        title += f" {badge['due']}"
    if badge["reviews"]:
        title += f" · {badge['reviews']}?"
    if fresh["sync_error"]:
        title += " ⚠"
    lines = [title, "---"]

    if fresh["sync_error"]:
        lines.append(f"{SYNC_PROBLEMS.get(fresh['sync_error'], 'Gmail sync problem')} | color=red")
        lines.append("---")

    actions = snap["actions"]
    if actions:
        lines.append("Due | size=12")
        for a in actions[:MAX_ACTIONS]:
            mark = "⚠ overdue · " if a["overdue"] else ""
            lines.append(f"{clean(a['description'], 48)} | size=14")
            lines.append(f"{mark}{clean(a['company'], 30)} · {due_label(a['due'])} | size=11 color=gray")
            lines.append(f"--Done | {action('done', str(a['action_id']))}")
            lines.append(f"--Snooze until tomorrow {SNOOZE_HOUR} | {action('snooze', str(a['action_id']))}")
            if a.get("gmail_url"):
                lines.append(f"--Open email | {action('open', a['gmail_url'], refresh=False)}")
            if a.get("reason"):
                lines.append(f"--Why: {clean(a['reason'], 70)} | color=gray")
        if len(actions) > MAX_ACTIONS:
            lines.append(f"+{len(actions) - MAX_ACTIONS} more: run `jt today` | color=gray")
    else:
        lines.append("Nothing due | color=gray")

    review = snap["review"]
    if review["pending"]:
        lines.append("---")
        top = review["top"]["reason"] if review["top"] else ""
        lines.append(f"{review['pending']} to review | {action('review', refresh=False)}")
        if top:
            lines.append(f"Oldest: {clean(top, 70)} | size=11 color=gray")

    if snap["suggestions"]:
        lines.append("---")
        lines.append("Suggestions | size=12")
        for s in snap["suggestions"][:MAX_ACTIONS]:
            lines.append(f"{clean(s['kind'].title(), 12)}: {clean(s['company'], 30)} | size=13")
            lines.append(f"--{clean(s['reason'], 70)} | color=gray")

    lines.append("---")
    lines.append(f"Gmail synced {ago(fresh['gmail_synced_at'], now)} · "
                 f"classified {ago(fresh['classified_at'], now)} | size=11 color=gray")
    lines.append("Refresh | refresh=true")
    return lines


# ---------------------------------------------------------------- clicks

def tomorrow_at(hhmm: str, today: date | None = None) -> str:
    return f"{((today or date.today()) + timedelta(days=1)).isoformat()} {hhmm}"


def handle_click(argv: list[str]) -> int:
    cmd, rest = argv[0], argv[1:]
    log_usage("click", action=cmd, target=rest[0] if rest and cmd != "open" else None)
    if cmd == "done":
        r = jt("action", "done", rest[0], "--json")
    elif cmd == "snooze":
        r = jt("action", "snooze", rest[0], "--until", tomorrow_at(SNOOZE_HOUR), "--json")
    elif cmd == "open":
        return subprocess.run(["open", rest[0]]).returncode
    elif cmd == "review":
        script = f'tell application "Terminal" to do script "cd {JT_REPO} && {JT_PYTHON} jt.py review"'
        return subprocess.run(["osascript", "-e", script]).returncode
    else:
        return 2
    try:
        ok = json.loads(r.stdout).get("ok", False)
    except ValueError:
        ok = False
    if not ok:
        log_usage("click_failed", action=cmd, detail=clean(r.stdout or r.stderr, 120))
    return 0 if ok else 1


def main() -> None:
    if len(sys.argv) > 1:
        sys.exit(handle_click(sys.argv[1:]))
    if os.environ.get("SWIFTBAR_PLUGIN_REFRESH_REASON") == "MenuOpen":
        log_usage("open")
    snap, error = load_snapshot()
    print("\n".join(render(snap, error)))


if __name__ == "__main__":
    main()
