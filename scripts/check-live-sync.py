#!/usr/bin/env python3
"""Verify live Gmail import and deduplication in a disposable database."""

import argparse
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jt
import sync


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="Start date in YYYY-MM-DD format")
    args = parser.parse_args()
    since = jt.parse_day(args.since)

    with TemporaryDirectory(prefix="jt-sync-check-") as directory:
        os.environ["JT_DB"] = str(Path(directory) / "jobs.db")
        service = sync.build_gmail_service()
        with jt.connect() as conn:
            first_seen, first_inserted = sync.sync_messages(conn, service, since=since)
            second_seen, second_inserted = sync.sync_messages(conn, service, since=since)
            total = conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0]

    print(f"First scan: {first_seen} seen, {first_inserted} inserted")
    print(f"Second scan: {second_seen} seen, {second_inserted} inserted")
    if first_inserted < 1 or second_seen < 1 or second_inserted or total != first_inserted:
        raise SystemExit("Live sync check failed: expected a nonempty import and zero repeat inserts.")
    print("Live sync check passed.")


if __name__ == "__main__":
    main()
