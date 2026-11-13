# jt architecture (as built)

**Status:** describes `main` at `59c1184` (2026-11-13), schema v8, 304 tests.
**Companion docs:** [PRD.md](PRD.md) is what we build and why. [DESIGN.md](DESIGN.md) is the decision log (D1–D7). This file is how it works today.

---

## 1. Principles

These are load-bearing. Changing one is a design decision, not a refactor.

1. **The model proposes; deterministic code commits.** Model output is untrusted input, validated like a form from a stranger.
2. **Never guess.** Anything ambiguous, low-confidence, or consequential goes to review instead of the database.
3. **History is append-only; status is derived.** No status column exists for anything to overwrite.
4. **Local-first.** Data lives on this Mac. The only outbound traffic is the Gmail API (read-only) and the classifier API call.
5. **One read contract.** Every UI reads `jt today --json` and writes through `jt … --json` commands. No UI opens the database.

## 2. System overview

```
Gmail (gmail.readonly)
   │  sync.py — hourly via launchd; watermark minus 2-day overlap
   ▼
raw_messages ─────────────► classify.py ──► Anthropic API (tool call, JSON schema)
                               │
                               ▼  validate_proposal(): types, verbatim evidence,
                               │  confidence, company/role match, transition
                               │  legality, D3 policy, request deadline checks
                 ┌─────────────┼──────────────────────┐
                 ▼             ▼                      ▼
              events        actions              review_queue / application_candidates
          (append-only)  (to-dos; email or manual)   (human decides)
                 └─────────────┬──────────────────────┘
                               ▼
                 jt.py: derive() → today_entries() → today_snapshot()
                               │  `jt today --json` (schema v1)
          ┌────────────────────┼──────────────────────┐
          ▼                    ▼                      ▼
      CLI (jt …)      SwiftBar panel (live)    Native star app (draft PRs, paused)
```

## 3. Components

| Component | File(s) | Responsibility |
|---|---|---|
| Tracker core | `jt.py` (~2,100 lines) | Schema and migrations, `write_txn`, status derivation, actions, Today read model, CLI, backups |
| Transition rules | `transitions.py` | The allowed-transition table. **Owned by a person; agents never edit it.** |
| Gmail sync | `sync.py` | OAuth, paginated fetch, retry (429, 5xx, 403 rate limit), watermark, `--audit` coverage check |
| Classifier | `classify.py` | Prompt and tool schema, validation pipeline, D3 policy, request → action, batch apply |
| Eval | `evals/` | Labeling page (localhost-only), dataset build, frozen dev/holdout split, scoring with Wilson intervals, `--policy compare` |
| Panel | `scripts/swiftbar/jt.5m.py` | Menu-bar UI; cached last-good snapshot; usage log |
| Automation | `launchd/`, `scripts/jt-automation.sh` | Hourly sync and classify; daily stale check |
| Native app (draft) | `mac/` on PRs #21/#22 | SwiftUI menu-bar app; **paused at M6** |

## 4. Data model (schema v8)

| Table | Purpose | Key rules |
|---|---|---|
| `applications` | What you applied to | `UNIQUE(company, role, applied_on)`; no status column |
| `events` | Status history | **Append-only** (triggers block UPDATE and DELETE); `gmail_msg_id UNIQUE`; `kind` FK to `event_kinds` |
| `actions` | Things to do | open, done, snoozed or dismissed; all-day **or** timed (UTC + IANA zone); `source_gmail_msg_id` with a UNIQUE partial index (one action per email); origin manual, review, migrated or email |
| `review_queue` | Proposals that need you | reason, proposal JSON, resolution audit fields |
| `application_candidates` | Possible applications jt found | Never authoritative until you accept one |
| `raw_messages` | Synced email | `processed` flag; bodies stay local |
| `suggestion_dismissals` | Hidden follow-up nudges | per application and kind, until a date |
| `sync_state` | Watermark, freshness, schema version | key/value |

**Migration chain:** v1→v2 (events rebuilt as append-only) · v6 (`next_actions` → `actions`) · v7 (`actions.source_gmail_msg_id`, table rebuilt for the CHECK) · v8 (`suggestion_dismissals`). Every migration runs inside `write_txn`, and each runs once.

## 5. Core flows

**Sync (hourly).** Query = `GMAIL_QUERY` from `~/.jobtrack/jt.env`, or the default (label plus assessment and interview platforms and subjects), after `watermark − 2 days`. Inserts use `INSERT OR IGNORE`; the watermark only moves forward; `gmail_synced_at` is recorded. `jt sync --audit --since DATE` lists matching mail missing from the tracker and never writes.

**Classify (hourly, after sync).** For a batch of unprocessed messages:
1. One model call, outside any lock.
2. Validate each proposal: field types (an all-empty `request` is treated as none) → verbatim evidence → confidence ≥ 0.85 → company and role match → transition legality → D3 policy (terminal kinds and offer go to review) → request deadline checks.
3. Apply everything in **one `write_txn`**: recheck legality inside the lock, insert the event and the action together, write review items, mark messages processed, record `classified_at`.

**Review.** `jt review show|approve|dismiss` reads and decides inside the lock. Approval re-runs legality and the evidence check.

**Action lifecycle.** open → done or dismissed (closed, with a timestamp) · open → snoozed (until) → open. Any pending action suppresses FOLLOW UP and CLOSE OUT suggestions for that application. Terminal applications hide their actions without deleting them.

