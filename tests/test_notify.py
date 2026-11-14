"""PRD P0 (audit 2026-11-13): Gmail sync was silently dead for 6 days, and a
deadline only showed if you opened the menu. `jt notify` sends each alert once."""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import jt

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=NY)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("JT_TZ", "America/New_York")
    monkeypatch.setenv("JT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("JT_NOTIFY", raising=False)
    monkeypatch.delenv("JT_NOTIFY_PRIVATE", raising=False)
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
    return jt.notification_plan(conn, NOW)


# ---------------------------------------------------------------- sync health

def test_sync_failure_for_hours_alerts_once_a_day(conn, tmp_path):
    sync_failed(tmp_path)
    jt.mark_fresh(conn, "gmail_synced_at", NOW - timedelta(hours=5))
    [n] = plan(conn)
    assert n["title"] == "jt can't read your Gmail"
    assert "sign-in expired" in n["message"]
    jt.record_notified(conn, [n])
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
    jt.record_notified(conn, [n])
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
    jt.record_notified(conn, [n])
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
    monkeypatch.setattr(jt.subprocess, "run", lambda args, **k: calls.append(args))
    jt.send_notification('Say "hi"', 'x" & (do shell script "rm -rf ~") & "')
    args = calls[0]
    assert args[0] == "osascript"
    assert 'do shell script' not in args[2]          # the script itself is fixed
    assert args[-2:] == ['Say "hi"', 'x" & (do shell script "rm -rf ~") & "']


def test_notify_command_sends_and_records(conn, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(jt, "send_notification", lambda t, m: sent.append((t, m)))
    jt.add_action(conn, 10, "Complete Northstar assessment", due_date="2026-11-14")
    monkeypatch.setattr(jt, "_notify_now", lambda tz: NOW)
    jt.main_args(["notify"])
    jt.main_args(["notify"])
    assert sent == [("Due today: Complete Northstar assessment", "Northstar · Support Agent")]


def test_dry_run_sends_nothing_and_records_nothing(conn, monkeypatch, capsys):
    monkeypatch.setattr(jt, "send_notification", lambda t, m: pytest.fail("sent during dry run"))
    jt.add_action(conn, 10, "Complete Northstar assessment", due_date="2026-11-14")
    monkeypatch.setattr(jt, "_notify_now", lambda tz: NOW)
    jt.main_args(["notify", "--dry-run", "--json"])
    assert len(json.loads(capsys.readouterr().out)) == 1
    assert len(plan(conn)) == 1


def test_notifications_can_be_turned_off(conn, monkeypatch):
    monkeypatch.setenv("JT_NOTIFY", "0")
    monkeypatch.setattr(jt, "send_notification", lambda t, m: pytest.fail("sent while off"))
    jt.add_action(conn, 10, "Complete Northstar assessment", due_date="2026-11-14")
    monkeypatch.setattr(jt, "_notify_now", lambda tz: NOW)
    jt.main_args(["notify"])
