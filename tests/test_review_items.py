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


# ---------------------------------------------------------------- new application

def td_review(conn, kind="screen", role="Associate Banker II"):
    return review(conn, "td1", "no application company match for 'TD Bank'",
                  P(kind, company="TD Bank", role_hint=role) | {
                      "gmail_msg_id": "td1", "occurred_on": "2026-09-23",
                      "evidence": "Thank you for taking the time to complete the video interview"},
                  sender="recruiting@td.com", subject="Interview for Associate Banker II at TD Bank")


def test_unknown_company_offers_add_as_new_application(conn):
    td_review(conn)
    [it] = items(conn)
    assert it["summary"] == ("Looks like an interview or screen invite from TD Bank, "
                             "but you have no application at TD Bank.")
    assert it["choices"] == [{"label": "Add TD Bank · Associate Banker II as a new application",
                              "new_app": True, "lanes": ["swe", "ops", "comms"]}]


def test_no_new_app_choice_without_a_role_or_for_an_illegal_first_stage(conn):
    td_review(conn, role="")
    td_review_id = conn.execute("SELECT MAX(id) FROM review_queue").fetchone()[0]
    assert items(conn)[0]["choices"] == []
    with jt.write_txn(conn):
        conn.execute("UPDATE review_queue SET resolved = 1 WHERE id = ?", (td_review_id,))
    # 'tech' can't be an application's first stage under transitions.py
    with jt.write_txn(conn):
        conn.execute("DELETE FROM raw_messages")
    td_review(conn, kind="tech")
    assert items(conn)[0]["choices"] == []


def test_add_app_creates_application_and_records_the_update(conn, capsys):
    rid = td_review(conn)
    jt.main_args(["review", "add-app", str(rid), "--lane", "ops", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["resolved"]
    app = conn.execute("SELECT * FROM applications WHERE company = 'TD Bank'").fetchone()
    assert (app["role"], app["lane"], app["applied_on"]) == ("Associate Banker II", "ops", "2026-09-23")
    assert "applied date unknown" in app["notes"]
    kinds = [r[0] for r in conn.execute("SELECT kind FROM events WHERE application_id = ?", (app["id"],))]
    assert kinds == ["screen"]
    assert conn.execute("SELECT resolved FROM review_queue WHERE id = ?", (rid,)).fetchone()[0] == 1


def test_add_app_failure_leaves_nothing_behind(conn, capsys):
    """If the update can't be recorded, the new application must not exist."""
    rid = td_review(conn, kind="tech")
    with pytest.raises(SystemExit):
        jt.main_args(["review", "add-app", str(rid), "--lane", "ops", "--json"])
    assert conn.execute("SELECT COUNT(*) FROM applications WHERE company = 'TD Bank'").fetchone()[0] == 0
    assert conn.execute("SELECT resolved FROM review_queue WHERE id = ?", (rid,)).fetchone()[0] == 0


# ---------------------------------------------------------------- evidence source

def test_evidence_quoted_from_the_subject_counts(conn, capsys):
    """Real case (TD Bank): the model quoted the subject line. It is part of the
    email, so a verbatim subject quote is valid evidence."""
    rid = review(conn, "td2", "no application company match for 'TD Bank'",
                 P("screen", company="TD Bank", role_hint="Associate Banker II") | {
                     "gmail_msg_id": "td2", "occurred_on": "2026-09-23",
                     "evidence": "Interview for Associate Banker II at TD Bank"},
                 sender="recruiting@td.com", subject="Interview for Associate Banker II at TD Bank",
                 body="Hello, please pick a time that works for you.")
    assert items(conn)[0]["choices"]
    jt.main_args(["review", "add-app", str(rid), "--lane", "ops", "--json"])
    assert json.loads(capsys.readouterr().out)["ok"]


def test_no_choice_is_offered_when_the_evidence_is_not_in_the_email(conn):
    """A shown button must always work: same evidence check as approval."""
    review(conn, "td3", "no application company match for 'TD Bank'",
           P("screen", company="TD Bank", role_hint="Associate Banker II") | {
               "gmail_msg_id": "td3", "occurred_on": "2026-09-23",
               "evidence": "words that appear nowhere"},
           sender="recruiting@td.com", subject="Hello", body="Nothing relevant.")
    assert items(conn)[0]["choices"] == []


# ---------------------------------------------------------------- stale stage (from #19)

def test_choices_carry_the_stage_they_were_computed_for(conn):
    with jt.write_txn(conn):
        conn.execute("INSERT INTO events (application_id, occurred_on, kind) VALUES (10, '2026-08-29', 'response')")
    review(conn, "m2", "confidence below threshold", P("tech") | {"gmail_msg_id": "m2"})
    [it] = items(conn)
    assert it["choices"][0]["stage"] == "screen"


def test_approval_is_refused_if_the_stage_changed_since_you_looked(conn, capsys):
    """Originally Codex's #19: the menu showed a choice computed at one stage;
    if the hourly job moved the application on, the click must not apply."""
    rid = review(conn, "m2", "confidence below threshold", P("tech") | {"gmail_msg_id": "m2"})
    with pytest.raises(SystemExit):
        jt.main_args(["review", "approve", str(rid), "--app-id", "10",
                      "--expected-stage", "confirmed", "--json"])
    err = json.loads(capsys.readouterr().out)
    assert "reload" in err["error"]
    assert conn.execute("SELECT resolved FROM review_queue WHERE id = ?", (rid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'tech'").fetchone()[0] == 0
    jt.main_args(["review", "approve", str(rid), "--app-id", "10", "--expected-stage", "screen", "--json"])
    assert json.loads(capsys.readouterr().out)["ok"]
