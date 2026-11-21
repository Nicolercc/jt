"""PRD P0 (audit 2026-11-13): Gmail sync was silently dead for 6 days, and a
deadline only showed if you opened the menu. `jt notify` sends each alert once."""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import jt
import notify

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=NY)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("JT_TZ", "America/New_York")
    monkeypatch.setenv("JT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("JT_NOTIFY", raising=False)
    monkeypatch.delenv("JT_NOTIFY_PRIVATE", raising=False)
    monkeypatch.delenv("JT_PHONE", raising=False)
    c = jt.connect()
    with jt.write_txn(c):
        c.execute("INSERT INTO applications (id, company, role, lane, applied_on) "
                  "VALUES (10, 'Northstar', 'Support Agent', 'ops', '2026-08-28')")
    yield c
    c.close()


def sync_failed(tmp_path, error="error: Gmail token refresh failed with invalid_grant"):
    p = tmp_path / "logs" / "jt-sync.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ts": "2026-11-14T13:00:00+00:00", "event": "failure",
                             "error_type": "SystemExit", "error": error}) + "\n")


def plan(conn):
    return notify.notification_plan(conn, NOW)


# ---------------------------------------------------------------- sync health

def test_sync_failure_for_hours_alerts_once_a_day(conn, tmp_path):
    sync_failed(tmp_path)
    jt.mark_fresh(conn, "gmail_synced_at", NOW - timedelta(hours=5))
    [n] = plan(conn)
    assert n["title"] == "jt can't read your Gmail"
    assert "sign-in expired" in n["message"]
    notify.record_notified(conn, [n])
    assert plan(conn) == []


def test_brief_sync_failure_does_not_alert(conn, tmp_path):
    sync_failed(tmp_path)
    jt.mark_fresh(conn, "gmail_synced_at", NOW - timedelta(minutes=40))
    assert plan(conn) == []


def test_never_synced_and_failing_alerts(conn, tmp_path):
    sync_failed(tmp_path)
    assert [n["title"] for n in plan(conn)] == ["jt can't read your Gmail"]


# ---------------------------------------------------------------- deadlines

def test_task_due_today_alerts_once(conn):
    jt.add_action(conn, 10, "Complete Northstar assessment", due_date="2026-11-14")
    [n] = plan(conn)
    assert n["title"] == "Due today: Complete Northstar assessment"
    assert n["message"] == "Northstar · Support Agent"
    notify.record_notified(conn, [n])
    assert plan(conn) == []


def test_overdue_task_alerts_and_future_task_does_not(conn):
    jt.add_action(conn, 10, "Send availability", due_date="2026-11-12")
    jt.add_action(conn, 10, "Prepare for onsite", due_date="2026-11-20")
    [n] = plan(conn)
    assert n["title"] == "Overdue: Send availability"


def test_snoozed_or_done_tasks_do_not_alert(conn):
    a = jt.add_action(conn, 10, "A", due_date="2026-11-14")
    b = jt.add_action(conn, 10, "B", due_date="2026-11-14")
    jt.set_action_state(conn, a, "done")
    jt.set_action_state(conn, b, "snoozed", snoozed_until=NOW + timedelta(days=1))
    assert plan(conn) == []


def test_timed_task_shows_its_time(conn):
    jt.add_action(conn, 10, "Interview", due_local=datetime(2026, 11, 14, 14, 0), tz="America/New_York")
    [n] = plan(conn)
    assert n["title"] == "Due today: Interview" and n["message"] == "Northstar · Support Agent · 2:00 PM"


# ---------------------------------------------------------------- new applications

def test_new_application_found_alerts_once(conn):
    jt.insert_application_candidate(conn, raw_input="{}", source="classifier",
                                    source_ref="gmail:x1", company="Harbor Bank", role="Associate II")
    [n] = plan(conn)
    assert n["title"] == "New application found"
    assert n["message"] == "Harbor Bank · Associate II. Open the jt menu to add it."
    notify.record_notified(conn, [n])
    assert plan(conn) == []


