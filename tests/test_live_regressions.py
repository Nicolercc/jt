"""Replays model outputs recorded from LIVE runs (tests/fixtures/live_regressions.json)
through the real classifier path. Each case once broke jt or exercised a rule only
live data reached. If one fails, that bug has come back."""

import json
from pathlib import Path

import pytest

import classify
import jt

CASES = json.loads((Path(__file__).parent / "fixtures/live_regressions.json").read_text())["cases"]


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("JT_TZ", "America/New_York")
    monkeypatch.delenv("CLASSIFIER_AUTO_COMMIT_TERMINAL", raising=False)
    c = jt.connect()
    yield c
    c.close()


def seed(conn, case):
    with jt.write_txn(conn):
        for a in case["apps"]:
            conn.execute("INSERT INTO applications (id, company, role, lane, applied_on) VALUES (?,?,?,?,?)",
                         (a["id"], a["company"], a["role"], "ops", a["applied_on"]))
        for app_id, kind, day in case["events"]:
            conn.execute("INSERT INTO events (application_id, occurred_on, kind) VALUES (?,?,?)",
                         (app_id, day, kind))
        for m in case["messages"]:
            conn.execute("INSERT INTO raw_messages (gmail_msg_id, received_on, sender, subject, body) "
                         "VALUES (?,?,?,?,?)", (m["id"], m["received_on"], m["sender"], m["subject"], m["body"]))


def replay(conn, case):
    """Messages are applied in order, one batch each, like separate hourly runs."""
    by_msg = {p["gmail_msg_id"]: p for p in case["response"]}
    for m in case["messages"]:
        msgs = [r for r in classify.load_unprocessed(conn, 100) if r["gmail_msg_id"] == m["id"]]
        response = json.dumps([by_msg[m["id"]]] if m["id"] in by_msg else [])
        classify.apply_verdicts(conn, classify.verdicts_for(conn, msgs, response))


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_live_regression(conn, case):
    seed(conn, case)
    replay(conn, case)
    exp = case["expect"]

    events = [[r["application_id"], r["kind"]] for r in
              conn.execute("SELECT application_id, kind FROM events ORDER BY id")]
    assert events == exp["events"], case["found"]

    actions = conn.execute("SELECT * FROM actions ORDER BY id").fetchall()
    assert len(actions) == exp["actions"], case["found"]
    if "action_app" in exp:
        assert actions[0]["application_id"] == exp["action_app"]
    if "action_due_at_utc" in exp:
        assert actions[0]["due_at_utc"] == exp["action_due_at_utc"]
    if "action_reason_contains" in exp:
        assert exp["action_reason_contains"] in (actions[0]["reason"] or "")

    pending = conn.execute("SELECT reason FROM review_queue WHERE resolved = 0").fetchall()
    assert len(pending) == exp["pending_reviews"], [r["reason"] for r in pending]
    if "review_reason_contains" in exp:
        assert exp["review_reason_contains"] in pending[0]["reason"]
    if "ignored" in exp:
        n = conn.execute("SELECT COUNT(*) FROM review_queue WHERE resolved_note LIKE 'auto-ignored%'").fetchone()[0]
        assert n == exp["ignored"]
    if "candidates" in exp:
        got = [[r["company"], r["role"]] for r in
               conn.execute("SELECT company, role FROM application_candidates WHERE status = 'pending'")]
        assert got == exp["candidates"]

    unprocessed = conn.execute("SELECT COUNT(*) FROM raw_messages WHERE processed = 0").fetchone()[0]
    assert unprocessed == 0, "a message was left to be re-sent to the model every hour"


def test_fixture_uses_no_real_names():
    """The repository is public. Real cases must be anonymized before they land here."""
    text = (Path(__file__).parent / "fixtures/live_regressions.json").read_text().lower()
    for banned in ("ramp", "td bank", "td.com"):
        assert banned not in text
