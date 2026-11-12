#!/usr/bin/env python3
"""
Turn real mail into eval fixtures, and keep a frozen dev/holdout split.

Everything here reads and writes local files only. Nothing leaves this machine.

    # 1. Pull messages you haven't labeled yet into the label CSV
    python3 -m evals.dataset export --db ~/.jobtrack/jobs.db --db evals/raw/pool.db

    # 2. Label them in the browser
    python3 evals/label_server.py

    # 3. Build evals/fixtures.jsonl from every labeled row
    python3 -m evals.dataset build --db ~/.jobtrack/jobs.db --db evals/raw/pool.db

    # 4. Assign new fixtures to dev or holdout (existing assignments never move)
    python3 -m evals.dataset split

Label CSV rules:
    kind = ""      not labeled yet; skipped by `build`
    kind = "none"  labeled: not a status change (job alerts, talent-community
                   mail, scams, newsletters)
    kind = <kind>  labeled status change; company and role are required

The holdout set is for measuring, never for tuning. See docs/DESIGN.md (M1).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "evals/raw/label-me.csv"
DEFAULT_FIXTURES = ROOT / "evals/fixtures.jsonl"
DEFAULT_SPLIT = ROOT / "evals/raw/split.json"
DEFAULT_DB = Path.home() / ".jobtrack" / "jobs.db"

CSV_FIELDS = ["n", "date", "from", "subject", "preview", "kind", "company", "role", "gmail_id"]
PREVIEW_CHARS = 300
NONE_KIND = "none"
HOLDOUT_FRACTION = 0.2
DEFAULT_SEED = 20261112


# --------------------------------------------------------------------------
# CSV helpers
# --------------------------------------------------------------------------

def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    """Write via a temp file, then rename, so a crash never leaves half a CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def _messages(db_paths: Iterable[Path]) -> dict[str, sqlite3.Row]:
    """All raw messages across the given databases, first database wins."""
    found: dict[str, sqlite3.Row] = {}
    for p in db_paths:
        p = Path(p).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"database not found: {p}")
        # Read-only: exporting must never write to the live tracker database.
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            for r in conn.execute(
                "SELECT gmail_msg_id, received_on, sender, subject, body FROM raw_messages"
            ):
                found.setdefault(r["gmail_msg_id"], r)
        finally:
            conn.close()
    return found


def _preview(body: str | None) -> str:
    return " ".join((body or "").split())[:PREVIEW_CHARS]


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def export_unlabeled(db_paths: Iterable[Path], csv_path: Path) -> int:
    """Append messages not already in the CSV. Returns how many were added."""
    rows = _read_csv(csv_path)
    seen = {r["gmail_id"] for r in rows}
    next_n = max((int(r["n"]) for r in rows), default=0) + 1
    new = [m for gid, m in _messages(db_paths).items() if gid not in seen]
    new.sort(key=lambda m: (m["received_on"] or "", m["gmail_msg_id"]))
    for i, m in enumerate(new):
        rows.append({
            "n": str(next_n + i),
            "date": m["received_on"] or "",
            "from": m["sender"] or "",
            "subject": m["subject"] or "",
            "preview": _preview(m["body"]),
            "kind": "", "company": "", "role": "",
            "gmail_id": m["gmail_msg_id"],
        })
    if new or not csv_path.exists():
        _write_csv(csv_path, rows)
    return len(new)


def mark_blank_as_none(csv_path: Path, through_n: int) -> int:
    """One-time migration: rows labeled before "none" existed used a blank kind
    to mean "not a status change". Mark rows 1..through_n that way."""
    rows = _read_csv(csv_path)
    changed = 0
    for r in rows:
        if int(r["n"]) <= through_n and not r["kind"]:
            r["kind"] = NONE_KIND
            changed += 1
    if changed:
        _write_csv(csv_path, rows)
    return changed


# --------------------------------------------------------------------------
# Build fixtures
# --------------------------------------------------------------------------

