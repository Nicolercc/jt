"""Audit finding: the live application_candidates table had an older,
incompatible shape (title/url_key/captured_on...). CREATE TABLE IF NOT EXISTS
never fixes that, so the first email from an unknown company would crash the
classifier batch every hour. connect() must repair it."""

import sqlite3

import pytest

import classify
import jt

OLD_SHAPE = """CREATE TABLE application_candidates (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, company TEXT, role TEXT,
    url TEXT NOT NULL, company_key TEXT NOT NULL, role_key TEXT NOT NULL,
    url_key TEXT NOT NULL, captured_on DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', application_id INTEGER,
    rejected_note TEXT, resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')))"""


def old_db(path, rows=0):
    c = sqlite3.connect(path)
    c.execute(OLD_SHAPE)
    c.execute("CREATE UNIQUE INDEX idx_application_candidates_url_unique ON application_candidates(url)")
    for i in range(rows):
        c.execute("INSERT INTO application_candidates (title, url, company_key, role_key, url_key, captured_on) "
                  "VALUES (?, ?, 'k', 'r', ?, '2026-09-01')", (f"t{i}", f"https://x/{i}", f"u{i}"))
    c.commit()
    c.close()


def cols(conn):
    return {r["name"] for r in conn.execute("PRAGMA table_info(application_candidates)")}


def test_empty_old_shaped_table_is_rebuilt(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    old_db(db)
    monkeypatch.setenv("JT_DB", str(db))
    c = jt.connect()
    assert {"raw_input", "source_ref", "accepted_application_id"} <= cols(c)
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'application_candidates_legacy'").fetchone()
    c.close()


def test_old_rows_are_kept_in_a_legacy_table_never_dropped(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    old_db(db, rows=2)
    monkeypatch.setenv("JT_DB", str(db))
    c = jt.connect()
    assert "raw_input" in cols(c)
    assert c.execute("SELECT COUNT(*) FROM application_candidates_legacy").fetchone()[0] == 2
    c.close()


def test_repair_runs_once(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    old_db(db, rows=1)
    monkeypatch.setenv("JT_DB", str(db))
    jt.connect().close()
    c = jt.connect()
    assert c.execute("SELECT COUNT(*) FROM application_candidates_legacy").fetchone()[0] == 1
    c.close()


def test_classifier_can_record_a_candidate_after_repair(tmp_path, monkeypatch):
    """The exact path that would have crashed in production."""
    db = tmp_path / "jobs.db"
    old_db(db)
    monkeypatch.setenv("JT_DB", str(db))
    c = jt.connect()
    with jt.write_txn(c):
        c.execute("INSERT INTO applications (company, role, lane, applied_on) VALUES ('Northstar','CX','ops','2026-08-01')")
        c.execute("INSERT INTO raw_messages (gmail_msg_id, received_on, sender, subject, body) VALUES "
                  "('n1', '2026-11-14', 'no-reply@greenhouse.io', 'Thanks', 'We received your application.')")
    import json
    p = {"gmail_msg_id": "n1", "company": "Figma", "role_hint": "Designer", "kind": "confirmed",
         "occurred_on": "2026-11-14", "confidence": 0.97, "evidence": "We received your application."}
    msgs = classify.load_unprocessed(c, 10)
    classify.apply_verdicts(c, classify.verdicts_for(c, msgs, json.dumps([p])))
    assert c.execute("SELECT company FROM application_candidates").fetchone()[0] == "Figma"
    c.close()