**Today read model.** `today_snapshot()` returns: `badge` (due count, review count), `freshness` (gmail_synced_at, classified_at, sync_error), `actions` (due or overdue), `suggestions` (nudges, excluding dismissed ones), `recorded` (the 3 newest email events), and `review`.

## 6. Invariants (enforced in code and tests)

1. Events are append-only. `INSERT OR REPLACE` on events is forbidden; the `recursive_triggers` pragma is set in `connect()`.
2. Status is derived, never stored.
3. One event per email (`events.gmail_msg_id UNIQUE`), and one action per email (UNIQUE partial index).
4. Every write goes through `write_txn` and reads its deciding state inside the lock. No network calls inside it.
5. The classifier never creates applications and never writes `note` events.
6. Consequential kinds never auto-commit unless `CLASSIFIER_AUTO_COMMIT_TERMINAL=1`.
7. A request deadline is accepted only if quoted verbatim (whitespace-tolerant) and between arrival and +120 days.
8. The Today JSON is versioned. Renaming or removing a field bumps `schema_version`.

## 7. Failure behavior

| Failure | Behavior | Where you see it |
|---|---|---|
| Gmail offline, sign-in expired, other error | Sync logs the failure; no partial watermark move | Panel title ⚠ plus a plain-language line; `freshness.sync_error` |
| Gmail rate limit (403 or 429) | Retry with ≥ 20 s waits, up to 5 attempts | `jt-sync.jsonl` |
| Model down or malformed output | No events or actions written; messages route to review or stay unprocessed | `jt-classify.jsonl`, review count |
| Concurrent writers | `BEGIN IMMEDIATE` serializes them; the loser rechecks and goes to review | Race test |
| Panel can't read jt | Shows the cached snapshot with its age; write buttons disabled | Panel |
| Failed click | Retry item; a retry after a lost response doesn't double-write | Panel |
| Bad `$TZ` | Falls back to the Mac's zone (`$JT_TZ` stays strict) | n/a |

## 8. Deployment

| Thing | Where |
|---|---|
| Dev checkout | `~/Documents/ChatGPT/building/jt-work` (plus extra Codex copies; see debt) |
| **Production** | `~/Developer/jt` with `.venv`; run by launchd (`dev.nicole.jt.sync` hourly, `dev.nicole.jt.daily-stale` at 8:30) |
| Data | `~/.jobtrack/jobs.db`, logs in `~/.jobtrack/logs/`, panel cache and usage log in `~/.jobtrack/` |
| Config | `~/.jobtrack/jt.env` (OAuth paths, `GMAIL_QUERY`, model, API key). Mode 600. |
| Panel | `~/SwiftBar/jt.5m.py` is a symlink into the production checkout |
| Backups | `~/jobtrack-backups/jobs-<timestamp>.db/.csv`; `jt verify-backup` |
| CI | GitHub Actions: pytest + mypy on Python 3.11 and 3.12. **Required on `main`** (branch protection, admins included, no force pushes). |

**Deploy procedure (today, manual):** `jt backup` → `jt verify-backup` → `git -C ~/Developer/jt pull --ff-only` → `launchctl kickstart -k gui/$UID/dev.nicole.jt.sync` → confirm `schema_version`, `gmail_synced_at` and `classified_at` updated and the last sync log line says `success`.

## 9. Security and privacy

- Gmail scope is `gmail.readonly`. OAuth client and token files live outside the repo. *(Debt: the token is a file, not in Keychain.)*
- The labeling page binds to 127.0.0.1, checks the Host header, and requires a JSON same-origin save.
- Private data is never committed: labels, fixtures, pool and live databases are gitignored, and `scripts/secret-scan.sh` runs before every commit.
- Backups include email bodies by default, warn when the target is a cloud folder, and `--no-bodies` vacuums them out.
- The panel title never shows company names.

## 10. Known technical debt (ranked)

| # | Debt | Impact | Fix |
|---|---|---|---|
| 1 | **Classifier evidence is thin:** 23 labeled emails, 5 in the holdout | Accuracy claims can't be trusted yet | Labeling (PRD Now-3) |
| 2 | **Two roles at one company stall in review** (both Ramp CodeSignal invites) | The flagship case still needs manual work | Same-company role resolution (PRD Now-E1) |
| 3 | **Review gets noisy:** "no proposal" non-job mail lands in review | Review fatigue with the wider search | An "ignored" bucket (PRD Now-E2) |
| 4 | Manual deploy, no `jt version` | Production fell 6 commits behind once | `jt version` + `scripts/deploy.sh` (PRD Now-E3) |
| 5 | Workspace sprawl: 13 local copies; Codex drafts #17–#22 stale against v8 | Confusion, merge hazards | Prune merged copies; close or rebase the drafts |
| 6 | `jt sync` (audit only) vs `sync.py` (real sync) naming | Confusing CLI | Rename to `jt audit-gmail`, or fold sync into jt |
| 7 | OAuth token stored in a file; publishing status unconfirmed | 7-day expiry risk in Testing mode | Check the console; Keychain later |
| 8 | Repo auto-merge disabled | Merges need a manual step after CI | Optional repo setting |
| 9 | No Swift CI | Native drafts can rot unnoticed | Only if M6 says go |
