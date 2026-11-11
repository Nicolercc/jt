#!/usr/bin/env bash
# Replay the core workflow with synthetic data and no provider credentials.
set -euo pipefail
cd "$(dirname "$0")/.."
demo_dir="$(mktemp -d)"
trap 'rm -rf -- "$demo_dir"' EXIT
export JT_DB="$demo_dir/jobs.db"
python_bin="${PYTHON_BIN:-python3}"

"$python_bin" jt.py capture "Applied to Example Co for UI Engineer"
"$python_bin" jt.py candidates accept 1 "Example Co" "UI Engineer" --lane swe \
  --on 2026-08-01 --contact recruiter@example.com
"$python_bin" jt.py add "Example Co" "Platform Engineer" --lane swe --on 2026-08-01

"$python_bin" - <<'PY'
import json

import classify
import jt

with jt.connect() as conn:
    conn.execute(
        """INSERT INTO raw_messages (gmail_msg_id, received_on, subject, body)
           VALUES ('demo-email', '2026-08-02', 'Next steps', 'Can we schedule a call?')"""
    )
    conn.commit()
    messages = classify.load_unprocessed(conn, 1)
    proposal = {
        "gmail_msg_id": "demo-email",
        "company": "Example Co",
        "role_hint": "",
        "kind": "screen",
        "occurred_on": "2026-08-02",
        "confidence": 0.99,
        "evidence": "schedule a call",
    }
    verdicts = classify.verdicts_for(conn, messages, json.dumps([proposal]))
    assert classify.apply_verdicts(conn, verdicts) == (0, 1)
PY

"$python_bin" jt.py review show 1
"$python_bin" jt.py review approve 1 --app-id 1
"$python_bin" jt.py next-action 1 "Send availability" --due 2026-08-03
"$python_bin" jt.py today --on 2026-08-03
"$python_bin" jt.py draft-followup 1