def fixture_id(n: int) -> str:
    return f"real{n:02d}"


def build_fixtures(csv_path: Path, db_paths: Iterable[Path]) -> tuple[list[dict], int]:
    """Labeled CSV rows + message bodies -> fixtures. Returns (fixtures, skipped)."""
    messages = _messages(db_paths)
    fixtures: list[dict] = []
    skipped = 0
    for r in _read_csv(csv_path):
        kind = (r.get("kind") or "").strip()
        if not kind:
            skipped += 1
            continue
        n = int(r["n"])
        m = messages.get(r["gmail_id"])
        if m is None:
            raise ValueError(
                f"row {n}: message {r['gmail_id']} is not in any database given with --db")
        if kind == NONE_KIND:
            # Company is kept when given (e.g. a talent-community blast from a
            # known employer); it does not seed an application in the eval.
            expect: dict[str, Any] = {"kind": None,
                                      "company": (r.get("company") or "").strip() or None,
                                      "should_commit": False}
        else:
            company, role = (r.get("company") or "").strip(), (r.get("role") or "").strip()
            if not company or not role:
                raise ValueError(f"row {n}: kind {kind!r} needs both company and role")
            # Changes in M2 (DESIGN D3): terminal kinds will expect review, not commit.
            expect = {"kind": kind, "company": company, "role": role, "should_commit": True}
        fixtures.append({
            "id": fixture_id(n),
            "sender": m["sender"] or "",
            "subject": m["subject"] or "",
            "body": m["body"] or "",
            "expect": expect,
            "note": f"row {n}",
        })
    return fixtures, skipped


def write_fixtures(fixtures: list[dict], out: Path) -> None:
    tmp = out.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for fx in fixtures:
            f.write(json.dumps(fx, ensure_ascii=False) + "\n")
    tmp.replace(out)


# --------------------------------------------------------------------------
# Split
# --------------------------------------------------------------------------

def _stratum(fx: dict) -> str:
    return (fx.get("expect") or {}).get("kind") or NONE_KIND


def _rank(seed: int, fid: str) -> str:
    return hashlib.sha256(f"{seed}:{fid}".encode()).hexdigest()


