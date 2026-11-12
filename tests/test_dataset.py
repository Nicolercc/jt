"""M1: label export, fixture building, and the frozen dev/holdout split."""

import csv
import json
import sqlite3

import pytest

from evals import dataset


FIELDS = ["n", "date", "from", "subject", "preview", "kind", "company", "role", "gmail_id"]


def make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE raw_messages (gmail_msg_id TEXT PRIMARY KEY,
        received_on DATE, sender TEXT, subject TEXT, body TEXT,
        processed INTEGER NOT NULL DEFAULT 0)""")
    conn.executemany(
        "INSERT INTO raw_messages VALUES (?,?,?,?,?,0)", rows)
    conn.commit()
    conn.close()


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def row(n, gid, kind="", company="", role=""):
    return {"n": str(n), "date": "2026-08-11", "from": "a@example.com",
            "subject": f"s{n}", "preview": "p", "kind": kind,
            "company": company, "role": role, "gmail_id": gid}


# ---------------------------------------------------------------- export

def test_export_appends_only_new_messages_in_date_order(tmp_path):
    db = tmp_path / "pool.db"
    make_db(db, [
        ("g1", "2026-08-11", "a@x.com", "old", "already labeled"),
        ("g3", "2026-08-13", "c@x.com", "later", "body   three\n\nwith  spaces"),
        ("g2", "2026-08-12", "b@x.com", "earlier", "body two"),
    ])
    out = tmp_path / "label-me.csv"
    write_csv(out, [row(1, "g1", "confirmed", "A", "R")])

    added = dataset.export_unlabeled([db], out)

    rows = read_csv(out)
    assert added == 2
    assert [r["gmail_id"] for r in rows] == ["g1", "g2", "g3"]
    assert [r["n"] for r in rows] == ["1", "2", "3"]
    assert rows[0]["kind"] == "confirmed"          # existing labels untouched
    assert rows[2]["preview"] == "body three with spaces"
    assert rows[2]["kind"] == ""                   # new rows start unlabeled


def test_export_is_idempotent_and_dedupes_across_databases(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    make_db(a, [("g1", "2026-08-11", "x", "s", "b")])
    make_db(b, [("g1", "2026-08-11", "x", "s", "b"), ("g2", "2026-08-12", "y", "t", "c")])
    out = tmp_path / "label-me.csv"

    assert dataset.export_unlabeled([a, b], out) == 2
    assert dataset.export_unlabeled([a, b], out) == 0
    assert len(read_csv(out)) == 2


def test_export_preview_is_capped(tmp_path):
    db = tmp_path / "pool.db"
    make_db(db, [("g1", "2026-08-11", "x", "s", "word " * 500)])
    out = tmp_path / "label-me.csv"
    dataset.export_unlabeled([db], out)
    assert len(read_csv(out)[0]["preview"]) <= dataset.PREVIEW_CHARS


# ---------------------------------------------------------------- build

def test_build_skips_unlabeled_rows_and_maps_none(tmp_path):
    db = tmp_path / "pool.db"
    make_db(db, [
        ("g1", "2026-08-11", "a@x.com", "s1", "body one"),
        ("g2", "2026-08-12", "b@x.com", "s2", "body two"),
        ("g3", "2026-08-13", "c@x.com", "s3", "body three"),
    ])
    csv_path = tmp_path / "label-me.csv"
    write_csv(csv_path, [
        row(1, "g1", "confirmed", "Acme", "Engineer"),
        row(2, "g2", "none", "Ignored Co", "Ignored role"),
        row(3, "g3"),                                   # not labeled yet
    ])

    fixtures, skipped = dataset.build_fixtures(csv_path, [db])

    assert skipped == 1
    assert [f["id"] for f in fixtures] == ["real01", "real02"]
    assert fixtures[0] == {
        "id": "real01", "sender": "a@x.com", "subject": "s1", "body": "body one",
        "expect": {"kind": "confirmed", "company": "Acme", "role": "Engineer",
                   "should_commit": True},
        "note": "row 1",
    }
    assert fixtures[1]["expect"] == {"kind": None, "company": "Ignored Co", "should_commit": False}


def test_build_refuses_status_label_without_company_and_role(tmp_path):
    db = tmp_path / "pool.db"
    make_db(db, [("g1", "2026-08-11", "a", "s", "b")])
    csv_path = tmp_path / "label-me.csv"
    write_csv(csv_path, [row(1, "g1", "rejected", "Acme", "")])
    with pytest.raises(ValueError, match="row 1"):
        dataset.build_fixtures(csv_path, [db])


def test_build_refuses_message_missing_from_every_db(tmp_path):
    db = tmp_path / "pool.db"
    make_db(db, [])
    csv_path = tmp_path / "label-me.csv"
    write_csv(csv_path, [row(1, "gone", "confirmed", "Acme", "Engineer")])
    with pytest.raises(ValueError, match="gone"):
        dataset.build_fixtures(csv_path, [db])


def test_mark_legacy_blank_rows_as_none(tmp_path):
    csv_path = tmp_path / "label-me.csv"
    write_csv(csv_path, [row(1, "g1"), row(2, "g2", "confirmed", "A", "R"), row(3, "g3")])
    assert dataset.mark_blank_as_none(csv_path, through_n=2) == 1
    kinds = [r["kind"] for r in read_csv(csv_path)]
    assert kinds == ["none", "confirmed", ""]       # row 3 is past the cutoff


# ---------------------------------------------------------------- split

def fx(i, kind):
    return {"id": f"real{i:02d}", "expect": {"kind": kind}}


def mixed(n_conf=40, n_rej=10, n_none=10):
    out, i = [], 1
    for kind, count in (("confirmed", n_conf), ("rejected", n_rej), (None, n_none)):
        for _ in range(count):
            out.append(fx(i, kind))
            i += 1
    return out


def test_split_is_stratified_by_kind(tmp_path):
    split = dataset.assign_split(mixed(), tmp_path / "split.json", seed=7)
    hold = [k for k, v in split["assignments"].items() if v == "holdout"]
    by_kind = {"confirmed": 0, "rejected": 0, "none": 0}
    lookup = {f["id"]: (f["expect"]["kind"] or "none") for f in mixed()}
    for h in hold:
        by_kind[lookup[h]] += 1
    assert by_kind == {"confirmed": 8, "rejected": 2, "none": 2}


def test_split_is_deterministic_for_a_seed(tmp_path):
    a = dataset.assign_split(mixed(), tmp_path / "a.json", seed=7)
    b = dataset.assign_split(mixed(), tmp_path / "b.json", seed=7)
    assert a["assignments"] == b["assignments"]


def test_split_never_moves_existing_assignments(tmp_path):
    path = tmp_path / "split.json"
    first = dataset.assign_split(mixed(20, 5, 5), path, seed=7)
    grown = dataset.assign_split(mixed(40, 10, 10), path, seed=999)  # seed ignored once frozen

    for fid, side in first["assignments"].items():
        assert grown["assignments"][fid] == side
    assert grown["seed"] == 7
    hold = sum(1 for v in grown["assignments"].values() if v == "holdout")
    assert hold == 12                               # still 20% overall


def test_split_force_rebuild_requires_flag(tmp_path):
    path = tmp_path / "split.json"
    dataset.assign_split(mixed(), path, seed=7)
    with pytest.raises(FileExistsError):
        dataset.assign_split(mixed(), path, seed=8, rebuild=True)
    rebuilt = dataset.assign_split(mixed(), path, seed=8, rebuild=True, force=True)
    assert rebuilt["seed"] == 8


def test_select_set_filters_and_rejects_unassigned(tmp_path):
    path = tmp_path / "split.json"
    fixtures = mixed(10, 0, 0)
    dataset.assign_split(fixtures, path, seed=7)
    split = json.loads(path.read_text())

    dev = dataset.select_set(fixtures, split, "dev")
    hold = dataset.select_set(fixtures, split, "holdout")
    assert len(dev) + len(hold) == 10 and len(hold) == 2
    assert dataset.select_set(fixtures, split, "all") == fixtures

    with pytest.raises(ValueError, match="real99"):
        dataset.select_set(fixtures + [fx(99, "confirmed")], split, "dev")
