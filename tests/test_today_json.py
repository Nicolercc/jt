"""DESIGN D5: `jt today --json` is the only thing a frontend may read.

These tests pin the contract. Adding a field is fine (update TOP_KEYS etc.
deliberately); renaming or removing one must bump schema_version.
"""

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import jt

NY = ZoneInfo("America/New_York")
TOP_KEYS = {"schema_version", "generated_at", "badge", "freshness", "actions",
            "suggestions", "review", "recorded", "version"}
ACTION_KEYS = {"action_id", "application_id", "company", "role", "description", "due",
               "overdue", "origin", "reason", "gmail_msg_id", "gmail_url"}
SUGGESTION_KEYS = {"application_id", "company", "role", "kind", "reason"}
FRESHNESS_KEYS = {"gmail_synced_at", "classified_at", "sync_error"}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("JT_TZ", "America/New_York")
    monkeypatch.setenv("JT_LOG_DIR", str(tmp_path / "logs"))
    c = jt.connect()
    yield c
    c.close()


def app(conn, company="Acme", role="Engineer", applied_on="2026-08-03"):
    with jt.write_txn(conn):
        return conn.execute("INSERT INTO applications (company, role, lane, applied_on) "
                            "VALUES (?,?,?,?)", (company, role, "swe", applied_on)).lastrowid


def snapshot(conn, day="2026-08-05"):
    today = date.fromisoformat(day)
    return jt.today_snapshot(conn, today, datetime(2026, 8, 5, 12, tzinfo=NY), NY)


def sync_log(tmp_path, *events):
    p = tmp_path / "logs" / "jt-sync.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        for e in events:
            f.write(json.dumps({"ts": "2026-08-05T15:00:00+00:00", "component": "jt-sync", **e}) + "\n")


def check_shape(s):
    assert set(s) == TOP_KEYS
    assert s["schema_version"] == 1
    assert set(s["freshness"]) == FRESHNESS_KEYS
    assert set(s["badge"]) == {"due", "reviews"}
    assert set(s["review"]) == {"pending", "oldest_days", "top"}
    for a in s["actions"]:
        assert set(a) == ACTION_KEYS
        assert a["due"]["kind"] in {"date", "time"}
    for x in s["suggestions"]:
        assert set(x) == SUGGESTION_KEYS
    json.dumps(s)            # must be serializable as-is


# ---------------------------------------------------------------- six states

def test_state_empty(conn):
    s = snapshot(conn)
    check_shape(s)
    assert s["badge"] == {"due": 0, "reviews": 0}
    assert s["actions"] == [] and s["suggestions"] == []
    assert s["freshness"] == {"gmail_synced_at": None, "classified_at": None, "sync_error": None}
    assert s["review"] == {"pending": 0, "oldest_days": 0, "top": None}


def test_state_healthy(conn, tmp_path):
    a = app(conn)
    jt.add_action(conn, a, "Send availability", due_date="2026-08-05", reason="Recruiter asked")
    jt.mark_fresh(conn, "gmail_synced_at", datetime(2026, 8, 5, 15, tzinfo=timezone.utc))
    jt.mark_fresh(conn, "classified_at", datetime(2026, 8, 5, 15, 1, tzinfo=timezone.utc))
    sync_log(tmp_path, {"event": "start"}, {"event": "success"})
    s = snapshot(conn)
    check_shape(s)
    assert s["badge"] == {"due": 1, "reviews": 0}
    assert s["freshness"] == {"gmail_synced_at": "2026-08-05T15:00:00Z",
                              "classified_at": "2026-08-05T15:01:00Z", "sync_error": None}
    act = s["actions"][0]
    assert act["description"] == "Send availability" and act["reason"] == "Recruiter asked"
    assert act["due"] == {"kind": "date", "value": "2026-08-05"} and act["overdue"] is False
    assert act["origin"] == "manual"


def test_state_offline(conn, tmp_path):
    sync_log(tmp_path, {"event": "start"},
             {"event": "failure", "error_type": "ServerNotFoundError", "error": "Unable to find the server"})
    assert snapshot(conn)["freshness"]["sync_error"] == "offline"


@pytest.mark.parametrize("error_type,error", [
    ("RefreshError", "invalid_grant: Token has been expired or revoked."),
    ("SystemExit", "error: Gmail credentials are not valid for unattended use; run jt-sync interactively"),
])
def test_state_auth_expired(conn, tmp_path, error_type, error):
    sync_log(tmp_path, {"event": "failure", "error_type": error_type, "error": error})
    assert snapshot(conn)["freshness"]["sync_error"] == "auth_expired"


def test_other_sync_failure_and_recovery(conn, tmp_path):
    sync_log(tmp_path, {"event": "failure", "error_type": "HttpError", "error": "500"})
    assert snapshot(conn)["freshness"]["sync_error"] == "failed"
    sync_log(tmp_path, {"event": "start"}, {"event": "success"})
    assert snapshot(conn)["freshness"]["sync_error"] is None


def test_state_ambiguous_match_review(conn):
    with jt.write_txn(conn):
        conn.execute("INSERT INTO review_queue (gmail_msg_id, proposed_json, reason, created_at) "
                     "VALUES ('m1', '{}', 'ambiguous role for ''Acme''; multiple roles matched', "
                     "'2026-08-03 10:00:00')")
        conn.execute("INSERT INTO review_queue (gmail_msg_id, proposed_json, reason, created_at) "
                     "VALUES ('m2', '{}', 'confidence below threshold', '2026-08-04 10:00:00')")
    s = snapshot(conn)
    check_shape(s)
    assert s["badge"]["reviews"] == 2
    assert s["review"]["pending"] == 2
    assert s["review"]["top"] == {"review_id": 1,
                                  "reason": "ambiguous role for 'Acme'; multiple roles matched"}


