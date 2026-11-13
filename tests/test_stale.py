"""PRD R11: an application quiet for weeks with no follow-up is a 'close out?'
question, not a daily 'follow up' nag. Only closes your rules allow are offered."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import jt

NY = ZoneInfo("America/New_York")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("JT_TZ", "America/New_York")
    c = jt.connect()
    yield c
    c.close()


def app(conn, kinds=(), applied_on="2026-08-28"):
    with jt.write_txn(conn):
        i = conn.execute("INSERT INTO applications (company, role, lane, applied_on) "
                         "VALUES ('Ramp', 'CX Agent', 'ops', ?)", (applied_on,)).lastrowid
        for k in kinds:
            conn.execute("INSERT INTO events (application_id, occurred_on, kind) VALUES (?, ?, ?)",
                         (i, applied_on, k))
    return i


def suggestions(conn, day):
    d = date.fromisoformat(day)
    return jt.today_snapshot(conn, d, datetime(d.year, d.month, d.day, 12, tzinfo=NY), NY)["suggestions"]


def test_recent_quiet_application_still_says_follow_up(conn):
    app(conn, ("confirmed",))
    [s] = suggestions(conn, "2026-09-10")            # ~9 business days
    assert s["kind"] == "FOLLOW UP"


def test_long_quiet_application_becomes_close_out_question(conn):
    app(conn, ("confirmed",))
    [s] = suggestions(conn, "2026-11-13")            # ~55 business days
    assert s["kind"] == "CLOSE OUT"
    assert "no reply" in s["reason"] and "business days" in s["reason"]


def test_close_choices_follow_your_transition_rules(conn):
    early = app(conn, ("confirmed",))
    late = app(conn, ("screen", "tech"), applied_on="2026-08-27")
    by_app = {s["application_id"]: s for s in suggestions(conn, "2026-11-13")}
    assert [c["kind"] for c in by_app[early]["close_choices"]] == ["ghosted", "withdrawn"]
    # transitions.py doesn't allow 'ghosted' after 'tech'; only withdrawing is offered.
    assert [c["kind"] for c in by_app[late]["close_choices"]] == ["withdrawn"]
    assert by_app[early]["close_choices"][0]["label"] == "Mark as no response"


def test_log_speaks_json_for_the_panel(conn, capsys):
    i = app(conn, ("confirmed",))
    jt.main_args(["log", str(i), "ghosted", "--json"])
    assert json.loads(capsys.readouterr().out) == {"ok": True, "application_id": i, "status": "ghosted"}


def test_illegal_close_from_the_panel_fails_cleanly(conn, capsys):
    i = app(conn, ("screen", "tech"))
    with pytest.raises(SystemExit) as exc:
        jt.main_args(["log", str(i), "ghosted", "--json"])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
