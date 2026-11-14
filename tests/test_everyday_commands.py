"""Audit P1: list, show, stale, bulk and priority were mostly untested.
These read today's real date, so test data is placed relative to today."""

import io
from datetime import date, timedelta

import pytest

import jt


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    c = jt.connect()
    yield c
    c.close()


def days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def app(conn, company, role="Engineer", lane="swe", applied_days_ago=3, events=(), contact=None):
    with jt.write_txn(conn):
        i = conn.execute("INSERT INTO applications (company, role, lane, applied_on, contact_email) "
                         "VALUES (?,?,?,?,?)", (company, role, lane, days_ago(applied_days_ago), contact)).lastrowid
        for kind, when, msg in events:
            conn.execute("INSERT INTO events (application_id, occurred_on, kind, gmail_msg_id, evidence) "
                         "VALUES (?,?,?,?,?)", (i, days_ago(when), kind, msg, "quoted line" if msg else None))
    return i


def run(argv, capsys, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    jt.main_args(argv)
    return capsys.readouterr().out


# ---------------------------------------------------------------- bulk

def test_bulk_adds_valid_lines_and_explains_every_skip(conn, capsys, monkeypatch):
    text = ("# today's batch\n"
            "Acme | Engineer | swe | https://acme.example/job\n"
            "Globex | Coordinator | OPS\n"
            "\n"
            "Missing lane | Role\n"
            "Bad | Role | design\n"
            " | Role | swe\n"
            "Acme | Engineer | swe\n")
    out = run(["bulk"], capsys, stdin=text, monkeypatch=monkeypatch)
    assert "added 2, skipped 4" in out
    assert "need 'Company | Role | lane'" in out and "bad lane 'design'" in out and "empty company" in out
    rows = conn.execute("SELECT company, lane, url FROM applications ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("Acme", "swe", "https://acme.example/job"), ("Globex", "ops", None)]


def test_bulk_uses_the_given_day(conn, capsys, monkeypatch):
    run(["bulk", "--on", "2026-08-03"], capsys, stdin="Acme | Engineer | swe\n", monkeypatch=monkeypatch)
    assert conn.execute("SELECT applied_on FROM applications").fetchone()[0] == "2026-08-03"


# ---------------------------------------------------------------- list

def test_list_hides_closed_applications_unless_all(conn, capsys):
    app(conn, "OpenCo")
    app(conn, "ClosedCo", events=[("rejected", 1, None)])
    out = run(["list"], capsys)
    assert "OpenCo" in out and "ClosedCo" not in out and "1 shown" in out and "--all" in out
    out = run(["list", "--all"], capsys)
    assert "ClosedCo" in out and "2 shown" in out


def test_list_filters_by_lane_and_status(conn, capsys):
    app(conn, "SweCo", lane="swe")
    app(conn, "OpsCo", lane="ops", events=[("screen", 1, None)])
    out = run(["list", "--lane", "ops"], capsys)
    assert "OpsCo" in out and "SweCo" not in out
    out = run(["list", "--status", "screen"], capsys)
    assert "OpsCo" in out and "SweCo" not in out


# ---------------------------------------------------------------- show

def test_show_prints_details_and_marks_auto_vs_manual_history(conn, capsys):
    i = app(conn, "Northstar", contact="r@northstar.example",
            events=[("confirmed", 2, "msg1"), ("screen", 1, None)])
    out = run(["show", str(i)], capsys)
    assert "Northstar - Engineer" in out and "status screen" in out and "r@northstar.example" in out
    assert "auto" in out and "manual" in out and "quoted line" in out


def test_show_unknown_application_is_a_clear_error(conn):
    with pytest.raises(SystemExit, match="no application #99"):
        jt.main_args(["show", "99"])


# ---------------------------------------------------------------- stale

def test_stale_lists_follow_ups_first_then_close_outs(conn, capsys):
    app(conn, "Fresh", applied_days_ago=1)
    app(conn, "Nudge", applied_days_ago=14)          # ~10 business days: follow up
    app(conn, "Dead", applied_days_ago=80)           # 50+ business days: close out
    app(conn, "Closed", applied_days_ago=80, events=[("rejected", 70, None)])
    out = run(["stale"], capsys)
    assert "Fresh" not in out and "Closed" not in out
    assert out.index("Nudge") < out.index("Dead")
    assert "FOLLOW UP" in out and "CLOSE OUT" in out


# ---------------------------------------------------------------- priority

def test_priority_ranks_warm_contacts_higher_and_skips_closed(conn, capsys):
    app(conn, "Cold", lane="ops")
    app(conn, "Warm", lane="ops", contact="person@warm.example")
    app(conn, "Closed", events=[("rejected", 1, None)])
    out = run(["priority"], capsys)
    assert "Closed" not in out
    assert out.index("Warm") < out.index("Cold")


def test_priority_explain_shows_each_term_and_the_total(conn, capsys):
    i = app(conn, "Warm", contact="person@warm.example")
    out = run(["priority", "--explain", str(i)], capsys)
    for term in ("base", "contact", "had_response", "stage_progress", "quiet", "closeout", "TOTAL"):
        assert term in out
    with pytest.raises(SystemExit, match="no application #99"):
        jt.main_args(["priority", "--explain", "99"])
