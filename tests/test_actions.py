"""DESIGN D1 (many actions per application) and D2 (timed actions)."""

import sqlite3
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import jt

NY = ZoneInfo("America/New_York")
LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("JT_TZ", "America/New_York")
    c = jt.connect()
    yield c
    c.close()


def app(conn, company="Acme", applied_on="2026-08-03"):
    with jt.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO applications (company, role, lane, applied_on) VALUES (?,?,?,?)",
            (company, "Engineer", "swe", applied_on))
    return cur.lastrowid


def at(y, m, d, hh=12, mm=0, tz=NY):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


# ---------------------------------------------------------------- schema

def test_old_next_actions_rows_migrate_to_open_all_day_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "old.db"))
    c = jt.connect()
    a = app(c)
    c.execute("CREATE TABLE next_actions (application_id INTEGER PRIMARY KEY, "
              "description TEXT NOT NULL, due_on TEXT NOT NULL)")
    c.execute("INSERT INTO next_actions VALUES (?, 'Send availability', '2026-08-05')", (a,))
    c.commit()
    c.close()

    c = jt.connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM actions")]
    assert len(rows) == 1
    r = rows[0]
    assert (r["application_id"], r["description"], r["due_date"], r["state"], r["origin"]) == \
        (a, "Send availability", "2026-08-05", "open", "migrated")
    assert r["due_at_utc"] is None
    assert not c.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'next_actions'").fetchone()
    c.close()
    jt.connect().close()     # migrating twice is a no-op


def test_due_date_and_due_time_are_mutually_exclusive(conn):
    a = app(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO actions (application_id, description, due_date, due_at_utc, due_tz) "
                     "VALUES (?, 'x', '2026-08-05', '2026-08-05T18:00:00Z', 'America/New_York')", (a,))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO actions (application_id, description, due_at_utc) "
                     "VALUES (?, 'x', '2026-08-05T18:00:00Z')", (a,))
    conn.rollback()


def test_snoozed_state_requires_a_return_time(conn):
    a = app(conn)
    action = jt.add_action(conn, a, "Follow up", due_date="2026-08-10")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE actions SET state = 'snoozed' WHERE id = ?", (action,))
    conn.rollback()


# ---------------------------------------------------------------- many per app

def test_an_application_can_hold_several_actions(conn):
    a = app(conn)
    first = jt.add_action(conn, a, "Send availability", due_date="2026-08-05")
    second = jt.add_action(conn, a, "Follow up", due_date="2026-08-12")
    assert first != second
    descs = [r["description"] for r in jt.pending_actions(conn, now=at(2026, 8, 4))]
    assert descs == ["Send availability", "Follow up"]


def test_done_and_dismissed_close_an_action_with_a_timestamp(conn):
    a = app(conn)
    done = jt.add_action(conn, a, "A", due_date="2026-08-05")
    gone = jt.add_action(conn, a, "B", due_date="2026-08-05")
    jt.set_action_state(conn, done, "done")
    jt.set_action_state(conn, gone, "dismissed")
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM actions")}
    assert rows[done]["state"] == "done" and rows[done]["closed_at"]
    assert rows[gone]["state"] == "dismissed" and rows[gone]["closed_at"]
    assert jt.pending_actions(conn, now=at(2026, 8, 6)) == []


def test_closing_twice_is_refused(conn):
    a = app(conn)
    x = jt.add_action(conn, a, "A", due_date="2026-08-05")
    jt.set_action_state(conn, x, "done")
    with pytest.raises(SystemExit, match="already done"):
        jt.set_action_state(conn, x, "dismissed")


def test_snoozed_action_returns_after_its_time(conn):
    a = app(conn)
    x = jt.add_action(conn, a, "Follow up", due_date="2026-08-05")
    jt.set_action_state(conn, x, "snoozed", snoozed_until=at(2026, 8, 7, 9, 0))
    assert jt.pending_actions(conn, now=at(2026, 8, 6), include_snoozed=False) == []
    assert [r["id"] for r in jt.pending_actions(conn, now=at(2026, 8, 7, 9, 1),
                                                include_snoozed=False)] == [x]


