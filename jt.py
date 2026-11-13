#!/usr/bin/env python3
"""
jt - job application tracker

Single file. Standard library only. SQLite backed. Local first.

INVARIANTS. These are enforced by the database, not by convention, and an
agent working in this repo must not remove them:

  1. `events` is APPEND-ONLY. SQLite triggers abort any UPDATE or DELETE.
     Correcting a mistake means appending a corrective event. The mistake
     stays in history, which is the point.

  2. `applications` has NO status column. Status is DERIVED from the event
     log on every read. There is no status field for a classifier to write
     to, so a bad classification can only ever propose -- never commit.

  3. `events.gmail_msg_id` is UNIQUE. The DATABASE owns idempotency, not
     the Python, and definitely not the model. This is what lets Gmail sync
     be deliberately sloppy about its cursor: overlap the window, let the
     constraint eat the duplicates.

  4. `events.kind` is a foreign key into `event_kinds`. A CHECK constraint
     gives the same guarantee but cannot be altered in SQLite without a
     table rebuild -- painful once append-only triggers exist.

  5. `events.application_id` is ON DELETE RESTRICT. An application can be
     deleted only while it has zero events (see `jt rm`). Past the first
     event it is history, and history does not get erased.

  6. Every write goes through `write_txn()` (BEGIN IMMEDIATE), and any state
     a write depends on is read INSIDE that block. Checking legality first and
     locking later lets two writers both pass the check. The only exemption is
     idempotent schema setup in `connect()`. Never call a model or the network
     while holding the lock.

TRAP FOR AGENTS AND FUTURE ME: `INSERT OR REPLACE` is internally a DELETE plus
an INSERT. SQLite only fires the delete trigger for it when
`PRAGMA recursive_triggers = ON`, which is OFF by default -- so on a default
connection, OR REPLACE punches a silent hole straight through invariant 1.
`connect()` turns the pragma on. Any code that opens this database WITHOUT
going through `connect()` reopens that hole. Always use `INSERT OR IGNORE`
on `events`, and always open the database through `connect()`.

Usage:
    jt add "Stripe" "Backend Engineer" --lane swe
    jt bulk < today.txt
    jt list
    jt stale
    jt log 3 screen --on 2026-08-19
    jt show 3
    jt export --lane swe --out pursuit.csv
    jt backup ~/jobtrack-backups
"""

import argparse
import contextlib
import csv
import hashlib
import html
import json
import os
import sqlite3
import sys
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from transitions import is_legal


_TYPOGRAPHY = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u200b": None, "\u200c": None, "\u200d": None, "\ufeff": None,
})


def normalize_for_match(text: str) -> str:
    """Canonical form for the verbatim-evidence check.

    Real ATS mail carries HTML entities (&nbsp;), curly quotes, and hard line
    wraps mid-sentence, while the model quotes the rendered text. Normalising
    both sides the same way removes formatting differences only: the words and
    their order must still appear in the body, so an invented quote still fails.
    """
    text = unicodedata.normalize("NFKC", html.unescape(text)).translate(_TYPOGRAPHY)
    return " ".join(text.split()).casefold()


def evidence_in_body(evidence: str, body: str | None) -> bool:
    needle = normalize_for_match(evidence)
    return bool(needle) and needle in normalize_for_match(body or "")

SCHEMA_VERSION = 6

# --------------------------------------------------------------------------
# Domain vocabulary. You own this. Changing it is a schema-level decision.
# --------------------------------------------------------------------------

LANES = ("swe", "ops", "comms")

# Stage events advance the pipeline. Higher rank = further along.
STAGE_RANK = {
    "applied": 0,
    "confirmed": 1,   # ATS acknowledged receipt -- a robot, not a human
    "response": 2,    # a human replied
    "screen": 3,      # recruiter screen
    "tech": 4,        # technical / take-home
    "onsite": 5,
    "final": 6,
    "offer": 7,
}

# Terminal events end the story. The most recent one wins outright.
TERMINAL = ("rejected", "withdrawn", "accepted", "ghosted")

# Touch events reset the staleness clock but do not change stage.
TOUCH = ("followup_sent", "thankyou_sent", "note")

# Evidence a human on the other side engaged. Feeds Phase 4 scoring.
INBOUND = ("response", "screen", "tech", "onsite", "final", "offer",
           "rejected", "accepted")

EVENT_KINDS = tuple(k for k in STAGE_RANK if k != "applied") + TERMINAL + TOUCH

# Follow-up policy. One nudge, then let it go.
FOLLOWUP_AFTER_BUSINESS_DAYS = 6
CLOSEOUT_AFTER_BUSINESS_DAYS = 15

# Scoring weights for `jt priority`. Tune these without reading the scorer.
WEIGHTS = {
    # Software roles get the stronger default push.
    "swe_base": 2.0,
    # Ops/comms roles still matter, just with a lower base.
    "other_base": 1.0,
    # A real contact is a warm channel and deserves action.
    "contact": 3.0,
    # Human inbound engagement is stronger than an ATS confirmation.
    "had_response": 2.0,
    # Later pipeline stages deserve more attention.
    "stage_progress": 1.5,
    # Every quiet business day decays urgency.
    "quiet_day": -0.25,
    # After one nudge and fifteen quiet business days, close it out.
    "closeout": -10.0,
}

DEFAULT_REVIEW_QUEUE_ALERT_THRESHOLD = 1
DEFAULT_REVIEW_AGE_ALERT_DAYS = 2
DEFAULT_CLASSIFY_FAILURE_ALERT_THRESHOLD = 3
CLASSIFY_FAILURE_EVENTS = {"final_model_exhaustion", "non_retryable_api_failure", "failure"}
CLASSIFY_RECOVERY_EVENTS = {
    "healthy_zero_message_run",
    "successful_processed_run",
    "malformed_classifier_output",
    "success",
}


KINDS_DDL = """
CREATE TABLE IF NOT EXISTS event_kinds (
    kind     TEXT PRIMARY KEY,
    category TEXT NOT NULL   -- stage | terminal | touch
);
"""

EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL
                   REFERENCES applications(id) ON DELETE RESTRICT,
    occurred_on    DATE NOT NULL,
    kind           TEXT NOT NULL REFERENCES event_kinds(kind),
    gmail_msg_id   TEXT UNIQUE,
    confidence     REAL CHECK (confidence IS NULL
                               OR (confidence >= 0.0 AND confidence <= 1.0)),
    evidence       TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id            INTEGER PRIMARY KEY,
    company       TEXT NOT NULL,
    role          TEXT NOT NULL,
    lane          TEXT NOT NULL CHECK (lane IN ('swe','ops','comms')),
    applied_on    DATE NOT NULL,
    source        TEXT,
    contact_email TEXT,
    url           TEXT,
    notes         TEXT,
    UNIQUE (company, role, applied_on)
);
""" + EVENTS_DDL + """
CREATE INDEX IF NOT EXISTS idx_events_app ON events(application_id);

-- Invariant 1, enforced by the engine rather than by good intentions.
CREATE TRIGGER IF NOT EXISTS events_append_only_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: UPDATE forbidden. Append a corrective event instead.');
END;

CREATE TRIGGER IF NOT EXISTS events_append_only_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: DELETE forbidden. Append a corrective event instead.');
END;

-- Phase 2 writes here: raw messages land before anything interprets them.
CREATE TABLE IF NOT EXISTS raw_messages (
    gmail_msg_id TEXT PRIMARY KEY,
    received_on  DATE,
    sender       TEXT,
    subject      TEXT,
    body         TEXT,
    processed    INTEGER NOT NULL DEFAULT 0
);

