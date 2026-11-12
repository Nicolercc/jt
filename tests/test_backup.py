"""DESIGN D7: backups that don't overwrite, are written atomically, can be
verified, and are honest about holding raw email."""

import sqlite3
from pathlib import Path

import pytest

import jt

SECRET = "PRIVATE-RECRUITER-NOTE-7f3a"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "live" / "jobs.db"))
    c = jt.connect()
    with jt.write_txn(c):
        c.execute("INSERT INTO applications (company, role, lane, applied_on) "
                  "VALUES ('Acme', 'Engineer', 'swe', '2026-08-01')")
        c.execute("INSERT INTO raw_messages (gmail_msg_id, received_on, sender, subject, body) "
                  "VALUES ('m1', '2026-08-02', 'r@acme.com', 'Hi', ?)", (f"body {SECRET} body",))
    yield c
    c.close()


def backup(conn, dest, capsys, *extra):
    args = type("A", (), {"dir": str(dest), "no_bodies": "--no-bodies" in extra})()
    jt.cmd_backup(conn, args)
    return capsys.readouterr()


def test_two_backups_on_one_day_do_not_overwrite(conn, tmp_path, capsys, monkeypatch):
    stamps = iter(["2026-11-12T090000", "2026-11-12T090001"])
    monkeypatch.setattr(jt, "_backup_stamp", lambda: next(stamps))
    backup(conn, tmp_path / "b", capsys)
    backup(conn, tmp_path / "b", capsys)
    names = sorted(p.name for p in (tmp_path / "b").iterdir())
    assert names == ["jobs-2026-11-12T090000.csv", "jobs-2026-11-12T090000.db",
                     "jobs-2026-11-12T090001.csv", "jobs-2026-11-12T090001.db"]


def test_backup_refuses_to_overwrite_an_existing_snapshot(conn, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(jt, "_backup_stamp", lambda: "2026-11-12T090000")
    backup(conn, tmp_path / "b", capsys)
    with pytest.raises(SystemExit, match="already exists"):
        backup(conn, tmp_path / "b", capsys)


def test_backup_leaves_no_temp_files(conn, tmp_path, capsys):
    backup(conn, tmp_path / "b", capsys)
    assert not [p for p in (tmp_path / "b").iterdir() if ".tmp" in p.name]


def test_default_backup_keeps_email_bodies(conn, tmp_path, capsys):
    backup(conn, tmp_path / "b", capsys)
    db = next((tmp_path / "b").glob("*.db"))
    body = sqlite3.connect(db).execute("SELECT body FROM raw_messages").fetchone()[0]
    assert SECRET in body


def test_no_bodies_backup_removes_bodies_from_the_file_itself(conn, tmp_path, capsys):
    """Setting the column to NULL is not enough: SQLite keeps old bytes in
    free pages until VACUUM. Check the raw file, not just the query."""
    backup(conn, tmp_path / "b", capsys, "--no-bodies")
    db = next((tmp_path / "b").glob("*.db"))
    assert sqlite3.connect(db).execute("SELECT body FROM raw_messages").fetchone()[0] is None
    assert SECRET.encode() not in db.read_bytes()
    # Everything else survives.
    assert sqlite3.connect(db).execute("SELECT subject FROM raw_messages").fetchone()[0] == "Hi"


@pytest.mark.parametrize("parts", [
    ("Dropbox", "jt"),
    ("Library", "Mobile Documents", "com~apple~CloudDocs", "jt"),
    ("Library", "CloudStorage", "GoogleDrive-x", "jt"),
    ("OneDrive", "jt"),
])
def test_cloud_synced_destination_warns(conn, tmp_path, capsys, parts):
    out = backup(conn, tmp_path.joinpath(*parts), capsys)
    assert "raw email bodies" in out.err and "cloud" in out.err


def test_local_destination_does_not_warn(conn, tmp_path, capsys):
    assert backup(conn, tmp_path / "backups", capsys).err == ""


def test_no_bodies_to_cloud_does_not_warn_about_bodies(conn, tmp_path, capsys):
    out = backup(conn, tmp_path / "Dropbox" / "jt", capsys, "--no-bodies")
    assert "raw email bodies" not in out.err


def test_verify_reports_counts_for_a_good_backup(conn, tmp_path, capsys):
    backup(conn, tmp_path / "b", capsys)
    db = next((tmp_path / "b").glob("*.db"))
    jt.cmd_verify_backup(conn, type("A", (), {"file": str(db)})())
    out = capsys.readouterr().out
    assert "integrity ok" in out
    assert "applications" in out and "1" in out
    assert "with bodies: 1" in out


def test_verify_fails_loudly_on_a_corrupt_file(conn, tmp_path):
    bad = tmp_path / "jobs-bad.db"
    bad.write_bytes(b"this is not a database" * 100)
    with pytest.raises(SystemExit):
        jt.cmd_verify_backup(conn, type("A", (), {"file": str(bad)})())


def test_verify_does_not_modify_the_backup(conn, tmp_path, capsys):
    backup(conn, tmp_path / "b", capsys)
    db = next((tmp_path / "b").glob("*.db"))
    before = db.read_bytes()
    jt.cmd_verify_backup(conn, type("A", (), {"file": str(db)})())
    assert db.read_bytes() == before
