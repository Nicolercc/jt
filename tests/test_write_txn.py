"""DESIGN D6: every write path takes the writer lock before it reads the state
it is about to act on."""

import sqlite3
import threading
import time

import pytest

import classify
import jt


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    conn = jt.connect()
    conn.execute(
        "INSERT INTO applications (company, role, lane, applied_on) VALUES (?,?,?,?)",
        ("Acme", "Backend Engineer", "swe", "2026-08-01"))
    conn.execute(
        "INSERT INTO raw_messages (gmail_msg_id, received_on, sender, subject, body) "
        "VALUES ('m1', '2026-08-05', 'r@acme.com', 'Next steps', "
        "'Do you have 30 minutes this week for an intro call?')")
    conn.commit()
    yield conn
    conn.close()


def _events(conn):
    return [r["kind"] for r in conn.execute("SELECT kind FROM events ORDER BY id")]


# ---------------------------------------------------------------- helper

def test_write_txn_commits_on_success(db):
    with jt.write_txn(db):
        db.execute("INSERT INTO events (application_id, occurred_on, kind) VALUES (1, '2026-08-02', 'screen')")
    other = jt.connect()
    assert _events(other) == ["screen"]
    other.close()


@pytest.mark.parametrize("exc", [RuntimeError("boom"), SystemExit("error: stop")])
def test_write_txn_rolls_back_on_any_exception_including_sys_exit(db, exc):
    with pytest.raises(type(exc)):
        with jt.write_txn(db):
            db.execute("INSERT INTO events (application_id, occurred_on, kind) VALUES (1, '2026-08-02', 'screen')")
            raise exc
    assert _events(db) == []
    assert not db.in_transaction


def test_write_txn_restores_isolation_level(db):
    before = db.isolation_level
    with jt.write_txn(db):
        assert db.isolation_level is None
    assert db.isolation_level == before


def test_write_txn_refuses_to_nest_inside_an_open_transaction(db):
    db.execute("UPDATE raw_messages SET processed = 0")   # opens an implicit txn
    assert db.in_transaction
    with pytest.raises(RuntimeError, match="already open"):
        with jt.write_txn(db):
            pass
    db.rollback()


# ---------------------------------------------------------------- the race

def _screen_verdict():
    return classify.Verdict(
        gmail_msg_id="m1", ok=True, reason="",
        proposal={"gmail_msg_id": "m1", "company": "Acme", "role_hint": "Backend Engineer",
                  "kind": "screen", "occurred_on": "2026-08-05", "confidence": 0.97,
                  "evidence": "Do you have 30 minutes this week"},
        application_id=1)


def test_classifier_rechecks_inside_the_lock_when_another_writer_races_it(db):
    """Writer A marks the application rejected and holds the lock. The
    classifier, validated against the pre-race state (applied -> screen is
    legal), tries to commit 'screen' while A holds the lock. Exactly one must
    win: 'rejected' is committed, 'screen' goes to review with a recheck
    reason, and the classifier never errors out."""
    a_has_lock = threading.Event()
    release_a = threading.Event()
    b_ready = threading.Event()
    b_go = threading.Event()
    errors: list[BaseException] = []
    result: dict = {}

    def writer_a():
        c = jt.connect()
        try:
            with jt.write_txn(c):
                c.execute("INSERT INTO events (application_id, occurred_on, kind) "
                          "VALUES (1, '2026-08-04', 'rejected')")
                a_has_lock.set()
                release_a.wait(5)
        except BaseException as e:   # pragma: no cover - reported below
            errors.append(e)
        finally:
            c.close()

    def classifier_b():
        # Open BEFORE A takes the lock: jt.connect() itself writes schema
        # metadata, so connecting later would just wait for A and hide the race.
        c = jt.connect()
        b_ready.set()
        b_go.wait(5)
        try:
            result["counts"] = classify.apply_verdicts(c, [_screen_verdict()])
        except BaseException as e:
            errors.append(e)
        finally:
            c.close()

    tb = threading.Thread(target=classifier_b)
    tb.start()
    assert b_ready.wait(5)
    ta = threading.Thread(target=writer_a)
    ta.start()
    assert a_has_lock.wait(5)
    b_go.set()
    time.sleep(0.3)          # let B reach its lock attempt while A still holds it
    release_a.set()
    ta.join(10)
    tb.join(10)

    assert errors == []
    assert result["counts"] == (0, 1)
    fresh = jt.connect()
    assert _events(fresh) == ["rejected"]
    reasons = [r["reason"] for r in fresh.execute("SELECT reason FROM review_queue")]
    assert len(reasons) == 1 and "rejected is terminal" in reasons[0]
    assert fresh.execute("SELECT processed FROM raw_messages").fetchone()[0] == 1
    fresh.close()


def test_jt_log_rechecks_legality_inside_the_lock(db, monkeypatch):
    """`jt log` checks the transition and inserts in one locked transaction:
    if the application went terminal first, the manual log is refused."""
    with jt.write_txn(db):
        db.execute("INSERT INTO events (application_id, occurred_on, kind) VALUES (1, '2026-08-04', 'rejected')")
    args = type("A", (), {"id": 1, "kind": "screen", "force": False, "on": "2026-08-05", "note": None})()
    with pytest.raises(SystemExit, match="terminal"):
        jt.cmd_log(db, args)
    assert _events(db) == ["rejected"]
    assert not db.in_transaction


def test_sync_insert_and_watermark_hold_the_lock(db):
    from sync import RawMessage, insert_rows
    with jt.write_txn(db):
        n = insert_rows(db, [RawMessage("m2", "2026-08-06", "x", "s", "b")])
    assert n == 1
    assert not db.in_transaction


def test_busy_writer_times_out_instead_of_hanging(db, tmp_path):
    """A second writer waits for the lock, then fails loudly rather than
    reading stale state. Uses a short timeout so the test is fast."""
    holder = jt.connect()
    waiter = sqlite3.connect(jt.db_path(), timeout=0.2)
    try:
        with jt.write_txn(holder):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                with jt.write_txn(waiter):
                    pass
        assert not waiter.in_transaction
    finally:
        waiter.close()
        holder.close()
