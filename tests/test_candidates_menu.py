"""PRD P0 (audit): a 'thanks for applying' email for a company jt doesn't know
becomes a candidate, and candidates were invisible outside `jt candidates`."""

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


def candidate(conn, company="Figma", role="Product Designer", ref="gmail:f1"):
    cid, _ = jt.insert_application_candidate(conn, raw_input="{}", source="classifier",
                                             source_ref=ref, company=company, role=role,
                                             applied_on="2026-11-14")
    return cid


def snap(conn):
    return jt.today_snapshot(conn, date(2026, 11, 14), datetime(2026, 11, 14, 12, tzinfo=NY), NY)


def test_today_json_lists_pending_candidates_and_counts_them(conn):
    cid = candidate(conn)
    s = snap(conn)
    assert s["candidates"] == [{"candidate_id": cid, "company": "Figma", "role": "Product Designer",
                                "applied_on": "2026-11-14", "can_add": True}]
    assert s["badge"]["candidates"] == 1


def test_candidate_without_role_cannot_be_added_in_one_click(conn):
    candidate(conn, role=None)
    assert snap(conn)["candidates"][0]["can_add"] is False


def test_accept_and_reject_speak_json(conn, capsys):
    a, b = candidate(conn), candidate(conn, company="Acme", role="PM", ref="gmail:a1")
    jt.main_args(["candidates", "accept", str(a), "--lane", "comms", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["application_id"]
    app = conn.execute("SELECT company, role, lane FROM applications").fetchone()
    assert tuple(app) == ("Figma", "Product Designer", "comms")
    jt.main_args(["candidates", "reject", str(b), "--note", "not mine", "--json"])
    assert json.loads(capsys.readouterr().out)["ok"]
    assert snap(conn)["candidates"] == []


def test_accepting_twice_is_safe(conn, capsys):
    a = candidate(conn)
    jt.main_args(["candidates", "accept", str(a), "--lane", "ops", "--json"])
    first = json.loads(capsys.readouterr().out)
    jt.main_args(["candidates", "accept", str(a), "--lane", "ops", "--json"])
    again = json.loads(capsys.readouterr().out)
    assert again["ok"] and again["application_id"] == first["application_id"]
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