# ---------------------------------------------------------------- today

def today_rows(conn, day, capsys):
    jt.cmd_today(conn, type("A", (), {"on": day})())
    return capsys.readouterr().out


def test_future_action_suppresses_the_generic_follow_up(conn, capsys):
    """The audit's bug: an explicit action due Friday must stop `today` from
    telling you to chase the recruiter on Tuesday."""
    a = app(conn, applied_on="2026-08-03")
    out = today_rows(conn, "2026-08-12", capsys)          # 7 business days quiet
    assert "FOLLOW UP" in out

    jt.add_action(conn, a, "Email the hiring manager", due_date="2026-08-14")
    out = today_rows(conn, "2026-08-12", capsys)
    assert "FOLLOW UP" not in out
    assert "No actions due." in out

    out = today_rows(conn, "2026-08-14", capsys)
    assert "Email the hiring manager" in out


def test_snoozed_action_still_suppresses_follow_up_while_snoozed(conn, capsys):
    a = app(conn, applied_on="2026-08-03")
    x = jt.add_action(conn, a, "Follow up", due_date="2026-08-10")
    jt.set_action_state(conn, x, "snoozed", snoozed_until=at(2026, 8, 20, 9))
    out = today_rows(conn, "2026-08-12", capsys)
    assert "FOLLOW UP" not in out and "Follow up" not in out


def test_overdue_action_is_listed_and_marked(conn, capsys):
    a = app(conn)
    jt.add_action(conn, a, "Send availability", due_date="2026-08-05")
    out = today_rows(conn, "2026-08-07", capsys)
    assert "Send availability" in out and "overdue" in out


def test_terminal_application_hides_its_actions(conn, capsys):
    a = app(conn)
    jt.add_action(conn, a, "Send availability", due_date="2026-08-05")
    with jt.write_txn(conn):
        conn.execute("INSERT INTO events (application_id, occurred_on, kind) "
                     "VALUES (?, '2026-08-04', 'rejected')", (a,))
    out = today_rows(conn, "2026-08-05", capsys)
    assert "Send availability" not in out
    assert conn.execute("SELECT state FROM actions").fetchone()[0] == "open"   # hidden, not deleted


# ---------------------------------------------------------------- time zones

def test_timed_action_is_stored_in_utc_with_its_zone(conn):
    a = app(conn)
    x = jt.add_action(conn, a, "Interview", due_local=datetime(2026, 10, 27, 14, 0), tz="America/New_York")
    r = conn.execute("SELECT due_at_utc, due_tz, due_date FROM actions WHERE id = ?", (x,)).fetchone()
    assert (r["due_at_utc"], r["due_tz"], r["due_date"]) == \
        ("2026-10-27T18:00:00Z", "America/New_York", None)   # EDT, UTC-4


def test_timed_action_keeps_wall_time_across_the_dst_change(conn):
    """DST ends 2026-11-01 in New York. A 2pm interview the week after must
    still read 2pm in New York, and 11am in Los Angeles."""
    a = app(conn)
    before = jt.add_action(conn, a, "Before", due_local=datetime(2026, 10, 27, 14), tz="America/New_York")
    after = jt.add_action(conn, a, "After", due_local=datetime(2026, 11, 3, 14), tz="America/New_York")
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM actions")}
    assert rows[after]["due_at_utc"] == "2026-11-03T19:00:00Z"            # EST, UTC-5
    for x in (before, after):
        assert jt.action_due_local(rows[x], NY).strftime("%H:%M") == "14:00"
        assert jt.action_due_local(rows[x], LA).strftime("%H:%M") == "11:00"


