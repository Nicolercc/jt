"""Review must read in plain English and offer only choices that will work."""

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import jt

NY = ZoneInfo("America/New_York")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("JT_TZ", "America/New_York")
    c = jt.connect()
    with jt.write_txn(c):
        c.execute("INSERT INTO applications (id, company, role, lane, applied_on) VALUES "
                  "(9, 'Ramp', 'CX Associate', 'ops', '2026-08-28'), "
                  "(10, 'Ramp', 'CX Agent', 'ops', '2026-08-28')")
        c.execute("INSERT INTO events (application_id, occurred_on, kind) VALUES "
                  "(9, '2026-09-08', 'rejected'), (10, '2026-08-28', 'screen')")
    yield c
    c.close()


def review(conn, msg_id, reason, proposal, sender="no-reply@hireflix.com",
           subject="Thank you for completing your video interview",
           body="Thank you for taking the time to complete the video interview."):
    with jt.write_txn(conn):
        conn.execute("INSERT INTO raw_messages (gmail_msg_id, received_on, sender, subject, body) "
                     "VALUES (?, '2026-09-05', ?, ?, ?)", (msg_id, f"Hireflix <{sender}>", subject, body))
        return conn.execute("INSERT INTO review_queue (gmail_msg_id, proposed_json, reason) "
                            "VALUES (?, ?, ?)", (msg_id, json.dumps(proposal), reason)).lastrowid


def P(kind, company="Ramp", **extra):
    return {"gmail_msg_id": "x", "company": company, "role_hint": "", "kind": kind,
            "occurred_on": "2026-09-05", "confidence": 0.6,
            "evidence": "Thank you for taking the time to complete the video interview", **extra}


def items(conn):
    return jt.review_items(conn)


def test_no_proposal_reads_plainly_and_offers_no_record_choice(conn):
    review(conn, "m1", "classifier returned no proposal for this message", None)
    [it] = items(conn)
    assert it["summary"] == "No job update found in this email."
    assert it["choices"] == []
    assert it["from"] == "hireflix.com" and it["subject"].startswith("Thank you")
    assert it["gmail_url"].endswith("#all/m1")


def test_low_confidence_names_the_guess_and_offers_only_legal_choices(conn):
    """Agent is at 'tech' (manual) or 'screen'; Associate is rejected (closed).
    A 'tech' update is legal for Agent only."""
    with jt.write_txn(conn):
        conn.execute("INSERT INTO events (application_id, occurred_on, kind) VALUES (10, '2026-08-29', 'response')")
    review(conn, "m2", "confidence below threshold", P("tech") | {"gmail_msg_id": "m2"})
    [it] = items(conn)
    assert "an assessment or technical interview from Ramp" in it["summary"]
    assert "isn't sure" in it["summary"]
    assert [c["app_id"] for c in it["choices"]] == [10]
    assert it["choices"][0]["label"] == "Record assessment or technical interview for Ramp · CX Agent"


def test_illegal_for_every_candidate_offers_nothing_to_record(conn):
    """'screen' after Agent's 'screen' isn't a legal transition; Associate is closed."""
    review(conn, "m3", "confidence below threshold", P("screen") | {"gmail_msg_id": "m3"})
    [it] = items(conn)
    assert it["choices"] == []


def test_confirmation_policy_reads_as_confirm(conn):
    review(conn, "m4", "needs your confirmation: offer for #10 (all other checks passed; DESIGN D3)",
           P("rejected") | {"gmail_msg_id": "m4"})
    [it] = items(conn)
    assert it["summary"] == "Looks like a rejection from Ramp. Confirm before jt records it."


def test_request_choice_reads_as_a_task(conn):
    review(conn, "m5", "ambiguous role at Ramp; no role_hint (CX Associate, CX Agent)",
           P("request", request={"description": "Complete Ramp assessment", "due_text": "",
                                 "due_iso": "", "due_tz": ""}) | {"gmail_msg_id": "m5"})
    [it] = items(conn)
    assert it["summary"] == "Which Ramp role is this for?"
    assert [c["label"] for c in it["choices"]] == ["Add task “Complete Ramp assessment” for Ramp · CX Agent"]


def test_today_json_carries_review_items(conn):
    review(conn, "m1", "classifier returned no proposal for this message", None)
    s = jt.today_snapshot(conn, date(2026, 9, 6), datetime(2026, 9, 6, 12, tzinfo=NY), NY)
    assert s["review"]["items"][0]["summary"] == "No job update found in this email."


def test_terminal_review_is_readable(conn, capsys):
    review(conn, "m1", "classifier returned no proposal for this message", None)
    jt.main_args(["review"])
    out = capsys.readouterr().out
    assert "No job update found in this email." in out
    assert "null" not in out and "classifier returned" not in out
    assert "jt review dismiss" in out
