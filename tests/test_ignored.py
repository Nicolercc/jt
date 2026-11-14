"""PRD Now-E2: emails the classifier finds nothing in, from senders that aren't
recruiting platforms or companies you applied to, go to an 'ignored' list
instead of Review. Visible, reversible, and never applied to model failures."""

import json

import pytest

import classify
import jt


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    c = jt.connect()
    with jt.write_txn(c):
        c.execute("INSERT INTO applications (company, role, lane, applied_on) "
                  "VALUES ('Northstar', 'Support Agent', 'ops', '2026-08-28')")
    yield c
    c.close()


def raw(conn, msg_id, sender, subject="Hello", body="nothing job related here"):
    with jt.write_txn(conn):
        conn.execute("INSERT INTO raw_messages (gmail_msg_id, received_on, sender, subject, body) "
                     "VALUES (?, '2026-10-16', ?, ?, ?)", (msg_id, sender, subject, body))


def run(conn, response="[]"):
    msgs = classify.load_unprocessed(conn, 50)
    return classify.apply_verdicts(conn, classify.verdicts_for(conn, msgs, response))


def pending(conn):
    return [r["gmail_msg_id"] for r in conn.execute(
        "SELECT gmail_msg_id FROM review_queue WHERE resolved = 0 ORDER BY id")]


@pytest.mark.parametrize("sender", [
    "GitHub <noreply@github.com>", "Tailscale <hello@tailscale.com>",
    "Dior <news@communications.dior.us>"])
def test_non_job_sender_with_no_proposal_is_ignored(conn, sender):
    raw(conn, "n1", sender)
    run(conn)
    assert pending(conn) == []
    assert conn.execute("SELECT processed FROM raw_messages").fetchone()[0] == 1
    row = conn.execute("SELECT resolved, resolved_note FROM review_queue").fetchone()
    assert row["resolved"] == 1 and row["resolved_note"].startswith("auto-ignored")


@pytest.mark.parametrize("sender", [
    "Greenhouse <no-reply@us.greenhouse-mail.io>", "Lever <no-reply@hire.lever.co>",
    "CodeSignal <no-reply@codesignal.com>", "Hireflix <noreply@hireflix.com>",
    "Workday <acme@myworkday.com>", "Jane <jane@northstar.example>"])
def test_recruiting_or_applied_company_sender_still_goes_to_review(conn, sender):
    raw(conn, "r1", sender)
    run(conn)
    assert pending(conn) == ["r1"]


def test_model_failure_never_auto_ignores(conn):
    raw(conn, "n1", "GitHub <noreply@github.com>")
    run(conn, response="this is not json")
    assert pending(conn) == ["n1"]


def test_failed_validation_from_non_job_sender_still_goes_to_review(conn):
    """Only the 'found nothing' case is ignored; a proposal that fails a check
    is the classifier being unsure, which is yours to decide."""
    raw(conn, "n1", "GitHub <noreply@github.com>", body="we received your application today")
    p = {"gmail_msg_id": "n1", "company": "Northstar", "role_hint": "Support Agent", "kind": "screen",
         "occurred_on": "2026-10-16", "confidence": 0.2, "evidence": "we received your application"}
    run(conn, json.dumps([p]))
    assert pending(conn) == ["n1"]


def test_ignored_list_and_restore(conn, capsys):
    raw(conn, "n1", "GitHub <noreply@github.com>", subject="Your build passed")
    run(conn)
    jt.main_args(["ignored"])
    out = capsys.readouterr().out
    assert "github.com" in out and "Your build passed" in out
    rid = conn.execute("SELECT id FROM review_queue").fetchone()[0]
    jt.main_args(["ignored", "restore", str(rid)])
    assert pending(conn) == ["n1"]
    jt.main_args(["ignored"])
    assert "github.com" not in capsys.readouterr().out


def test_restore_refuses_a_real_review_item(conn):
    raw(conn, "r1", "Jane <jane@northstar.example>")
    run(conn)
    rid = conn.execute("SELECT id FROM review_queue").fetchone()[0]
    with pytest.raises(SystemExit, match="not an ignored"):
        jt.main_args(["ignored", "restore", str(rid)])
