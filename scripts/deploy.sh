#!/usr/bin/env bash
# Deploy main to the production checkout, safely. PRD Now-E3.
#
#   scripts/deploy.sh            back up, verify, pull, run the hourly job, verify
#   scripts/deploy.sh --dry-run  show what would be deployed; change nothing
#
# Refuses to run if the production checkout has uncommitted changes, and stops
# before pulling if the backup or its integrity check fails.
set -euo pipefail

REPO="${JT_DEPLOY_REPO:-$HOME/Developer/jt}"
PY="${JT_PYTHON:-$REPO/.venv/bin/python}"
BACKUP_DIR="${JT_BACKUP_DIR:-$HOME/jobtrack-backups}"
LAUNCHCTL="${JT_LAUNCHCTL:-launchctl}"
LABEL="${JT_LAUNCHD_LABEL:-dev.nicole.jt.sync}"
LOG="${JT_LOG_DIR:-$HOME/.jobtrack/logs}/jt-sync.jsonl"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

fail() { echo "deploy: $*" >&2; exit 1; }
cd "$REPO" || fail "no production checkout at $REPO"

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  fail "production checkout $REPO has uncommitted changes; refusing to deploy over them"
fi

git fetch -q origin main
incoming="$(git log --oneline HEAD..origin/main)"
if [[ -z "$incoming" ]]; then
  echo "Already up to date at $(git rev-parse --short HEAD)."
  exit 0
fi

echo "Commits to deploy:"
echo "$incoming" | sed 's/^/  /'
if (( DRY_RUN )); then
  echo "Dry run: would back up to $BACKUP_DIR, verify it, pull --ff-only,"
  echo "run $LABEL once, and check that sync and classify succeed. Nothing changed."
  exit 0
fi

echo "1/4 Backing up the live database..."
"$PY" jt.py backup "$BACKUP_DIR" >/dev/null || fail "backup failed; nothing was deployed"
latest="$(ls -t "$BACKUP_DIR"/jobs-*.db 2>/dev/null | head -1)"
[[ -n "$latest" ]] || fail "backup file not found in $BACKUP_DIR; nothing was deployed"
"$PY" jt.py verify-backup "$latest" >/dev/null || fail "backup integrity check failed; nothing was deployed"
echo "    $latest (verified)"

echo "2/4 Pulling..."
git pull -q --ff-only || fail "pull failed (not a fast-forward?); the backup is at $latest"
echo "    now at $("$PY" jt.py version)"

echo "3/4 Running the hourly job once..."
fresh() { "$PY" jt.py today --json | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["freshness"]["classified_at"])'; }
before="$(fresh)"
"$LAUNCHCTL" kickstart -k "gui/$(id -u)/$LABEL"
for _ in $(seq 1 120); do
  [[ "$(fresh)" != "$before" ]] && break
  sleep 1
done
[[ "$(fresh)" != "$before" ]] || fail "the job didn't finish within 2 minutes; check $LOG (backup: $latest)"

echo "4/4 Checking the result..."
last="$(tail -1 "$LOG")"
[[ "$last" == *'"event": "success"'* ]] || fail "last sync did not succeed: $last (backup: $latest)"
echo "Deployed $("$PY" jt.py version). Sync and classify succeeded."