def test_timed_action_due_day_follows_the_viewers_zone(conn):
    """11pm in Los Angeles is already tomorrow in New York."""
    a = app(conn)
    x = jt.add_action(conn, a, "Late call", due_local=datetime(2026, 8, 5, 23, 0), tz="America/Los_Angeles")
    row = conn.execute("SELECT * FROM actions WHERE id = ?", (x,)).fetchone()
    assert jt.action_due_day(row, LA) == date(2026, 8, 5)
    assert jt.action_due_day(row, NY) == date(2026, 8, 6)


def test_unknown_time_zone_is_refused(conn):
    a = app(conn)
    with pytest.raises(SystemExit, match="time zone"):
        jt.add_action(conn, a, "x", due_local=datetime(2026, 8, 5, 9), tz="Mars/Olympus")


# ---------------------------------------------------------------- CLI

def run(argv, capsys):
    jt.main_args(argv)
    return capsys.readouterr().out


def test_cli_action_add_list_done(conn, capsys):
    a = app(conn)
    out = run(["action", "add", str(a), "Send availability", "--due", "2026-08-05"], capsys)
    assert "action #1" in out
    out = run(["action", "add", str(a), "Interview", "--due", "2026-08-06", "--at", "14:00"], capsys)
    assert "action #2" in out and "14:00" in out
    out = run(["action", "list"], capsys)
    assert "Send availability" in out and "Interview" in out
    run(["action", "done", "1"], capsys)
    run(["action", "snooze", "2", "--until", "2026-08-08"], capsys)
    out = run(["action", "list"], capsys)
    assert "Send availability" not in out and "snoozed" in out


def test_cli_next_action_still_works_and_clear_dismisses(conn, capsys):
    a = app(conn)
    run(["next-action", str(a), "Send availability", "--due", "2026-08-05"], capsys)
    assert conn.execute("SELECT COUNT(*) FROM actions WHERE state = 'open'").fetchone()[0] == 1
    run(["next-action", str(a), "--clear"], capsys)
    assert conn.execute("SELECT state FROM actions").fetchone()[0] == "dismissed"


def test_cli_snooze_requires_until(conn, capsys):
    a = app(conn)
    run(["action", "add", str(a), "x", "--due", "2026-08-05"], capsys)
    with pytest.raises(SystemExit):
        jt.main_args(["action", "snooze", "1"])


# ---------------------------------------------------------------- viewer zone

@pytest.mark.parametrize("tz_env", [":/etc/localtime", "EST5EDT", "garbage", "", "UTC0"])
def test_unusual_TZ_never_crashes_jt(monkeypatch, tz_env):
    """$TZ is a POSIX setting, not jt config. Values like ':/etc/localtime'
    are valid for the OS but are not IANA names; jt must fall back, not exit."""
    monkeypatch.delenv("JT_TZ", raising=False)
    monkeypatch.setenv("TZ", tz_env)
    assert isinstance(jt.viewer_tz(), ZoneInfo)


def test_TZ_with_leading_colon_iana_name_is_honored(monkeypatch):
    monkeypatch.delenv("JT_TZ", raising=False)
    monkeypatch.setenv("TZ", ":America/Los_Angeles")
    assert jt.viewer_tz().key == "America/Los_Angeles"


def test_bad_JT_TZ_is_still_a_clear_error(monkeypatch):
    """JT_TZ is jt's own setting, so a typo there should be reported."""
    monkeypatch.setenv("JT_TZ", "Mars/Olympus")
    with pytest.raises(SystemExit, match="time zone"):
        jt.viewer_tz()


def test_action_show_reconciles_completion_after_lost_response(conn, capsys):
    import json

    action_id = jt.add_action(conn, app(conn), "Send availability", due_date="2026-08-05")
    jt.main_args(["action", "done", str(action_id), "--json"])
    capsys.readouterr()
    jt.main_args(["action", "show", str(action_id), "--json"])
    assert json.loads(capsys.readouterr().out) == {
        "ok": True, "action_id": action_id, "state": "done",
    }


def test_action_show_reports_missing_action(conn, capsys):
    import json

    with pytest.raises(SystemExit) as exc:
        jt.main_args(["action", "show", "999", "--json"])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