def test_state_overdue_and_suggestion(conn):
    a = app(conn, "Acme")
    b = app(conn, "Globex", applied_on="2026-07-20")         # quiet long enough to nudge
    jt.add_action(conn, a, "Send availability", due_date="2026-08-03")
    s = snapshot(conn)
    check_shape(s)
    assert s["actions"][0]["overdue"] is True
    assert s["badge"]["due"] == 1                            # suggestions never count
    assert [x["application_id"] for x in s["suggestions"]] == [b]
    sug = s["suggestions"][0]
    assert sug["kind"] == "FOLLOW UP" and "business days" in sug["reason"]


# ---------------------------------------------------------------- details

def test_timed_action_due_is_utc_with_zone(conn):
    a = app(conn)
    jt.add_action(conn, a, "Interview", due_local=datetime(2026, 8, 5, 14), tz="America/New_York")
    due = snapshot(conn)["actions"][0]["due"]
    assert due == {"kind": "time", "value": "2026-08-05T18:00:00Z", "tz": "America/New_York"}


def test_action_from_an_email_links_to_it(conn, monkeypatch):
    a = app(conn)
    with jt.write_txn(conn):
        ev = conn.execute("INSERT INTO events (application_id, occurred_on, kind, gmail_msg_id) "
                          "VALUES (?, '2026-08-04', 'screen', '18cabc')", (a,)).lastrowid
    jt.add_action(conn, a, "Send availability", due_date="2026-08-05",
                  origin="review", source_event_id=ev)
    act = snapshot(conn)["actions"][0]
    assert act["gmail_msg_id"] == "18cabc"
    assert act["gmail_url"] == "https://mail.google.com/mail/u/0/#all/18cabc"
    monkeypatch.setenv("GMAIL_ACCOUNT_INDEX", "1")
    assert snapshot(conn)["actions"][0]["gmail_url"].startswith("https://mail.google.com/mail/u/1/")


def test_snoozed_action_is_not_counted_until_it_returns(conn):
    a = app(conn)
    x = jt.add_action(conn, a, "Follow up", due_date="2026-08-04")
    jt.set_action_state(conn, x, "snoozed", snoozed_until=datetime(2026, 8, 6, 9, tzinfo=NY))
    s = snapshot(conn)
    assert s["badge"]["due"] == 0 and s["actions"] == [] and s["suggestions"] == []


# ---------------------------------------------------------------- CLI

def test_cli_today_json_prints_the_snapshot(conn, capsys):
    a = app(conn)
    jt.add_action(conn, a, "Send availability", due_date="2026-08-05")
    jt.main_args(["today", "--on", "2026-08-05", "--json"])
    s = json.loads(capsys.readouterr().out)
    check_shape(s)
    assert s["badge"]["due"] == 1


def test_cli_action_commands_speak_json(conn, capsys):
    a = app(conn)
    jt.main_args(["action", "add", str(a), "x", "--due", "2026-08-05", "--json"])
    added = json.loads(capsys.readouterr().out)
    assert added == {"ok": True, "action_id": 1, "state": "open"}
    jt.main_args(["action", "snooze", "1", "--until", "2026-08-07 09:00", "--json"])
    assert json.loads(capsys.readouterr().out) == {"ok": True, "action_id": 1, "state": "snoozed"}
    jt.main_args(["action", "done", "1", "--json"])
    assert json.loads(capsys.readouterr().out) == {"ok": True, "action_id": 1, "state": "done"}


def test_cli_json_errors_are_json_and_exit_nonzero(conn, capsys):
    a = app(conn)
    jt.main_args(["action", "add", str(a), "x", "--due", "2026-08-05", "--json"])
    jt.main_args(["action", "done", "1", "--json"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        jt.main_args(["action", "done", "1", "--json"])
    assert exc.value.code == 1
    err = json.loads(capsys.readouterr().out)
    assert err["ok"] is False and "already done" in err["error"]


def test_cli_review_dismiss_speaks_json(conn, capsys):
    with jt.write_txn(conn):
        conn.execute("INSERT INTO review_queue (gmail_msg_id, proposed_json, reason) "
                     "VALUES ('m1', '{}', 'x')")
    jt.main_args(["review", "dismiss", "1", "--json"])
    assert json.loads(capsys.readouterr().out) == {"ok": True, "review_id": 1, "resolved": True,
                                                   "event_id": None}

def test_recorded_shows_latest_email_events_only(conn):
    application_id = app(conn)
    with jt.write_txn(conn):
        conn.executemany(
            "INSERT INTO events (application_id, occurred_on, kind, gmail_msg_id) "
            "VALUES (?, '2026-08-05', 'confirmed', ?)",
            [(application_id, source) for source in ("msg1", "msg2", "msg3", "msg4", None)],
        )
    recorded = snapshot(conn)["recorded"]
    assert [event["event_id"] for event in recorded] == [4, 3, 2]
    assert recorded[0]["gmail_url"] == jt.gmail_url("msg4")
    assert recorded[0]["company"] == "Acme"
    assert recorded[0]["role"] == "Engineer"
