"""PRD R12: a reminder email for a task you already have updates that task
instead of adding a duplicate (real case: CodeSignal invite + reminder)."""

import pytest

import classify
import jt


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("JT_TZ", "America/Los_Angeles")
    c = jt.connect()
    with jt.write_txn(c):
        c.execute("INSERT INTO applications (id, company, role, lane, applied_on) "
                  "VALUES (10, 'Northstar', 'Support Agent', 'ops', '2026-08-28')")
    yield c
    c.close()


def email_task(conn, msg_id, received, description, body="", due_text="", due_iso="", due_tz=""):
    with jt.write_txn(conn):
        conn.execute("INSERT INTO raw_messages (gmail_msg_id, received_on, sender, subject, body) "
                     "VALUES (?, ?, 'no-reply@codesignal.com', 'Assessment', ?)", (msg_id, received, body))
        classify.write_request(conn, 10, {
            "gmail_msg_id": msg_id, "occurred_on": received,
            "request": {"description": description, "due_text": due_text,
                        "due_iso": due_iso, "due_tz": due_tz}}, None)


def actions(conn):
    return conn.execute("SELECT * FROM actions ORDER BY id").fetchall()


def test_reminder_for_an_open_task_does_not_duplicate_it(conn):
    email_task(conn, "invite", "2026-08-28", "Complete Northstar Assessment on CodeSignal")
    email_task(conn, "reminder", "2026-09-02", "Complete Northstar Assessment on CodeSignal")
    rows = actions(conn)
    assert len(rows) == 1
    assert "later email (2026-09-02)" in rows[0]["reason"]


def test_reminder_with_an_explicit_deadline_updates_the_task(conn):
    email_task(conn, "invite", "2026-08-28", "Complete Northstar assessment on CodeSignal")
    email_task(conn, "reminder", "2026-09-02", "Complete the Northstar assessment on CodeSignal",
               body="Please finish by Sep 4, 5:00 PM PT.", due_text="by Sep 4, 5:00 PM PT",
               due_iso="2026-09-04T17:00", due_tz="America/Los_Angeles")
    [row] = actions(conn)
    assert row["due_at_utc"] == "2026-09-05T00:00:00Z" and row["due_date"] is None
    assert "deadline from a later email" in row["reason"]


def test_reminder_without_a_deadline_keeps_the_existing_deadline(conn):
    email_task(conn, "invite", "2026-08-28", "Complete Northstar assessment",
               body="Finish by Sep 4, 5:00 PM PT.", due_text="by Sep 4, 5:00 PM PT",
               due_iso="2026-09-04T17:00", due_tz="America/Los_Angeles")
    email_task(conn, "reminder", "2026-09-02", "Complete Northstar assessment")
    [row] = actions(conn)
    assert row["due_at_utc"] == "2026-09-05T00:00:00Z"


def test_reminder_for_a_recently_closed_task_is_skipped(conn):
    email_task(conn, "invite", "2026-08-28", "Complete Northstar assessment")
    with jt.write_txn(conn):
        conn.execute("UPDATE actions SET state = 'done', closed_at = '2026-09-01 10:00:00'")
    email_task(conn, "reminder", "2026-09-02", "Complete Northstar assessment")
    assert len(actions(conn)) == 1


def test_a_different_task_is_still_its_own_task(conn):
    email_task(conn, "invite", "2026-08-28", "Complete Northstar assessment")
    email_task(conn, "onsite", "2026-09-10", "Schedule your onsite interview")
    assert len(actions(conn)) == 2


def test_same_task_at_another_application_is_separate(conn):
    with jt.write_txn(conn):
        conn.execute("INSERT INTO applications (id, company, role, lane, applied_on) "
                     "VALUES (11, 'Acme', 'Engineer', 'swe', '2026-08-28')")
    email_task(conn, "invite", "2026-08-28", "Complete assessment")
    with jt.write_txn(conn):
        conn.execute("INSERT INTO raw_messages (gmail_msg_id, received_on, sender, subject, body) "
                     "VALUES ('acme', '2026-09-02', 'x', 's', '')")
        classify.write_request(conn, 11, {"gmail_msg_id": "acme", "occurred_on": "2026-09-02",
                                          "request": {"description": "Complete assessment", "due_text": "",
                                                      "due_iso": "", "due_tz": ""}}, None)
    assert len(actions(conn)) == 2