# ---------------------------------------------------------------- delivery

def test_private_mode_hides_company_names(conn, monkeypatch):
    monkeypatch.setenv("JT_NOTIFY_PRIVATE", "1")
    jt.add_action(conn, 10, "Complete Northstar assessment", due_date="2026-11-14")
    [n] = plan(conn)
    assert "Northstar" not in n["title"] + n["message"]
    assert n["title"] == "A task is due today"


def test_notification_text_is_passed_as_arguments_not_code(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.subprocess, "run", lambda args, **k: calls.append(args) or _Proc(0))
    notify.send_notification('Say "hi"', 'x" & (do shell script "rm -rf ~") & "')
    args = calls[0]
    assert args[0] == "osascript"
    assert 'do shell script' not in args[2]          # the script itself is fixed
    assert args[-2:] == ['Say "hi"', 'x" & (do shell script "rm -rf ~") & "']


def test_notify_command_sends_and_records(conn, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(notify, "send_notification", lambda t, m: sent.append((t, m)))
    jt.add_action(conn, 10, "Complete Northstar assessment", due_date="2026-11-14")
    monkeypatch.setattr(notify, "_notify_now", lambda tz: NOW)
    jt.main_args(["notify"])
    jt.main_args(["notify"])
    assert sent == [("Due today: Complete Northstar assessment", "Northstar · Support Agent")]


def test_dry_run_sends_nothing_and_records_nothing(conn, monkeypatch, capsys):
    monkeypatch.setattr(notify, "send_notification", lambda t, m: pytest.fail("sent during dry run"))
    jt.add_action(conn, 10, "Complete Northstar assessment", due_date="2026-11-14")
    monkeypatch.setattr(notify, "_notify_now", lambda tz: NOW)
    jt.main_args(["notify", "--dry-run", "--json"])
    assert len(json.loads(capsys.readouterr().out)) == 1
    assert len(plan(conn)) == 1


def test_notifications_can_be_turned_off(conn, monkeypatch):
    monkeypatch.setenv("JT_NOTIFY", "0")
    monkeypatch.setattr(notify, "send_notification", lambda t, m: pytest.fail("sent while off"))
    jt.add_action(conn, 10, "Complete Northstar assessment", due_date="2026-11-14")
    monkeypatch.setattr(notify, "_notify_now", lambda tz: NOW)
    jt.main_args(["notify"])


def test_notify_runs_from_the_script_the_way_launchd_calls_it(tmp_path):
    """launchd runs `python jt.py notify`, so jt is loaded as __main__ and
    notify.py imports it by name. That path must work on its own."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "JT_DB": str(tmp_path / "jobs.db"),
           "JT_LOG_DIR": str(tmp_path / "logs"), "JT_TZ": "America/New_York"}
    r = subprocess.run([sys.executable, str(root / "jt.py"), "notify", "--dry-run", "--json"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == []
    # ...and notify shares that running copy of jt instead of loading a second one.
    probe = ("import runpy, sys; sys.argv = ['jt.py', 'notify', '--dry-run']; "
             f"runpy.run_path({str(root / 'jt.py')!r}, run_name='__main__'); "
             "print(sys.modules['notify'].jt.__name__)")
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                       env=env, timeout=30, cwd=root)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "__main__"


# ---------------------------------------------------------------- delivery (T1)
# CEO review 2026-11-21, F2: a blocked or broken notification used to be
# recorded as sent, so the alert meant to catch silent failures failed silently.

class _Proc:
    def __init__(self, returncode=0, stderr=""):
        self.returncode, self.stderr = returncode, stderr


def _due_task(conn):
    jt.add_action(conn, 10, "Complete Northstar assessment", due_date="2026-11-14")


def _failing_send(calls):
    def send(t, m):
        calls.append(t)
        return "osascript exited 1: Not authorized to send Apple events"
    return send


@pytest.mark.parametrize("run, expected", [
    (lambda args, **k: _Proc(0), None),
    (lambda args, **k: _Proc(1, "execution error: Not authorized (-1743)\n"),
     "osascript exited 1: execution error: Not authorized (-1743)"),
])
def test_send_notification_reports_delivery(monkeypatch, run, expected):
    monkeypatch.setattr(notify.subprocess, "run", run)
    assert notify.send_notification("t", "m") == expected


@pytest.mark.parametrize("exc, expected", [
    (notify.subprocess.TimeoutExpired(cmd="osascript", timeout=10), "osascript timed out after 10s"),
    (FileNotFoundError("osascript"), "osascript not found: osascript"),
])
def test_send_notification_reports_crashes(monkeypatch, exc, expected):
    def run(args, **k):
        raise exc
    monkeypatch.setattr(notify.subprocess, "run", run)
    assert notify.send_notification("t", "m") == expected


def test_failed_send_is_not_recorded_and_is_retried(conn, monkeypatch, capsys):
    calls: list[str] = []
    monkeypatch.setattr(notify, "send_notification", _failing_send(calls))
    monkeypatch.setattr(notify, "_notify_now", lambda tz: NOW)
    _due_task(conn)
    with pytest.raises(SystemExit) as e:
        jt.main_args(["notify"])
    assert e.value.code == 1
    assert "not delivered" in capsys.readouterr().err
    assert len(plan(conn)) == 1                      # still due: retried next run
    monkeypatch.setattr(notify, "send_notification", lambda t, m: None)
    jt.main_args(["notify"])
    assert plan(conn) == []                          # delivered, now recorded


def test_three_failed_runs_raise_a_visible_warning(conn, monkeypatch, capsys):
    calls: list[str] = []
    monkeypatch.setattr(notify, "send_notification", _failing_send(calls))
    monkeypatch.setattr(notify, "_notify_now", lambda tz: NOW)
    _due_task(conn)
    for n in range(3):
        assert notify.alerts_failing(conn) is None, f"warned after {n} failed run(s)"
        with pytest.raises(SystemExit):
            jt.main_args(["notify"])
    warn = notify.alerts_failing(conn)
    assert warn == {"channel": "mac", "failed_runs": 3,
                    "last_error": "osascript exited 1: Not authorized to send Apple events"}
    snap = jt.today_snapshot(conn, NOW.date(), NOW, NY)
    assert snap["freshness"]["alerts_failing"] == warn
    capsys.readouterr()
    jt.main_args(["today", "--on", "2026-11-14"])
    assert "Alerts aren't reaching you" in capsys.readouterr().out


def test_a_delivered_alert_clears_the_warning(conn, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(notify, "send_notification", _failing_send(calls))
    monkeypatch.setattr(notify, "_notify_now", lambda tz: NOW)
    _due_task(conn)
    for _ in range(3):
        with pytest.raises(SystemExit):
            jt.main_args(["notify"])
    monkeypatch.setattr(notify, "send_notification", lambda t, m: None)
    jt.main_args(["notify"])
    assert notify.alerts_failing(conn) is None
    assert jt.today_snapshot(conn, NOW.date(), NOW, NY)["freshness"]["alerts_failing"] is None


def test_partial_delivery_records_only_what_was_sent(conn, monkeypatch):
    sent = []

    def send(t, m):
        if t.startswith("Overdue"):
            return "osascript exited 1: boom"
        sent.append(t)
        return None
    monkeypatch.setattr(notify, "send_notification", send)
    monkeypatch.setattr(notify, "_notify_now", lambda tz: NOW)
    _due_task(conn)
    jt.add_action(conn, 10, "Send availability", due_date="2026-11-12")
    with pytest.raises(SystemExit):
        jt.main_args(["notify"])
    assert sent == ["Due today: Complete Northstar assessment"]
    assert [n["title"] for n in plan(conn)] == ["Overdue: Send availability"]


# ---------------------------------------------------------------- phone (T2)
# CEO review B1/D3: an outage alert must reach the phone, not only a Mac
# banner nobody is looking at. iMessage to yourself; JT_PHONE is your own
# iMessage address and lives only in ~/.jobtrack/jt.env.

ME = "+15555550123"
INJECTION = 'x" & (do shell script "say pwned") & "'


def _gmail_down(conn, tmp_path):
    sync_failed(tmp_path)
    jt.mark_fresh(conn, "gmail_synced_at", NOW - timedelta(hours=5))


def test_no_phone_configured_means_mac_only(conn, tmp_path):
    _gmail_down(conn, tmp_path)
    assert [n.get("channel", "mac") for n in plan(conn)] == ["mac"]


def test_gmail_outage_also_goes_to_the_phone(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("JT_PHONE", ME)
    _gmail_down(conn, tmp_path)
    mac, phone = plan(conn)
    assert phone["channel"] == "phone" and phone["key"] == "phone:" + mac["key"]
    assert (phone["title"], phone["message"]) == (mac["title"], mac["message"])


def test_tasks_and_new_applications_stay_off_the_phone(conn, tmp_path, monkeypatch):
    """Phone alerts for tasks are E3, chosen after the test week; T2 sends outages only."""
    monkeypatch.setenv("JT_PHONE", ME)
    _due_task(conn)
    jt.insert_application_candidate(conn, raw_input="x", source="email", source_ref="m1",
                                    company="Harbor Bank", role="Analyst")
    assert plan(conn) and all(n.get("channel", "mac") == "mac" for n in plan(conn))


def test_imessage_text_is_passed_as_arguments_not_code(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.subprocess, "run", lambda args, **k: calls.append(args) or _Proc(0))
    assert notify.send_imessage(ME, INJECTION) is None
    args = calls[0]
    assert args[:2] == ["osascript", "-e"]
    assert "do shell script" not in args[2] and 'application "Messages"' in args[2]
    assert args[-2:] == [ME, INJECTION]


def test_imessage_failure_is_reported(monkeypatch):
    monkeypatch.setattr(notify.subprocess, "run",
                        lambda args, **k: _Proc(1, "Messages got an error: Can't get account 1 (-1728)\n"))
    assert notify.send_imessage(ME, "hi") == (
        "osascript exited 1: Messages got an error: Can't get account 1 (-1728)")


def test_notify_sends_each_channel_its_own_way(conn, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JT_PHONE", ME)
    _gmail_down(conn, tmp_path)
    mac, texts = [], []
    monkeypatch.setattr(notify, "send_notification", lambda t, m: mac.append(t))
    monkeypatch.setattr(notify, "send_imessage", lambda to, text: texts.append((to, text)))
    monkeypatch.setattr(notify, "_notify_now", lambda tz: NOW)
    jt.main_args(["notify"])
    assert mac == ["jt can't read your Gmail"]
    [(to, text)] = texts
    assert to == ME and text.startswith("jt can't read your Gmail: ")
    assert plan(conn) == []


def test_phone_failure_is_retried_and_tracked_separately(conn, tmp_path, monkeypatch, capsys):
    """A working Mac banner must not hide a dead phone channel."""
    monkeypatch.setenv("JT_PHONE", ME)
    _gmail_down(conn, tmp_path)
    monkeypatch.setattr(notify, "send_notification", lambda t, m: None)
    monkeypatch.setattr(notify, "send_imessage", lambda to, text: "osascript exited 1: not signed in")
    for day in range(3):
        now = NOW + timedelta(days=day)        # each day the outage alert is due again
        monkeypatch.setattr(notify, "_notify_now", lambda tz, now=now: now)
        with pytest.raises(SystemExit):
            jt.main_args(["notify"])
        assert [n["channel"] for n in notify.notification_plan(conn, now)] == ["phone"]
    failing = notify.alerts_failing(conn)
    assert failing == {"channel": "phone", "failed_runs": 3,
                       "last_error": "osascript exited 1: not signed in"}
    assert "Messages" in notify.alerts_failing_help(failing)