-- Things you committed to do (DESIGN D1/D2). Many per application. Unlike
-- events, these are mutable to-dos: done, dismissed, snoozed. A timed action
-- stores UTC plus the IANA zone it was entered in, so "2pm ET" survives DST.
CREATE TABLE IF NOT EXISTS actions (
    id              INTEGER PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE RESTRICT,
    description     TEXT NOT NULL CHECK (length(trim(description)) > 0),
    due_date        DATE,
    due_at_utc      TEXT,
    due_tz          TEXT,
    state           TEXT NOT NULL DEFAULT 'open'
                    CHECK (state IN ('open','done','snoozed','dismissed')),
    snoozed_until   TEXT,
    reason          TEXT,
    origin          TEXT NOT NULL DEFAULT 'manual'
                    CHECK (origin IN ('manual','review','migrated')),
    source_event_id INTEGER REFERENCES events(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at       TEXT,
    CHECK ((due_date IS NULL) != (due_at_utc IS NULL)),
    CHECK ((due_at_utc IS NULL) = (due_tz IS NULL)),
    CHECK (state != 'snoozed' OR snoozed_until IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS actions_by_app ON actions (application_id, state);

-- Phase 3 writes here: anything the classifier is not sure about.
-- A GROWING QUEUE IS THE FAILURE SIGNAL. `jt stale` surfaces the count.
CREATE TABLE IF NOT EXISTS review_queue (
    id            INTEGER PRIMARY KEY,
    gmail_msg_id  TEXT,
    proposed_json TEXT NOT NULL,
    reason        TEXT NOT NULL,
    resolved      INTEGER NOT NULL DEFAULT 0,
    resolved_event_id INTEGER REFERENCES events(id),
    resolved_note TEXT,
    resolved_at TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Local intake for possible applications. Nothing here is authoritative:
-- a human must explicitly accept a pending candidate before applications
-- changes, and acceptance uses the same validated insert path as `jt add`.
CREATE TABLE IF NOT EXISTS application_candidates (
    id            INTEGER PRIMARY KEY,
    raw_input     TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'manual_capture',
    source_ref    TEXT UNIQUE,
    company       TEXT,
    role          TEXT,
    lane          TEXT CHECK (lane IS NULL OR lane IN ('swe','ops','comms')),
    applied_on    DATE,
    contact_email TEXT,
    url           TEXT,
    notes         TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','accepted','rejected')),
    accepted_application_id INTEGER REFERENCES applications(id) ON DELETE RESTRICT,
    rejected_note TEXT,
    decided_at    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_application_candidates_url_unique
ON application_candidates(url)
WHERE url IS NOT NULL;

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------

def db_path() -> Path:
    p = os.environ.get("JT_DB")
    return Path(p).expanduser() if p else Path.home() / ".jobtrack" / "jobs.db"


def _seed_kinds(conn) -> None:
    rows = ([(k, "stage") for k in STAGE_RANK if k != "applied"]
            + [(k, "terminal") for k in TERMINAL]
            + [(k, "touch") for k in TOUCH])
    conn.executemany(
        "INSERT OR IGNORE INTO event_kinds (kind, category) VALUES (?,?)", rows)


def _is_legacy(conn) -> bool:
    """v1 shipped events with ON DELETE CASCADE and no kind FK.

    A leftover `events_legacy` table means an earlier migration attempt (on
    an older build) was interrupted after the rename but before it finished
    -- treat that as unmigrated too, rather than trusting whatever partial
    `events` table it left behind.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()
    legacy_shape = bool(row and "ON DELETE CASCADE" in (row[0] or ""))
    orphaned_rename = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events_legacy'"
    ).fetchone())
    return legacy_shape or orphaned_rename


def _migrate_v1_to_v2(conn) -> None:
    """Rebuild events with RESTRICT + kind FK, preserving every row.

    Runs as one real SQLite transaction. ALTER/CREATE/DROP TABLE are all
    transactional DDL in SQLite, so as long as executescript() (which forces
    an implicit commit before it runs) is never used here, a crash anywhere
    in this function leaves the database exactly as it was before migration
    started -- not half-migrated with history sitting in an orphaned table
    nobody looks at again.

    A `PRAGMA foreign_key_check` gate before the commit refuses to finish if
    any legacy row's `kind` isn't in `event_kinds`: silently letting such a
    row fall outside invariant 4 forever (invisible to every derive() call,
    still on disk) is worse than refusing to migrate.
    """
    conn.commit()  # close out the implicit transaction _seed_kinds left open
    recovering = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events_legacy'"
    ).fetchone())

    with write_txn(conn):
        if recovering:
            # An older build's migration died after the rename but before
            # the copy/drop. Discard whatever partial 'events' table it left
            # behind and recover from the real data in events_legacy.
            conn.execute("DROP TABLE IF EXISTS events")
        else:
            conn.execute("ALTER TABLE events RENAME TO events_legacy")

        conn.execute(EVENTS_DDL)
        conn.execute("""
            INSERT INTO events
                (id, application_id, occurred_on, kind, gmail_msg_id,
                 confidence, evidence, created_at)
            SELECT id, application_id, occurred_on, kind, gmail_msg_id,
                   confidence, evidence, created_at
            FROM events_legacy
        """)

        violations = conn.execute("PRAGMA foreign_key_check(events)").fetchall()
        if violations:
            bad_kinds = sorted({
                conn.execute(
                    "SELECT kind FROM events WHERE rowid = ?", (v[1],)
                ).fetchone()[0]
                for v in violations
            })
            raise RuntimeError(
                "migration refused: legacy events has kind(s) not in "
                f"event_kinds: {', '.join(bad_kinds)}. This entire attempt "
                "rolls back -- your database is untouched, still on the old "
                "schema. Fix or remap those rows by hand, or add the "
                "kind(s) to jt.py's EVENT_KINDS, then re-run."
            )

        conn.execute("DROP TABLE events_legacy")
    print("migrated database to schema v2 (append-only enforced)", file=sys.stderr)


def _ensure_review_columns(conn) -> None:
    cols = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(review_queue)")
    }
    additions = {
        "resolved_event_id": "ALTER TABLE review_queue ADD COLUMN resolved_event_id INTEGER REFERENCES events(id)",
        "resolved_note": "ALTER TABLE review_queue ADD COLUMN resolved_note TEXT",
        "resolved_at": "ALTER TABLE review_queue ADD COLUMN resolved_at TEXT",
    }
    for name, ddl in additions.items():
        if name not in cols:
            conn.execute(ddl)


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    # Without this, `INSERT OR REPLACE` performs its implicit DELETE WITHOUT
    # firing the delete trigger -- a silent hole straight through invariant 1.
    # It is a per-connection pragma, so any other client (a psql-style shell,
    # an agent using sqlite3 directly) reopens the hole. Hence the belt-and-
    # braces: pragma here, and a loud warning in the module docstring.
    conn.execute("PRAGMA recursive_triggers = ON")

    # FKs off during setup so migration can rebuild tables safely.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(KINDS_DDL)
    _seed_kinds(conn)
    if _is_legacy(conn):
        _migrate_v1_to_v2(conn)
    conn.executescript(SCHEMA)
    _ensure_review_columns(conn)
    conn.execute(
        "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    _migrate_next_actions(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_next_actions(conn) -> None:
    """Schema v6: one-per-application `next_actions` -> many-per-application
    `actions`. Each old row becomes an open, all-day action. Runs once."""
    if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='next_actions'").fetchone():
        return
    with write_txn(conn):
        n = conn.execute("""
            INSERT INTO actions (application_id, description, due_date, origin)
            SELECT application_id, description, substr(due_on, 1, 10), 'migrated'
            FROM next_actions""").rowcount
        conn.execute("DROP TABLE next_actions")
    print(f"migrated {n} next action(s) to the actions table (schema v6)", file=sys.stderr)


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """The one way to write. DESIGN D6.

    `BEGIN IMMEDIATE` takes SQLite's writer lock up front, so every read inside
    the block (current status, "already resolved?", "still pending?") sees the
    state the write will actually land on. A plain deferred transaction reads
    first and locks later, which is how a classifier batch and a review
    approval could both pass a legality check and both commit.

    Commits on success; rolls back on ANY exception, including the
    `sys.exit()` that command handlers use for user errors. Never call a
    model or the network inside this block: the lock blocks every other
    writer while it is held.
    """
    if conn.in_transaction:
        raise RuntimeError(
            "write_txn: a transaction is already open on this connection; "
            "commit or roll it back first (nested writes would escape the lock)")
    old = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.isolation_level = old


def parse_day(s: str | None, today: date | None = None) -> str:
    today = today or date.today()
    if not s or s == "today":
        return today.isoformat()
    if s == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        sys.exit(f"error: date must be YYYY-MM-DD, 'today' or 'yesterday' (got {s!r})")


def business_days_since(iso_day: str, today: date | None = None) -> int:
    """Injectable clock. Tests cannot control date.today(), and staleness
    assertions that silently rot overnight produce CI you learn to ignore."""
    today = today or date.today()
    d = date.fromisoformat(iso_day)
    if d >= today:
        return 0
    n, cur = 0, d
    while cur < today:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


# --------------------------------------------------------------------------
# Derived state -- the heart of the design
# --------------------------------------------------------------------------

def derive(conn, app_row, today: date | None = None) -> dict:
    """Compute current status from the event log. Never stored, always computed."""
    events = conn.execute(
        "SELECT * FROM events WHERE application_id = ? ORDER BY occurred_on, id",
        (app_row["id"],),
    ).fetchall()

    terminal = [e for e in events if e["kind"] in TERMINAL]
    if terminal:
        # The most recently APPENDED terminal event wins, not the one with
        # the latest occurred_on. A correction is entered after the fact and
        # is often dated to when it actually happened, which can be earlier
        # than the mistake it corrects -- id (insertion order) is the only
        # thing that reliably tracks "the user's most recent word."
        status = max(terminal, key=lambda e: e["id"])["kind"]
    else:
        status, rank = "applied", 0
        for e in events:
            r = STAGE_RANK.get(e["kind"])
            if r is not None and r > rank:
                status, rank = e["kind"], r

    last_touch = app_row["applied_on"]
    for e in events:
        if e["occurred_on"] > last_touch:
            last_touch = e["occurred_on"]

    return {
        "status": status,
        "is_terminal": status in TERMINAL,
        "stage_rank": STAGE_RANK.get(status, 0),
        "last_touch": last_touch,
        "bdays_quiet": business_days_since(last_touch, today),
        "followups": sum(1 for e in events if e["kind"] == "followup_sent"),
        "had_response": any(e["kind"] in INBOUND for e in events),
        "events": events,
    }


def action_for(d: dict) -> str:
    """The only opinion this tool holds: what to do about this one today."""
    if d["is_terminal"]:
        return "-"
    if d["followups"] == 0 and d["bdays_quiet"] >= FOLLOWUP_AFTER_BUSINESS_DAYS:
        return "FOLLOW UP"
    if d["followups"] >= 1 and d["bdays_quiet"] >= CLOSEOUT_AFTER_BUSINESS_DAYS:
        return "CLOSE OUT"
    return "wait"


def priority_terms(app_row, d: dict) -> list[tuple[str, float]]:
    max_rank = max(STAGE_RANK.values())
    terms = [
        ("base", WEIGHTS["swe_base"] if app_row["lane"] == "swe" else WEIGHTS["other_base"]),
        ("contact", WEIGHTS["contact"] if app_row["contact_email"] else 0.0),
        ("had_response", WEIGHTS["had_response"] if d["had_response"] else 0.0),
        ("stage_progress", WEIGHTS["stage_progress"] * (d["stage_rank"] / max_rank)),
        ("quiet", WEIGHTS["quiet_day"] * d["bdays_quiet"]),
    ]
    closeout = (
        WEIGHTS["closeout"]
        if d["followups"] >= 1 and d["bdays_quiet"] >= CLOSEOUT_AFTER_BUSINESS_DAYS
        else 0.0
    )
    terms.append(("closeout", closeout))
    return terms


def priority_score(app_row, d: dict) -> float:
    return sum(v for _, v in priority_terms(app_row, d))


def pending_reviews(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE resolved = 0").fetchone()[0]


def pending_candidates(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM application_candidates WHERE status = 'pending'"
    ).fetchone()[0]


def classify_log_path() -> Path:
    return Path(os.environ.get("JT_LOG_DIR", str(Path.home() / ".jobtrack" / "logs"))) / "jt-classify.jsonl"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        sys.exit(f"error: {name} must be an integer")


def _parse_created_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace(" ", "T"))


def _utcnow() -> datetime:
    """Naive UTC now. One seam so tests can pin the clock."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _age_days(created_at: str, now: datetime | None = None) -> int:
    now = now or _utcnow()
    created = _parse_created_at(created_at)
    return max(0, (now.date() - created.date()).days)


def review_queue_stats(conn, now: datetime | None = None) -> dict:
    row = conn.execute(
        """SELECT COUNT(*) AS total, MIN(created_at) AS oldest_created_at
           FROM review_queue
           WHERE resolved = 0"""
    ).fetchone()
    oldest = row["oldest_created_at"]
    age = _age_days(oldest, now) if oldest else 0
    return {"total": row["total"], "oldest_created_at": oldest, "oldest_age_days": age}


def _read_classify_log_events(path: Path, limit: int = 1000) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("component") == "jt-classify":
            rows.append(row)
    return rows


def classify_failure_stats(log_path: Path) -> dict:
    rows = _read_classify_log_events(log_path)
    since_recovery: list[dict] = []
    consecutive_failures = 0
    for row in rows:
        event = row.get("event")
        if event in CLASSIFY_RECOVERY_EVENTS:
            since_recovery = []
            consecutive_failures = 0
        elif event in CLASSIFY_FAILURE_EVENTS:
            since_recovery.append(row)
            consecutive_failures += 1
    exhaustion = next((r for r in reversed(since_recovery)
                       if r.get("event") == "final_model_exhaustion"), None)
    latest_failure = since_recovery[-1] if since_recovery else None
    return {
        "consecutive_failures": consecutive_failures,
        "has_classifier_exhaustion": exhaustion is not None,
        "latest_exhaustion": exhaustion,
        "latest_failure": latest_failure,
        "log_path": str(log_path),
    }


def review_alerts(conn, *, log_path: Path | None = None, now: datetime | None = None,
                  queue_threshold: int | None = None, age_days: int | None = None,
                  failure_threshold: int | None = None) -> tuple[list[str], dict]:
    queue_threshold = queue_threshold if queue_threshold is not None else _int_env(
        "JT_REVIEW_ALERT_THRESHOLD", DEFAULT_REVIEW_QUEUE_ALERT_THRESHOLD)
    age_days = age_days if age_days is not None else _int_env(
        "JT_REVIEW_AGE_ALERT_DAYS", DEFAULT_REVIEW_AGE_ALERT_DAYS)
    failure_threshold = failure_threshold if failure_threshold is not None else _int_env(
        "JT_CLASSIFY_FAILURE_ALERT_THRESHOLD", DEFAULT_CLASSIFY_FAILURE_ALERT_THRESHOLD)
    log_path = log_path or classify_log_path()

    review_stats = review_queue_stats(conn, now)
    failure_stats = classify_failure_stats(log_path)
    alerts = []
    if review_stats["total"] >= queue_threshold:
        alerts.append(f"total_review_queue_size={review_stats['total']} threshold={queue_threshold}")
    if review_stats["total"] and review_stats["oldest_age_days"] >= age_days:
        alerts.append(
            f"oldest_review_item_age_days={review_stats['oldest_age_days']} threshold={age_days} "
            f"oldest_created_at={review_stats['oldest_created_at']}"
        )
    if failure_stats["has_classifier_exhaustion"]:
        row = failure_stats["latest_exhaustion"] or {}
        alerts.append(
            "classifier_exhaustion_event "
            f"attempt_count={row.get('attempt_count')} status_code={row.get('status_code')} "
            f"error_type={row.get('error_type')}"
        )
    if failure_stats["consecutive_failures"] >= failure_threshold:
        row = failure_stats["latest_failure"] or {}
        alerts.append(
            f"repeated_classify_failures={failure_stats['consecutive_failures']} threshold={failure_threshold} "
            f"latest_event={row.get('event')} status_code={row.get('status_code')} "
            f"error_type={row.get('error_type')}"
        )
    return alerts, {"review": review_stats, "classify": failure_stats}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Actions (DESIGN D1/D2)
# --------------------------------------------------------------------------

ACTION_CLOSED = ("done", "dismissed")


def viewer_tz() -> ZoneInfo:
    """The zone dates are shown in: $JT_TZ, else $TZ, else this Mac's zone.

    $JT_TZ is jt's own setting, so a bad value is a clear error. $TZ belongs
    to the OS and may hold POSIX forms that aren't IANA names (":/etc/localtime",
    "EST5EDT"); those are skipped rather than crashing every command.
    """
    if os.environ.get("JT_TZ"):
        return _zone(os.environ["JT_TZ"])
    posix = (os.environ.get("TZ") or "").removeprefix(":")
    if posix and not posix.startswith("/"):
        try:
            return ZoneInfo(posix)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return ZoneInfo(link.split("zoneinfo/", 1)[1])
    except (OSError, ZoneInfoNotFoundError):
        pass
    return ZoneInfo("UTC")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        sys.exit(f"error: unknown time zone {name!r} (use an IANA name like America/New_York)")


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_utc_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def action_due_local(row, tz: ZoneInfo) -> datetime | None:
    """A timed action's due moment in `tz`; None for all-day actions."""
    return _from_utc_iso(row["due_at_utc"]).astimezone(tz) if row["due_at_utc"] else None


def action_due_day(row, tz: ZoneInfo) -> date:
    local = action_due_local(row, tz)
    return local.date() if local else date.fromisoformat(row["due_date"])


def action_due_label(row, tz: ZoneInfo) -> str:
    local = action_due_local(row, tz)
    return local.strftime("%Y-%m-%d %H:%M %Z") if local else row["due_date"]


def add_action(conn, application_id: int, description: str, *,
               due_date: str | None = None, due_local: datetime | None = None,
               tz: str | None = None, reason: str | None = None,
               origin: str = "manual", source_event_id: int | None = None) -> int:
    """Add an action. Exactly one of due_date (all-day) or due_local (a naive
    wall-clock time in zone `tz`) is required."""
    if (due_date is None) == (due_local is None):
        sys.exit("error: give a due date, or a due date with a time")
    due_at_utc = zone_name = None
    if due_local is not None:
        zone_name = tz or viewer_tz().key
        local = due_local.replace(tzinfo=_zone(zone_name))
        due_at_utc = _utc_iso(local)
    elif due_date is not None:
        due_date = parse_day(due_date)
    if not description or not description.strip():
        sys.exit("error: an action needs a description")
    with write_txn(conn):
        if not conn.execute("SELECT 1 FROM applications WHERE id = ?", (application_id,)).fetchone():
            sys.exit(f"error: no application #{application_id}")
        cur = conn.execute(
            """INSERT INTO actions (application_id, description, due_date, due_at_utc,
                                   due_tz, reason, origin, source_event_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (application_id, description.strip(), due_date, due_at_utc, zone_name,
             reason, origin, source_event_id))
    return int(cur.lastrowid or 0)


def set_action_state(conn, action_id: int, state: str, *,
                     snoozed_until: datetime | None = None) -> None:
    if state not in ("open", "done", "snoozed", "dismissed"):
        sys.exit(f"error: unknown action state {state!r}")
    if state == "snoozed" and snoozed_until is None:
        sys.exit("error: snoozing needs --until (when should it come back?)")
    with write_txn(conn):
        row = conn.execute("SELECT state FROM actions WHERE id = ?", (action_id,)).fetchone()
        if row is None:
            sys.exit(f"error: no action #{action_id}")
        if row["state"] in ACTION_CLOSED:
            sys.exit(f"error: action #{action_id} is already {row['state']}")
        conn.execute(
            """UPDATE actions SET state = ?, snoozed_until = ?,
                   closed_at = CASE WHEN ? IN ('done','dismissed') THEN datetime('now') END
               WHERE id = ?""",
            (state, _utc_iso(snoozed_until) if snoozed_until else None, state, action_id))


def pending_actions(conn, *, now: datetime, include_snoozed: bool = True,
                    application_id: int | None = None) -> list[sqlite3.Row]:
    """Open actions, plus snoozed ones. With include_snoozed=False, a snoozed
    action counts only once its snooze has run out."""
    tz = now.tzinfo if isinstance(now.tzinfo, ZoneInfo) else viewer_tz()
    sql = """SELECT x.*, a.company, a.role, e.gmail_msg_id AS source_gmail_msg_id
             FROM actions x
             JOIN applications a ON a.id = x.application_id
             LEFT JOIN events e ON e.id = x.source_event_id
             WHERE x.state IN ('open','snoozed')"""
    params: list = []
    if application_id is not None:
        sql += " AND x.application_id = ?"
        params.append(application_id)
    rows = conn.execute(sql, params).fetchall()
    now_utc = _utc_iso(now)
    keep = [r for r in rows
            if include_snoozed or r["state"] == "open" or (r["snoozed_until"] or "") <= now_utc]
    return sorted(keep, key=lambda r: (action_due_day(r, tz),
                                       action_due_local(r, tz) is not None,
                                       r["due_at_utc"] or "", r["id"]))


def _now_for(day: date, explicit: bool, tz: ZoneInfo) -> datetime:
    """`--on DAY` means "as of the end of that day"; otherwise right now."""
    return datetime.combine(day, time(23, 59, 59), tz) if explicit else datetime.now(tz)


def today_entries(conn, today: date, now: datetime, tz: ZoneInfo) -> dict:
    """The read model behind `jt today`: due actions you committed to, and
    jt's follow-up suggestions for applications with no pending action."""
    apps = {r["id"]: r for r in conn.execute("SELECT * FROM applications")}
    states = {i: derive(conn, app, today) for i, app in apps.items()}
    open_apps = {i for i, d in states.items() if not d["is_terminal"]}

    actions = []
    for r in pending_actions(conn, now=now, include_snoozed=False):
        if r["application_id"] not in open_apps:
            continue
        day = action_due_day(r, tz)
        if day <= today:
            actions.append({"row": r, "due_day": day, "overdue": day < today,
                            "due_label": action_due_label(r, tz)})

    covered = {r["application_id"] for r in pending_actions(conn, now=now)}
    suggestions = []
    for i in open_apps - covered:
        kind = action_for(states[i])
        if kind in {"FOLLOW UP", "CLOSE OUT"}:
            suggestions.append({"app": apps[i], "kind": kind, "state": states[i],
                                "score": priority_score(apps[i], states[i])})
    suggestions.sort(key=lambda x: (-x["score"], x["app"]["id"]))
    return {"actions": actions, "suggestions": suggestions}


# --------------------------------------------------------------------------
# `jt today --json` (DESIGN D5). The only thing a frontend may read.
# --------------------------------------------------------------------------

TODAY_SCHEMA_VERSION = 1   # bump on any rename, removal, or type change


def set_fresh(conn, key: str, when: datetime) -> None:
    """Record a freshness timestamp. Caller holds write_txn."""
    conn.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
                 (key, _utc_iso(when)))


def mark_fresh(conn, key: str, when: datetime | None = None) -> None:
    with write_txn(conn):
        set_fresh(conn, key, when or datetime.now(timezone.utc))


def _fresh(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _log_dir() -> Path:
    return Path(os.environ.get("JT_LOG_DIR", str(Path.home() / ".jobtrack" / "logs")))


OFFLINE_ERRORS = {"ServerNotFoundError", "ConnectionError", "TimeoutError", "gaierror",
                  "TransportError", "ConnectionResetError", "ConnectionRefusedError", "OSError"}
AUTH_MARKERS = ("invalid_grant", "credentials are not valid", "token has been expired",
                "token_path does not exist", "refresherror")


def sync_error_state(log_path: Path | None = None) -> str | None:
    """None if the last sync succeeded (or none ran), else offline /
    auth_expired / failed, from the last result in jt-sync.jsonl."""
    path = log_path or (_log_dir() / "jt-sync.jsonl")
    if not path.exists():
        return None
    last = None
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") in {"success", "failure"}:
                last = e
    if not last or last["event"] == "success":
        return None
    kind = str(last.get("error_type") or "")
    text = f"{kind} {last.get('error') or ''}".lower()
    if any(m in text for m in AUTH_MARKERS):
        return "auth_expired"
    if kind in OFFLINE_ERRORS:
        return "offline"
    return "failed"


def gmail_url(msg_id: str | None) -> str | None:
    if not msg_id:
        return None
    return f"https://mail.google.com/mail/u/{os.environ.get('GMAIL_ACCOUNT_INDEX', '0')}/#all/{msg_id}"


def _suggestion_reason(kind: str, d: dict) -> str:
    if kind == "FOLLOW UP":
        return f"{d['bdays_quiet']} business days quiet, no follow-up sent"
    return f"{d['bdays_quiet']} business days quiet after {d['followups']} follow-up(s)"


def today_snapshot(conn, today: date, now: datetime, tz: ZoneInfo) -> dict:
    entries = today_entries(conn, today, now, tz)
    actions = []
    for x in entries["actions"]:
        r = x["row"]
        due = ({"kind": "time", "value": r["due_at_utc"], "tz": r["due_tz"]} if r["due_at_utc"]
               else {"kind": "date", "value": r["due_date"]})
        actions.append({
            "action_id": r["id"], "application_id": r["application_id"],
            "company": r["company"], "role": r["role"], "description": r["description"],
            "due": due, "overdue": x["overdue"], "origin": r["origin"], "reason": r["reason"],
            "gmail_msg_id": r["source_gmail_msg_id"],
            "gmail_url": gmail_url(r["source_gmail_msg_id"]),
        })
    suggestions = [{
        "application_id": x["app"]["id"], "company": x["app"]["company"],
        "role": x["app"]["role"], "kind": x["kind"],
        "reason": _suggestion_reason(x["kind"], x["state"]),
    } for x in entries["suggestions"]]
    now_utc = now.astimezone(timezone.utc).replace(tzinfo=None)
    stats = review_queue_stats(conn, now_utc)
    top = conn.execute("""SELECT id, reason FROM review_queue WHERE resolved = 0
                          ORDER BY created_at, id LIMIT 1""").fetchone()
    recorded = [
        dict(event_id=r["id"], company=r["company"], role=r["role"], kind=r["kind"],
             occurred_on=r["occurred_on"], gmail_url=gmail_url(r["gmail_msg_id"]))
        for r in conn.execute("""
            SELECT e.id, a.company, a.role, e.kind, e.occurred_on, e.gmail_msg_id
            FROM events e JOIN applications a ON a.id = e.application_id
            WHERE e.gmail_msg_id IS NOT NULL
            ORDER BY e.created_at DESC, e.id DESC LIMIT 3
        """)
    ]
    return {
        "schema_version": TODAY_SCHEMA_VERSION,
        "generated_at": _utc_iso(now),
        "badge": {"due": len(actions), "reviews": stats["total"]},
        "freshness": {"gmail_synced_at": _fresh(conn, "gmail_synced_at"),
                      "classified_at": _fresh(conn, "classified_at"),
                      "sync_error": sync_error_state()},
        "actions": actions,
        "suggestions": suggestions,
        "recorded": recorded,
        "review": {"pending": stats["total"], "oldest_days": stats["oldest_age_days"],
                   "top": {"review_id": top["id"], "reason": top["reason"]} if top else None},
    }


def table(headers, rows) -> str:
    if not rows:
        return "  (nothing)"
    cols = [list(map(str, col)) for col in zip(headers, *rows)]
    widths = [max(len(c) for c in col) for col in cols]
    out = ["  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)),
           "  " + "  ".join("-" * w for w in widths)]
    for r in rows:
        out.append("  " + "  ".join(str(c).ljust(w) for c, w in zip(r, widths)))
    return "\n".join(out)


def review_banner(conn) -> str:
    chunks = []
    n = pending_reviews(conn)
    if n:
        chunks.append(
            f"  !! {n} classification(s) awaiting review. A growing queue means\n"
            f"     the system is silently missing events. Run: jt review\n"
        )
    c = pending_candidates(conn)
    if c:
        chunks.append(
            f"  !! {c} application candidate(s) awaiting decision. A growing queue means\n"
            f"     capture is invisible after creation. Run: jt candidates\n"
        )
    return "\n" + "".join(chunks) if chunks else ""


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _application_fields(a) -> tuple[str, str, str, str, str | None, str | None, str | None, str | None]:
    day = parse_day(getattr(a, "on", None))
    company = a.company.strip()
    role = a.role.strip()
    lane = a.lane
    if not company or not role:
        sys.exit("error: company and role must be non-empty")
    if lane not in LANES:
        sys.exit(f"error: lane must be one of: {', '.join(LANES)}")
    return (
        company,
        role,
        lane,
        day,
        getattr(a, "source", None),
        getattr(a, "contact", None),
        getattr(a, "url", None),
        getattr(a, "notes", None),
    )


def _find_application_by_key(conn, company: str, role: str, applied_on: str):
    return conn.execute(
        """SELECT * FROM applications
           WHERE company = ? AND role = ? AND applied_on = ?""",
        (company, role, applied_on),
    ).fetchone()


def _insert_application(conn, a) -> tuple[int, tuple]:
    fields = _application_fields(a)
    cur = conn.execute(
        """INSERT INTO applications
           (company, role, lane, applied_on, source, contact_email, url, notes)
           VALUES (?,?,?,?,?,?,?,?)""",
        fields,
    )
    return cur.lastrowid, fields


def cmd_add(conn, a):
    try:
        with write_txn(conn):
            app_id, fields = _insert_application(conn, a)
        company, role, lane, day = fields[:4]
        print(f"#{app_id}  {company} - {role}  [{lane}]  applied {day}")
    except sqlite3.IntegrityError:
        company, role, _, day = _application_fields(a)[:4]
        existing = _find_application_by_key(conn, company, role, day)
        suffix = f" as #{existing['id']}" if existing else ""
        print(f"already logged{suffix}: {company} - {role} on {day}")


def cmd_bulk(conn, a):
    """Read 'Company | Role | lane [| url]' from stdin. For a day's batch."""
    day = parse_day(a.on)
    added = skipped = 0
    # Parse all of stdin first: never hold the writer lock while waiting on input.
    to_insert: list[argparse.Namespace] = []
    for raw in sys.stdin:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            print(f"  skip (need 'Company | Role | lane'): {line}")
            skipped += 1
            continue
        company, role, lane = parts[0], parts[1], parts[2].lower()
        url = parts[3] if len(parts) > 3 else None
        if not company or not role:
            print(f"  skip (empty company/role): {line}")
            skipped += 1
            continue
        if lane not in LANES:
            print(f"  skip (bad lane {lane!r}): {line}")
            skipped += 1
            continue
        to_insert.append(argparse.Namespace(
            company=company, role=role, lane=lane, on=day,
            source=None, contact=None, url=url, notes=None,
        ))
    with write_txn(conn):
        for ns in to_insert:
            try:
                _insert_application(conn, ns)
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1
    print(f"added {added}, skipped {skipped}")


def cmd_list(conn, a):
    apps = conn.execute(
        "SELECT * FROM applications ORDER BY applied_on DESC, id DESC").fetchall()
    rows = []
    for app in apps:
        d = derive(conn, app)
        if a.lane and app["lane"] != a.lane:
            continue
        if a.status and d["status"] != a.status:
            continue
        if not a.all and d["is_terminal"]:
            continue
        rows.append([app["id"], app["applied_on"], app["company"][:26],
                     app["role"][:32], app["lane"], d["status"],
                     f'{d["bdays_quiet"]}d', action_for(d)])
    print()
    print(table(["#", "APPLIED", "COMPANY", "ROLE", "LANE", "STATUS", "QUIET", "ACTION"], rows))
    print(f"\n  {len(rows)} shown"
          + ("" if a.all else "  (terminal hidden; --all to include)"))
    print(review_banner(conn))


def cmd_stale(conn, a):
    apps = conn.execute("SELECT * FROM applications").fetchall()
    rows = []
    for app in apps:
        d = derive(conn, app)
        act = action_for(d)
        if act in ("FOLLOW UP", "CLOSE OUT"):
            rows.append([app["id"], app["company"][:26], app["role"][:32],
                         d["status"], f'{d["bdays_quiet"]}d', act,
                         app["contact_email"] or "-"])
    rows.sort(key=lambda r: (r[5] != "FOLLOW UP", -int(r[4][:-1])))
    print(review_banner(conn))
    print(table(["#", "COMPANY", "ROLE", "STATUS", "QUIET", "ACTION", "CONTACT"], rows))
    print()


def cmd_draft_followup(conn, a):
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (a.id,)).fetchone()
    if app is None:
        sys.exit(f"error: no application #{a.id}")
    state = derive(conn, app)
    if state["is_terminal"]:
        sys.exit("error: this application is closed")
    if not app["contact_email"]:
        sys.exit("error: add or verify a recruiter contact before drafting a follow-up")
    company, role = app["company"], app["role"]
    print(f"To: {app['contact_email']}")
    print(f"Subject: Following up on the {role} role at {company}")
    print(f"\nHi,\n\nI’m following up on the {role} role at {company}. "
          "I’m still interested and would appreciate any update on next steps.\n\n"
          "Thanks for your time,\n[Your name]")


def cmd_next_action(conn, a):
    """Kept for old scripts: adds an action, or --clear dismisses every
    pending action on the application."""
    if a.clear:
        with write_txn(conn):
            if not conn.execute("SELECT 1 FROM applications WHERE id = ?", (a.id,)).fetchone():
                sys.exit(f"error: no application #{a.id}")
            n = conn.execute(
                """UPDATE actions SET state = 'dismissed', closed_at = datetime('now')
                   WHERE application_id = ? AND state IN ('open','snoozed')""", (a.id,)).rowcount
        print(f"dismissed {n} action(s) on #{a.id}")
        return
    if not a.description or not a.description.strip():
        sys.exit("error: provide an action description and --due, or use --clear")
    action_id = add_action(conn, a.id, a.description, due_date=a.due)
    print(f"action #{action_id} on #{a.id}: {a.description.strip()} (due {parse_day(a.due)})")


def _parse_when(s: str, tz: ZoneInfo) -> datetime:
    """'YYYY-MM-DD' (start of that day) or 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DDTHH:MM', in tz."""
    s = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    sys.exit(f"error: could not read {s!r}; use YYYY-MM-DD or 'YYYY-MM-DD HH:MM'")


def _print_ok(**fields) -> None:
    print(json.dumps({"ok": True, **fields}))


def cmd_action(conn, a):
    tz = viewer_tz()
    if a.action_cmd == "add":
        if a.at:
            try:
                hh, mm = (int(x) for x in a.at.split(":"))
                wall = datetime.combine(date.fromisoformat(parse_day(a.due)), time(hh, mm))
            except ValueError:
                sys.exit(f"error: --at must be HH:MM, got {a.at!r}")
            action_id = add_action(conn, a.id, a.description, due_local=wall,
                                   tz=a.tz or tz.key, reason=a.reason)
        else:
            if a.tz:
                sys.exit("error: --tz only applies with --at")
            action_id = add_action(conn, a.id, a.description, due_date=a.due, reason=a.reason)
        row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        if a.json:
            return _print_ok(action_id=action_id, state="open")
        print(f"action #{action_id} on #{a.id}: {row['description']} (due {action_due_label(row, tz)})")
    elif a.action_cmd in ("done", "dismiss"):
        state = "done" if a.action_cmd == "done" else "dismissed"
        set_action_state(conn, a.action_id, state)
        if a.json:
            return _print_ok(action_id=a.action_id, state=state)
        print(f"action #{a.action_id} {state}")
    elif a.action_cmd == "snooze":
        until = _parse_when(a.until, tz)
        set_action_state(conn, a.action_id, "snoozed", snoozed_until=until)
        if a.json:
            return _print_ok(action_id=a.action_id, state="snoozed")
        print(f"action #{a.action_id} snoozed until {until.strftime('%Y-%m-%d %H:%M %Z')}")
    elif a.action_cmd == "reopen":
        set_action_state(conn, a.action_id, "open")
        if a.json:
            return _print_ok(action_id=a.action_id, state="open")
        print(f"action #{a.action_id} reopened")
    else:  # list
        if a.all:
            rows = conn.execute(
                """SELECT x.*, a.company, a.role FROM actions x
                   JOIN applications a ON a.id = x.application_id ORDER BY x.id""").fetchall()
        else:
            rows = pending_actions(conn, now=datetime.now(tz))
        if not rows:
            print("No pending actions.")
            return
        out = []
        for r in rows:
            state = r["state"]
            if state == "snoozed":
                state += " until " + _from_utc_iso(r["snoozed_until"]).astimezone(tz).strftime("%m-%d %H:%M")
            out.append([r["id"], r["application_id"], r["company"], r["role"],
                        action_due_label(r, tz), state, r["description"]])
        print(table(["ACT", "APP", "COMPANY", "ROLE", "DUE", "STATE", "ACTION"], out))


def cmd_today(conn, a):
    tz = viewer_tz()
    today = date.fromisoformat(parse_day(a.on))
    now = _now_for(today, bool(a.on), tz)
    if getattr(a, "json", False):
        print(json.dumps(today_snapshot(conn, today, now, tz), indent=2))
        return
    entries = today_entries(conn, today, now, tz)
    rows = []
    for x in entries["actions"]:
        r = x["row"]
        what = r["description"] + ("  (overdue)" if x["overdue"] else "")
        rows.append([r["id"], r["application_id"], r["company"], r["role"], x["due_label"], what])
    for x in entries["suggestions"]:
        app = x["app"]
        rows.append(["-", app["id"], app["company"], app["role"], "-", x["kind"]])
    if rows:
        print(table(["ACT", "APP", "COMPANY", "ROLE", "DUE", "ACTION"], rows))
    else:
        print("No actions due.")
    print(review_banner(conn))


def cmd_priority(conn, a):
    today = date.fromisoformat(a.today) if a.today else None
    if a.explain:
        app = conn.execute("SELECT * FROM applications WHERE id = ?", (a.explain,)).fetchone()
        if not app:
            sys.exit(f"error: no application #{a.explain}")
        d = derive(conn, app, today)
        rows = [[name, f"{value:.2f}"] for name, value in priority_terms(app, d)]
        rows.append(["TOTAL", f"{priority_score(app, d):.2f}"])
        print(f"\n  #{app['id']}  {app['company']} - {app['role']}  ({d['status']})")
        print(table(["TERM", "POINTS"], rows))
        print(f"\n  action: {action_for(d)}\n")
        return

    ranked: list[tuple[float, sqlite3.Row, dict]] = []
    for app in conn.execute("SELECT * FROM applications ORDER BY applied_on, id"):
        d = derive(conn, app, today)
        if d["is_terminal"]:
            continue
        ranked.append((priority_score(app, d), app, d))
    ranked.sort(key=lambda r: r[0], reverse=True)
    out = [[app["id"], f"{score:.2f}", app["company"][:26], app["role"][:32],
            d["status"], action_for(d)] for score, app, d in ranked[:10]]
    print()
    print(table(["#", "SCORE", "COMPANY", "ROLE", "STATUS", "ACTION"], out))
    print()


def cmd_log(conn, a):
    with write_txn(conn):
        app = _log_event_locked(conn, a)
    d = derive(conn, app)
    print(f"#{a.id} {app['company']} - {app['role']}  ->  {d['status']}")


def _log_event_locked(conn, a):
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (a.id,)).fetchone()
    if not app:
        sys.exit(f"error: no application #{a.id}")
    # `note` is a human act, deliberately outside the transition gate (see
    # transitions.py) -- always allowed, terminal or not, since it never
    # changes status. Everything else goes through the same legality check
    # that binds the classifier, so a manual `jt log` can't silently do what
    # a confidently-wrong model is forbidden from doing.
    if a.kind != "note" and not a.force:
        current = derive(conn, app)["status"]
        ok, reason = is_legal(current, a.kind)
        if not ok:
            sys.exit(f"error: {reason}\n  pass --force to log it anyway (for a deliberate manual correction)")
    try:
        conn.execute(
            "INSERT INTO events (application_id, occurred_on, kind, evidence) "
            "VALUES (?,?,?,?)",
            (a.id, parse_day(a.on), a.kind, a.note),
        )
    except sqlite3.IntegrityError as e:
        sys.exit(f"error: {e}\n  valid kinds: {', '.join(sorted(EVENT_KINDS))}")
    return app


def cmd_show(conn, a):
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (a.id,)).fetchone()
    if not app:
        sys.exit(f"error: no application #{a.id}")
    d = derive(conn, app)
    print(f"\n  #{app['id']}  {app['company']} - {app['role']}")
    print(f"  lane {app['lane']} | applied {app['applied_on']} | status {d['status']}")
    print(f"  quiet {d['bdays_quiet']} business days | follow-ups sent {d['followups']}")
    if app["contact_email"]:
        print(f"  contact  {app['contact_email']}")
    if app["url"]:
        print(f"  url      {app['url']}")
    print(f"  action   {action_for(d)}\n")
    rows = [[e["occurred_on"], e["kind"],
             "auto" if e["gmail_msg_id"] else "manual",
             (e["evidence"] or "")[:52]] for e in d["events"]]
    print(table(["WHEN", "KIND", "SOURCE", "EVIDENCE"], rows))
    print()


def cmd_rm(conn, a):
    """Deletable only while it has zero events. Past that, it is history."""
    with write_txn(conn):
        app = _rm_locked(conn, a)
    print(f"removed #{a.id} {app['company']} - {app['role']}")


def _rm_locked(conn, a):
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (a.id,)).fetchone()
    if not app:
        sys.exit(f"error: no application #{a.id}")
    n = conn.execute("SELECT COUNT(*) FROM events WHERE application_id = ?",
                     (a.id,)).fetchone()[0]
    if n:
        sys.exit(f"error: #{a.id} has {n} event(s) and cannot be deleted.\n"
                 f"  History is not erasable. Use: jt log {a.id} withdrawn")
    # Actions are to-dos, not history: they go with a mistyped application.
    conn.execute("DELETE FROM actions WHERE application_id = ?", (a.id,))
    conn.execute("DELETE FROM applications WHERE id = ?", (a.id,))
    return app


def _write_csv(conn, writer, lane=None) -> int:
    writer.writerow(["Applied", "Company", "Role", "Lane", "Status", "Link"])
    n = 0
    for app in conn.execute("SELECT * FROM applications ORDER BY applied_on"):
        if lane and app["lane"] != lane:
            continue
        d = derive(conn, app)
        writer.writerow([app["applied_on"], app["company"], app["role"],
                         app["lane"], d["status"], app["url"] or ""])
        n += 1
    return n


def cmd_export(conn, a):
    if a.out:
        with open(a.out, "w", newline="") as f:
            n = _write_csv(conn, csv.writer(f), a.lane)
        print(f"wrote {n} rows to {a.out}")
    else:
        _write_csv(conn, csv.writer(sys.stdout), a.lane)


CLOUD_DIR_NAMES = {"dropbox", "google drive", "onedrive", "icloud drive", "box", "box sync"}
CLOUD_PATH_MARKERS = ("library/mobile documents", "library/cloudstorage")
BACKUP_COUNT_TABLES = ("applications", "events", "actions", "review_queue",
                       "application_candidates", "raw_messages")


def _backup_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H%M%S")


def _is_cloud_synced(path: Path) -> bool:
    p = path.expanduser().resolve()
    lowered = str(p).lower()
    return (any(part.lower() in CLOUD_DIR_NAMES or part.lower().startswith("onedrive")
                for part in p.parts)
            or any(m in lowered for m in CLOUD_PATH_MARKERS))


def cmd_backup(conn, a):
    """The likeliest catastrophic failure here is not a race condition. It is
    `rm -rf ~/.jobtrack`. Put this in cron. (DESIGN D7)

    The .db snapshot is for recovery and holds everything, including raw
    email bodies unless --no-bodies. The .csv is an export of the pipeline.
    Both are written to a temp name and renamed, so a crash never leaves a
    half-written backup that looks complete."""
    dest = Path(a.dir).expanduser()
    no_bodies = bool(getattr(a, "no_bodies", False))
    if _is_cloud_synced(dest) and not no_bodies:
        print("warning: this backup contains raw email bodies and will sync to the cloud. "
              "Use a local folder, or pass --no-bodies.", file=sys.stderr)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = _backup_stamp()
    db_copy, csv_copy = dest / f"jobs-{stamp}.db", dest / f"jobs-{stamp}.csv"
    for final in (db_copy, csv_copy):
        if final.exists():
            sys.exit(f"error: {final} already exists; refusing to overwrite a backup")

    db_tmp = db_copy.with_name(db_copy.name + ".tmp")
    db_tmp.unlink(missing_ok=True)
    target = sqlite3.connect(db_tmp)
    try:
        conn.backup(target)      # consistent snapshot, safe on a live db
        if no_bodies:
            target.execute("UPDATE raw_messages SET body = NULL")
            target.commit()
            # NULLing leaves the old bytes in free pages; VACUUM rewrites the
            # file so the bodies are actually gone.
            target.execute("VACUUM")
    finally:
        target.close()
    csv_tmp = csv_copy.with_name(csv_copy.name + ".tmp")
    with open(csv_tmp, "w", newline="") as f:
        _write_csv(conn, csv.writer(f))
    db_tmp.replace(db_copy)
    csv_tmp.replace(csv_copy)
    kind = "without email bodies" if no_bodies else "including raw email bodies"
    print(f"backed up to {db_copy} ({kind}) and {csv_copy}")
    print(f"check it with: jt verify-backup {db_copy}")


def cmd_verify_backup(conn, a):
    """Open a backup read-only, run SQLite's integrity check, print what's in it."""
    path = Path(a.file).expanduser()
    if not path.exists():
        sys.exit(f"error: {path} not found")
    try:
        b = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            result = b.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                sys.exit(f"error: integrity check failed: {result}")
            tables = {r[0] for r in b.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            print(f"{path}: integrity ok")
            version = (b.execute("SELECT value FROM sync_state WHERE key='schema_version'").fetchone()
                       if "sync_state" in tables else None)
            print(f"  schema version   {version[0] if version else 'unknown'}")
            for t in BACKUP_COUNT_TABLES:
                if t in tables:
                    print(f"  {t:<24}{b.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
            if "raw_messages" in tables:
                with_body = b.execute(
                    "SELECT COUNT(*) FROM raw_messages WHERE body IS NOT NULL").fetchone()[0]
                print(f"  raw emails with bodies: {with_body}")
        finally:
            b.close()
    except sqlite3.DatabaseError as e:
        sys.exit(f"error: {path} is not a readable jt backup: {e}")


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _default_source_ref(source: str, raw_input: str, source_ref: str | None) -> str | None:
    source_ref = _clean_optional(source_ref)
    if source_ref:
        return source_ref
    if not raw_input:
        return None
    digest = hashlib.sha256(raw_input.encode("utf-8")).hexdigest()
    return f"{source}:{digest}"


def insert_application_candidate(
    conn,
    *,
    raw_input: str,
    source: str = "manual_capture",
    source_ref: str | None = None,
    company: str | None = None,
    role: str | None = None,
    lane: str | None = None,
    applied_on: str | None = None,
    contact_email: str | None = None,
    url: str | None = None,
    notes: str | None = None,
    commit: bool = True,
) -> tuple[int | None, bool]:
    source = _clean_optional(source) or "manual_capture"
    source_ref = _default_source_ref(source, raw_input, source_ref)
    company = _clean_optional(company)
    role = _clean_optional(role)
    lane = _clean_optional(lane)
    contact_email = _clean_optional(contact_email)
    url = _clean_optional(url)
    notes = _clean_optional(notes)
    if applied_on:
        applied_on = parse_day(applied_on)
    if lane and lane not in LANES:
        sys.exit(f"error: lane must be one of: {', '.join(LANES)}")

    def _insert():
        return conn.execute(
            """INSERT OR IGNORE INTO application_candidates
               (raw_input, source, source_ref, company, role, lane, applied_on,
                contact_email, url, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (raw_input, source, source_ref, company, role, lane, applied_on,
             contact_email, url, notes),
        )
    # commit=False means the caller already holds write_txn (the classifier).
    if commit:
        with write_txn(conn):
            cur = _insert()
    else:
        cur = _insert()
    if cur.rowcount:
        return cur.lastrowid, True
    existing = None
    if source_ref:
        existing = conn.execute(
            "SELECT id FROM application_candidates WHERE source_ref = ?",
            (source_ref,),
        ).fetchone()
    if existing is None and url:
        existing = conn.execute(
            "SELECT id FROM application_candidates WHERE url = ?",
            (url,),
        ).fetchone()
    return (existing["id"] if existing else None), False


def cmd_capture(conn, a):
    raw_input = " ".join(a.text) if a.text else sys.stdin.read()
    candidate_id, inserted = insert_application_candidate(
        conn,
        raw_input=raw_input,
        source=a.source,
        source_ref=a.source_ref,
        company=a.company,
        role=a.role,
        lane=a.lane,
        applied_on=a.on,
        contact_email=a.contact,
        url=a.url,
        notes=a.notes,
    )
    if inserted:
        print(f"captured candidate #{candidate_id}")
    else:
        print(f"already captured candidate #{candidate_id}; no-op")


def _candidate_application_args(candidate, a):
    return argparse.Namespace(
        company=a.company if a.company is not None else (candidate["company"] or ""),
        role=a.role if a.role is not None else (candidate["role"] or ""),
        lane=a.lane if a.lane is not None else (candidate["lane"] or ""),
        on=a.on if a.on is not None else candidate["applied_on"],
        source=a.source if a.source is not None else candidate["source"],
        contact=a.contact if a.contact is not None else candidate["contact_email"],
        url=a.url if a.url is not None else candidate["url"],
        notes=a.notes if a.notes is not None else candidate["notes"],
    )


def cmd_candidate_accept(conn, a):
    candidate = conn.execute(
        "SELECT * FROM application_candidates WHERE id = ?", (a.candidate_id,)
    ).fetchone()
    if not candidate:
        sys.exit(f"error: no application candidate #{a.candidate_id}")
    if candidate["status"] == "accepted":
        print(
            f"candidate #{a.candidate_id} already accepted"
            + (f" as application #{candidate['accepted_application_id']}"
               if candidate["accepted_application_id"] else "")
        )
        return
    if candidate["status"] == "rejected":
        print(f"candidate #{a.candidate_id} already rejected")
        return

    app_args = _candidate_application_args(candidate, a)
    with write_txn(conn):
        company, role, _, day = _application_fields(app_args)[:4]
        existing = _find_application_by_key(conn, company, role, day)
        if existing:
            sys.exit(f"error: matching application already exists as #{existing['id']}")
        cur = conn.execute(
            """UPDATE application_candidates
               SET status = 'accepted', decided_at = datetime('now')
               WHERE id = ? AND status = 'pending'""",
            (a.candidate_id,),
        )
        if cur.rowcount == 0:
            print(f"candidate #{a.candidate_id} was already decided")
            return
        app_id, fields = _insert_application(conn, app_args)
        conn.execute(
            """UPDATE application_candidates
               SET accepted_application_id = ?
               WHERE id = ?""",
            (app_id, a.candidate_id),
        )

    company, role, lane, day = fields[:4]
    print(f"accepted candidate #{a.candidate_id} as #{app_id}  {company} - {role}  [{lane}]  applied {day}")


def cmd_candidate_reject(conn, a):
    candidate = conn.execute(
        "SELECT * FROM application_candidates WHERE id = ?", (a.candidate_id,)
    ).fetchone()
    if not candidate:
        sys.exit(f"error: no application candidate #{a.candidate_id}")
    with write_txn(conn):
        cur = conn.execute(
            """UPDATE application_candidates
               SET status = 'rejected',
                   rejected_note = ?,
                   decided_at = datetime('now')
               WHERE id = ? AND status = 'pending'""",
            (a.note, a.candidate_id),
        )
    if cur.rowcount == 0:
        print(f"candidate #{a.candidate_id} was already decided")
    else:
        print(f"rejected candidate #{a.candidate_id}")


def cmd_candidates(conn, a):
    if getattr(a, "candidate_cmd", None) == "accept":
        return cmd_candidate_accept(conn, a)
    if getattr(a, "candidate_cmd", None) == "reject":
        return cmd_candidate_reject(conn, a)
    rows = conn.execute(
        """SELECT * FROM application_candidates
           WHERE status = 'pending'
           ORDER BY id"""
    ).fetchall()
    out = []
    for r in rows:
        preview = (r["raw_input"] or "").replace("\n", " ")[:36]
        out.append([
            r["id"], r["created_at"][:10], r["source"],
            (r["company"] or "-")[:24], (r["role"] or "-")[:28],
            r["lane"] or "-", r["applied_on"] or "-", r["url"] or preview,
        ])
    print()
    print(table(["#", "CREATED", "SOURCE", "COMPANY", "ROLE", "LANE", "APPLIED", "REF"], out))
    print(f"\n  {len(rows)} pending candidate(s)\n")


def cmd_review_item(conn, a):
    if a.queue_id is None:
        sys.exit(f"error: review {a.review_cmd} requires a queue ID")
    # Acquire the writer lock before reading the decision and application state.
    # `show` only reads, so it skips the lock.
    ctx = contextlib.nullcontext(conn) if a.review_cmd == "show" else write_txn(conn)
    with ctx:
        row = conn.execute("SELECT * FROM review_queue WHERE id = ?", (a.queue_id,)).fetchone()
        if row is None:
            sys.exit(f"error: no review queue item #{a.queue_id}")
        if a.review_cmd == "show":
            msg = conn.execute(
                "SELECT subject, received_on FROM raw_messages WHERE gmail_msg_id = ?",
                (row["gmail_msg_id"],),
            ).fetchone()
            print(f"#{row['id']} {'resolved' if row['resolved'] else 'pending'}: {row['reason']}")
            if msg:
                print(f"{msg['received_on'] or '-'}  {msg['subject'] or '-'}")
            print(row["proposed_json"])
            return
        if row["resolved"]:
            if getattr(a, "json", False):
                sys.exit(f"error: queue item {a.queue_id} was already resolved")
            print(f"queue item {a.queue_id} was already resolved")
            return
        event_id = None
        if a.review_cmd == "approve":
            proposal = json.loads(row["proposed_json"])
            if not isinstance(proposal, dict) or not a.app_id:
                sys.exit("error: approval requires a proposal and an explicit --app-id")
            app = conn.execute("SELECT * FROM applications WHERE id = ?", (a.app_id,)).fetchone()
            if app is None:
                sys.exit(f"error: no application #{a.app_id}")
            company = proposal.get("company")
            if isinstance(company, str) and company.strip():
                proposed = " ".join(company.casefold().split())
                selected = " ".join(app["company"].casefold().split())
                if proposed != selected and not (a.override_company and (a.note or "").strip()):
                    sys.exit(
                        f"error: proposed company {company!r} differs from selected application "
                        f"{app['company']!r} ({app['role']}). "
                        "Use --override-company with --note to record a deliberate correction."
                    )
            kind = proposal.get("kind")
            if not isinstance(kind, str) or kind == "note" or kind not in EVENT_KINDS:
                sys.exit("error: invalid proposed event kind")
            legal, reason = is_legal(derive(conn, app)["status"], kind)
            if not legal:
                sys.exit(f"error: {reason}")
            proposed_day = proposal.get("occurred_on")
            if not isinstance(proposed_day, str) or not proposed_day:
                sys.exit("error: proposal has no event date")
            day = parse_day(proposed_day)
            if day < app["applied_on"] or day > date.today().isoformat():
                sys.exit("error: proposed event date is outside the application timeline")
            raw = conn.execute("SELECT body FROM raw_messages WHERE gmail_msg_id = ?",
                               (row["gmail_msg_id"],)).fetchone()
            evidence = proposal.get("evidence")
            if not raw or not isinstance(evidence, str) or not evidence_in_body(evidence, raw["body"]):
                sys.exit("error: proposed evidence must appear in the source email")
            try:
                cursor = conn.execute(
                    """INSERT INTO events (application_id, occurred_on, kind, evidence, gmail_msg_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (a.app_id, day, kind, evidence, row["gmail_msg_id"]),
                )
            except sqlite3.IntegrityError as exc:
                sys.exit(f"error: could not approve review item: {exc}")
            event_id = cursor.lastrowid
        conn.execute(
            """UPDATE review_queue SET resolved = 1, resolved_event_id = ?,
               resolved_note = ?, resolved_at = datetime('now') WHERE id = ?""",
            (event_id, a.note, a.queue_id),
        )
    if getattr(a, "json", False):
        return _print_ok(review_id=a.queue_id, resolved=True, event_id=event_id)
    print(f"resolved queue item {a.queue_id}")


def cmd_review(conn, a):
    if getattr(a, "review_cmd", None) == "stale":
        return cmd_review_stale(conn, a)
    if getattr(a, "review_cmd", None) in {"show", "approve", "dismiss"}:
        return cmd_review_item(conn, a)
    rows = conn.execute(
        "SELECT * FROM review_queue WHERE resolved = 0 ORDER BY id").fetchall()
    if not rows:
        print("\n  review queue empty\n")
        return
    print()
    for r in rows:
        print(f"  [{r['id']}] {r['reason']}\n      {r['proposed_json']}")
    print(f"\n  {len(rows)} pending. Inspect with jt review show <id>.\n")


def cmd_review_stale(conn, a):
    alerts, stats = review_alerts(
        conn,
        log_path=Path(a.log) if a.log else None,
        queue_threshold=a.queue_threshold,
        age_days=a.age_days,
        failure_threshold=a.failure_threshold,
    )
    review = stats["review"]
    classify = stats["classify"]
    print(
        "review health: "
        f"total={review['total']} "
        f"oldest_age_days={review['oldest_age_days']} "
        f"oldest_created_at={review['oldest_created_at'] or '-'}"
    )
    print(
        "classify health: "
        f"consecutive_failures={classify['consecutive_failures']} "
        f"classifier_exhaustion={'yes' if classify['has_classifier_exhaustion'] else 'no'} "
        f"log={classify['log_path']}"
    )
    if alerts:
        for alert in alerts:
            print(f"ALERT {alert}")
        if not a.no_fail:
            sys.exit(1)
        print("review health alerts present")
        return
    print("review health OK")


def cmd_resolve(conn, a):
    with write_txn(conn):
        row = conn.execute(
            "SELECT * FROM review_queue WHERE id = ?", (a.queue_id,)).fetchone()
        if not row:
            sys.exit(f"error: no review queue item #{a.queue_id}")
        if a.event_id:
            event = conn.execute("SELECT id FROM events WHERE id = ?", (a.event_id,)).fetchone()
            if not event:
                sys.exit(f"error: no event #{a.event_id}")
        cur = conn.execute(
            """UPDATE review_queue
               SET resolved = 1,
                   resolved_event_id = ?,
                   resolved_note = ?,
                   resolved_at = datetime('now')
               WHERE id = ? AND resolved = 0""",
            (a.event_id, a.note, a.queue_id),
        )
    if cur.rowcount == 0:
        print(f"queue item {a.queue_id} was already resolved")
    else:
        print(f"resolved queue item {a.queue_id}")


def cmd_where(conn, a):
    print(db_path())


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jt", description="job application tracker")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="audit Gmail coverage without importing messages")
    s.add_argument("--audit", action="store_true", required=True)
    s.add_argument("--since", required=True, metavar="YYYY-MM-DD")

    s = sub.add_parser("add", help="log one application")
    s.add_argument("company"); s.add_argument("role")
    s.add_argument("--lane", required=True, choices=LANES)
    s.add_argument("--on"); s.add_argument("--source")
    s.add_argument("--contact"); s.add_argument("--url"); s.add_argument("--notes")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("bulk", help="stdin: 'Company | Role | lane [| url]' per line")
    s.add_argument("--on"); s.set_defaults(fn=cmd_bulk)

    s = sub.add_parser("list", help="show open applications")
    s.add_argument("--lane", choices=LANES); s.add_argument("--status")
    s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_list)

    s = sub.add_parser("stale", help="what needs action today")
    s.set_defaults(fn=cmd_stale)

    s = sub.add_parser("priority", help="rank open applications by next-action value")
    s.add_argument("--explain", type=int, metavar="ID")
    s.add_argument("--today", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_priority)

    s = sub.add_parser("log", help="append an event")
    s.add_argument("id", type=int)
    s.add_argument("kind", choices=sorted(EVENT_KINDS))
    s.add_argument("--on"); s.add_argument("--note")
    s.add_argument("--force", action="store_true",
                    help="log even if illegal per transitions.py (deliberate manual correction)")
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("show", help="full history for one application")
    s.add_argument("id", type=int); s.set_defaults(fn=cmd_show)

    s = sub.add_parser("rm", help="delete an application (only if it has no events)")
    s.add_argument("id", type=int); s.set_defaults(fn=cmd_rm)

    s = sub.add_parser("export", help="CSV out (Pursuit: --lane swe)")
    s.add_argument("--lane", choices=LANES); s.add_argument("--out")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("backup", help="snapshot db + csv to a directory")
    s.add_argument("dir")
    s.add_argument("--no-bodies", action="store_true",
                   help="blank raw email bodies in the snapshot (safer for cloud folders)")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("verify-backup", help="integrity-check a backup and show its counts")
    s.add_argument("file"); s.set_defaults(fn=cmd_verify_backup)

    s = sub.add_parser("capture", help="capture a possible application from stdin/text")
    s.add_argument("text", nargs="*")
    s.add_argument("--source", default="manual_capture")
    s.add_argument("--source-ref")
    s.add_argument("--company")
    s.add_argument("--role")
    s.add_argument("--lane", choices=LANES)
    s.add_argument("--on")
    s.add_argument("--contact")
    s.add_argument("--url")
    s.add_argument("--notes")
    s.set_defaults(fn=cmd_capture)

    s = sub.add_parser("candidates", help="pending captured application candidates")
    cand = s.add_subparsers(dest="candidate_cmd")
    s.set_defaults(fn=cmd_candidates)

    c = cand.add_parser("accept", help="accept one candidate as an application")
    c.add_argument("candidate_id", type=int)
    c.add_argument("company", nargs="?")
    c.add_argument("role", nargs="?")
    c.add_argument("--lane", choices=LANES)
    c.add_argument("--on")
    c.add_argument("--source")
    c.add_argument("--contact")
    c.add_argument("--url")
    c.add_argument("--notes")
    c.set_defaults(fn=cmd_candidates)

    c = cand.add_parser("reject", help="reject one candidate")
    c.add_argument("candidate_id", type=int)
    c.add_argument("--note")
    c.set_defaults(fn=cmd_candidates)

    s = sub.add_parser("action", help="add, list, finish, or snooze actions")
    act = s.add_subparsers(dest="action_cmd", required=True)
    c = act.add_parser("add", help="add an action to an application")
    c.add_argument("id", type=int, help="application id")
    c.add_argument("description")
    c.add_argument("--due", required=True, help="YYYY-MM-DD, or today/tomorrow")
    c.add_argument("--at", help="HH:MM wall-clock time (makes it a timed action)")
    c.add_argument("--tz", help="IANA zone for --at (default: $JT_TZ or this Mac's zone)")
    c.add_argument("--reason", help="why this exists; shown in the panel later")
    c.add_argument("--json", action="store_true")
    for name, help_ in (("done", "mark an action done"), ("dismiss", "it no longer applies"),
                        ("reopen", "reopen a snoozed action now")):
        c = act.add_parser(name, help=help_)
        c.add_argument("action_id", type=int)
        c.add_argument("--json", action="store_true")
    c = act.add_parser("snooze", help="hide an action until a date/time")
    c.add_argument("action_id", type=int)
    c.add_argument("--until", required=True, help="YYYY-MM-DD or 'YYYY-MM-DD HH:MM'")
    c.add_argument("--json", action="store_true")
    c = act.add_parser("list", help="pending actions (or --all)")
    c.add_argument("--all", action="store_true")
    c.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_action)

    s = sub.add_parser("next-action", help="(older form) add an action, or --clear them")
    s.add_argument("id", type=int)
    s.add_argument("description", nargs="?")
    action_mode = s.add_mutually_exclusive_group(required=True)
    action_mode.add_argument("--due")
    action_mode.add_argument("--clear", action="store_true")
    s.set_defaults(fn=cmd_next_action)

    s = sub.add_parser("draft-followup", help="print a local follow-up draft")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_draft_followup)

    s = sub.add_parser("today", help="show due actions, follow-ups and review counts")
    s.add_argument("--on")
    s.add_argument("--json", action="store_true",
                   help="print the versioned read model a frontend consumes (DESIGN D5)")
    s.set_defaults(fn=cmd_today)

    s = sub.add_parser("review", help="pending classifier proposals (Phase 3)")
    s.add_argument("review_cmd", nargs="?", choices=["stale", "show", "approve", "dismiss"])
    s.add_argument("queue_id", nargs="?", type=int)
    s.add_argument("--app-id", type=int)
    s.add_argument("--override-company", action="store_true")
    s.add_argument("--note")
    s.add_argument("--queue-threshold", type=int, default=None)
    s.add_argument("--age-days", type=int, default=None)
    s.add_argument("--failure-threshold", type=int, default=None)
    s.add_argument("--log")
    s.add_argument("--no-fail", action="store_true")
    s.add_argument("--json", action="store_true", help="approve/dismiss: print a JSON result")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("resolve", help="mark a review item handled")
    s.add_argument("queue_id", type=int)
    s.add_argument("--event-id", type=int)
    s.add_argument("--note")
    s.set_defaults(fn=cmd_resolve)

    s = sub.add_parser("where", help="print database path")
    s.set_defaults(fn=cmd_where)
    return p


def _run(a) -> None:
    if a.cmd == "sync":
        from sync import audit_missing

        audit_missing(a.since)
        return
    conn = connect()
    try:
        a.fn(conn, a)
    except SystemExit as e:
        # --json callers (a menu-bar panel) get JSON on failure too, and a
        # non-zero exit. Text callers keep the plain error message.
        if getattr(a, "json", False) and e.code not in (None, 0):
            msg = e.code if isinstance(e.code, str) else f"exit status {e.code}"
            print(json.dumps({"ok": False, "error": msg.removeprefix("error: ").strip()}))
            raise SystemExit(1) from None
        raise
    finally:
        conn.close()


def main_args(argv: list[str] | None = None) -> None:
    _run(build_parser().parse_args(argv))


def main():
    _run(build_parser().parse_args())


if __name__ == "__main__":
    main()