def assign_split(fixtures: list[dict], path: Path, seed: int = DEFAULT_SEED, *,
                 rebuild: bool = False, force: bool = False,
                 fraction: float = HOLDOUT_FRACTION) -> dict:
    """Assign each fixture to dev or holdout, stratified by expected kind.

    Once a fixture is assigned it never moves, so the holdout stays frozen as
    the set grows. New fixtures are added per kind until that kind's holdout
    share reaches `fraction`. The seed is fixed when the split is created.
    Starting over needs both rebuild=True and force=True.
    """
    existing: dict | None = None
    if path.exists():
        if rebuild and not force:
            raise FileExistsError(
                f"{path} exists. Rebuilding moves fixtures between dev and holdout, "
                "which invalidates every earlier holdout number. Pass --force to confirm.")
        if not rebuild:
            existing = json.loads(path.read_text(encoding="utf-8"))

    if existing:
        seed = int(existing["seed"])
        fraction = float(existing.get("holdout_fraction", fraction))
        assignments: dict[str, str] = dict(existing["assignments"])
        created = existing.get("created_at")
    else:
        assignments, created = {}, None

    by_kind: dict[str, list[str]] = defaultdict(list)
    for fx in fixtures:
        by_kind[_stratum(fx)].append(fx["id"])

    for _kind, ids in by_kind.items():
        target = int(round(fraction * len(ids)))
        have = sum(1 for i in ids if assignments.get(i) == "holdout")
        new = sorted((i for i in ids if i not in assignments), key=lambda i: _rank(seed, i))
        need = max(0, target - have)
        for i, fid in enumerate(new):
            assignments[fid] = "holdout" if i < need else "dev"

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    split = {
        "version": 1,
        "seed": seed,
        "holdout_fraction": fraction,
        "created_at": created or now,
        "updated_at": now,
        "assignments": dict(sorted(assignments.items())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(split, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return split


def select_set(fixtures: list[dict], split: dict, which: str) -> list[dict]:
    if which == "all":
        return fixtures
    if which not in ("dev", "holdout"):
        raise ValueError(f"unknown set {which!r}")
    assignments = split["assignments"]
    missing = [fx["id"] for fx in fixtures if fx["id"] not in assignments]
    if missing:
        raise ValueError(
            f"fixtures not assigned to dev/holdout: {', '.join(missing[:10])}. "
            "Run `python3 -m evals.dataset split` first.")
    return [fx for fx in fixtures if assignments[fx["id"]] == which]


def load_split(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"error: {path} does not exist. Run `python3 -m evals.dataset split`.")
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _summary(fixtures: list[dict], split: dict | None = None) -> str:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for fx in fixtures:
        side = 1 if split and split["assignments"].get(fx["id"]) == "holdout" else 0
        counts[_stratum(fx)][side] += 1
    lines = [f"{'kind':<12}{'total':>7}{'dev':>6}{'holdout':>9}"]
    for k in sorted(counts):
        dev, hold = counts[k]
        lines.append(f"{k:<12}{dev + hold:>7}{dev:>6}{hold:>9}")
    total_hold = sum(v[1] for v in counts.values())
    lines.append(f"{'TOTAL':<12}{len(fixtures):>7}{len(fixtures) - total_hold:>6}{total_hold:>9}")
    return "\n".join(lines)


def _load_fixtures(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="jt eval dataset tools (local only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def dbs(s):
        s.add_argument("--db", action="append", type=Path,
                       help=f"database(s) holding raw_messages (default {DEFAULT_DB})")

    s = sub.add_parser("export", help="append unlabeled messages to the label CSV")
    dbs(s)
    s.add_argument("--csv", type=Path, default=DEFAULT_CSV)

    s = sub.add_parser("build", help="build fixtures.jsonl from labeled CSV rows")
    dbs(s)
    s.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    s.add_argument("--out", type=Path, default=DEFAULT_FIXTURES)

    s = sub.add_parser("split", help="assign new fixtures to dev/holdout")
    s.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    s.add_argument("--split", type=Path, default=DEFAULT_SPLIT)
    s.add_argument("--seed", type=int, default=DEFAULT_SEED)
    s.add_argument("--rebuild", action="store_true")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("mark-none", help="one-time: blank kind -> none for rows 1..N")
    s.add_argument("through_n", type=int)
    s.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    return p


def main(argv: list[str] | None = None) -> None:
    a = build_parser().parse_args(argv)
    if a.cmd == "export":
        added = export_unlabeled(a.db or [DEFAULT_DB], a.csv)
        total = len(_read_csv(a.csv))
        unlabeled = sum(1 for r in _read_csv(a.csv) if not r["kind"])
        print(f"added {added} messages; {total} rows, {unlabeled} not labeled yet -> {a.csv}")
    elif a.cmd == "build":
        try:
            fixtures, skipped = build_fixtures(a.csv, a.db or [DEFAULT_DB])
        except ValueError as e:
            raise SystemExit(f"error: {e}")
        write_fixtures(fixtures, a.out)
        print(f"wrote {len(fixtures)} fixtures to {a.out}; skipped {skipped} unlabeled rows")
        print(_summary(fixtures))
    elif a.cmd == "split":
        fixtures = _load_fixtures(a.fixtures)
        try:
            split = assign_split(fixtures, a.split, a.seed, rebuild=a.rebuild, force=a.force)
        except FileExistsError as e:
            raise SystemExit(f"error: {e}")
        print(f"split saved to {a.split} (seed {split['seed']})")
        print(_summary(fixtures, split))
    elif a.cmd == "mark-none":
        print(f"marked {mark_blank_as_none(a.csv, a.through_n)} rows as none")


if __name__ == "__main__":
    main()
