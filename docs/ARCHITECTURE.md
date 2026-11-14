# jt architecture (as built)

**Status:** describes `main` after PR #49 (2026-11-13): schema v8, 419 tests, 85% line coverage.
**Companion docs:** [PRD.md](PRD.md) is what we build and why. [DESIGN.md](DESIGN.md) is the decision log (D1–D7). This file is how it works today. **Every PR that changes behavior updates this file and the PRD row** (see `.github/pull_request_template.md`).

---

## 1. Principles

These are load-bearing. Changing one is a design decision, not a refactor.

1. **The model proposes; deterministic code commits.** Model output is untrusted input, validated like a form from a stranger.
2. **Never guess.** Anything ambiguous, low-confidence, or consequential goes to you instead of the database. When jt *does* infer something (a role), it says so.
3. **History is append-only; status is derived.** There is no status column for anything to overwrite.
4. **Local-first.** Data lives on this Mac. The only outbound traffic is the Gmail API (read-only) and the classifier API call.
5. **One read contract.** Every UI reads `jt today --json` and writes through `jt … --json` commands. No UI opens the database.
6. **A shown button always works.** Menu choices are computed with the same checks the write path runs.
7. **Nothing private in the repo.** The repository is public. Real companies, roles and outcomes never appear in code, tests, docs or PR text; real cases are anonymized first.

## 2. System overview

```
Gmail (gmail.readonly)
   │  sync.py: hourly via launchd; watermark minus 2-day overlap
   ▼
raw_messages ─────────────► classify.py ──► Anthropic API (tool call, JSON schema)
                               │
                               ▼  validate_proposal(): types, verbatim evidence (subject or body),
                               │  confidence, company/role match (+ evidence-based role inference),
                               │  transition legality, D3 policy, request deadline checks
            ┌──────────────┬───┴──────────────┬──────────────────────┬─────────────────┐
            ▼              ▼                  ▼                      ▼                 ▼
         events         actions          review_queue        application_candidates   ignored
     (append-only)  (to-dos; merged     (you decide)         (unknown company)    (resolved review
                     reminders)                                                     rows, restorable)
            └──────────────┴──────────────────┼──────────────────────┘
                                              ▼
                      jt.py: derive() → today_entries() → today_snapshot()  ──► jt today --json (v1)
                                              │
            ┌──────────────────┬──────────────┴───────────┬────────────────────────┐
            ▼                  ▼                          ▼                        ▼
        CLI (jt …)    SwiftBar panel (live)        jt notify (macOS          Native app
                                                   notifications, hourly)   (PRs #21/#22, frozen)
```

## 3. Components

| Component | File(s) | Responsibility |
|---|---|---|
| Tracker core | `jt.py` (~2,400 lines) | Schema and migrations (incl. self-repair), `write_txn`, status derivation, actions, Today read model, review items, notifications, CLI, backups, version |
| Transition rules | `transitions.py` | Allowed transitions. **Owned by Nicole; agents never edit without her decision.** |
| Gmail sync | `sync.py` | OAuth, paginated fetch, retry (429, 5xx, 403 rate limit), watermark, freshness, `jt sync --audit` |
| Classifier | `classify.py` | Tool schema, validation pipeline, role inference, D3 policy, request → action with reminder merging, ignored bucket, candidates |
| Eval | `evals/` | Localhost labeling page (host-checked), dataset build, frozen split, scoring with Wilson intervals, `--policy compare` |
| Panel | `scripts/swiftbar/jt.5m.py` | Menu bar: due tasks, review items, new applications, close-outs; cached last-good snapshot; usage log |
| Automation | `scripts/jt-automation.sh`, `launchd/` | Hourly: **notify first**, then sync, classify, review health, stale summary |
| Deploy | `scripts/deploy.sh` | Backup → verify → pull → run the job → check success; refuses a dirty production checkout |

## 4. Data model (schema v8)

| Table | Purpose | Key rules |
|---|---|---|
| `applications` | Where you applied | `UNIQUE(company, role, applied_on)`; no status column |
| `events` | Status history | **Append-only** (triggers); `gmail_msg_id UNIQUE`; `kind` FK to `event_kinds` |
| `actions` | Things to do | open / done / snoozed / dismissed; all-day **or** timed (UTC + IANA zone); one per source email (unique partial index); origin manual / review / migrated / email |
| `review_queue` | Proposals that need you | Reason, proposal JSON, resolution audit fields. Auto-ignored mail is a resolved row with an `auto-ignored` note. |
| `application_candidates` | Applications found in email | Pending until you add (pick a lane) or say "not mine" |
| `raw_messages` | Synced email | `processed` flag; bodies stay local |
| `suggestion_dismissals` | Hidden suggestions | Per application and kind, until a date |
| `sync_state` | Watermark, freshness, schema version, sent-notification keys | key/value |

**Migrations, all inside `write_txn` and each run once:** v1→v2 (append-only events) · v6 (`next_actions` → `actions`) · v7 (`actions.source_gmail_msg_id`, table rebuilt for the CHECK) · v8 (`suggestion_dismissals`) · **self-repair:** an `application_candidates` table of an older, unrelated shape is rebuilt (rows kept in `application_candidates_legacy`).

## 5. Core flows

**Sync (hourly).** Query = `GMAIL_QUERY` (in `~/.jobtrack/jt.env`) or the default (label + assessment/interview platforms and subjects), after `watermark − 2 days`. `INSERT OR IGNORE`; the watermark only moves forward; `gmail_synced_at` is recorded on success.

**Classify (hourly).** One model call outside any lock, then per proposal:
1. Normalize: an all-empty `request` object counts as none.
2. Validate: verbatim evidence from **subject or body** → confidence ≥ 0.85 → company/role match. When two roles at one company fit and the email doesn't say which, **for progress kinds only**: skip closed applications, else pick the one application whose process has moved past "confirmed"; otherwise review. → transition legality → D3 (terminal kinds + offer go to review).
3. Apply in one `write_txn`: recheck legality inside the lock; write the event; write the request as an action (deadline quote verbatim, date between arrival and +120 days; **a matching open task is updated instead of duplicated**, a recently closed one is skipped); unknown company → candidate; "found nothing" from a non-recruiting, non-applied sender → ignored; everything else → review; mark processed; record `classified_at`.

**Review.** `review_items()` turns each pending item into a plain-English summary plus **only the choices `jt review approve` would accept** (same company, date after applying, legal transition, matching evidence), each carrying the stage it was computed at. `approve --expected-stage` refuses if the application moved on since. `add-app` creates the application and records the update in one transaction.

**Stale applications.** After 6 business days quiet with no follow-up → "Follow up". After **30** → "Close out?" with only the closes `transitions.py` allows (no response is allowed through the final round, not after an offer), or "Keep it open" (hidden 14 days).

**Notifications (`jt notify`, first step of the hourly job).** Each sent once, keyed in `sync_state`:
- Gmail sync failing **and** no good sync for 2+ hours (once a day, with the fix)
- tasks due today or overdue (once per task per due day)
- new applications found (once each)

The text goes to `osascript` as arguments, never pasted into the script.

## 6. Invariants (enforced in code and tests)

1. Events are append-only; `INSERT OR REPLACE` on events is forbidden (`recursive_triggers` set in `connect()`).
2. Status is derived, never stored.
3. One event per email, one action per email; reminders merge into the existing task.
4. Every write goes through `write_txn` and reads its deciding state inside the lock. No network calls inside it.
5. The classifier never creates applications and never writes `note` events.
6. Consequential kinds never auto-commit unless `CLASSIFIER_AUTO_COMMIT_TERMINAL=1`.
7. Evidence and deadline quotes must be verbatim (whitespace-tolerant) from the email's subject or body.
8. The Today JSON is versioned. Renaming or removing a field bumps `schema_version`; additions are allowed.
9. Every live-model failure becomes a case in `tests/fixtures/live_regressions.json` (anonymized).

## 7. Failure behavior

| Failure | Behavior | Where you see it |
|---|---|---|
| Gmail sign-in expired, offline, other | Sync logs it; no watermark move | **Notification after 2 h**; panel title ⚠ with words; `freshness.sync_error` |
| Gmail rate limit (403/429) | Retry with ≥ 20 s waits, 5 attempts | `jt-sync.jsonl` |
| Model down or malformed output | Nothing written; messages go to review or stay unprocessed | `jt-classify.jsonl`, review count |
| Concurrent writers | `BEGIN IMMEDIATE` serializes; the loser rechecks and goes to review | Race test |
| Stage changed between menu render and click | Approval refused, nothing written | Panel failure line (expires in 15 min) |
| Panel can't read jt | Cached snapshot with its age; write buttons hidden | Panel |
| Failed click | Real error shown; Retry only for safe repeatable clicks | Panel |
| Old-shaped candidates table | Repaired on connect, rows kept | stderr note |
| Mac asleep or off | **Nothing runs.** No sync, no alerts | Freshness ages in the panel |

## 8. Deployment

| Thing | Where |
|---|---|
| Dev checkout | `~/Documents/ChatGPT/building/jt-work` (other local copies are leftovers; see debt) |
| **Production** | `~/Developer/jt` + `.venv`, run by launchd: `dev.nicole.jt.sync` (hourly), `dev.nicole.jt.daily-stale` (8:30) |
| Data | `~/.jobtrack/jobs.db`; logs `~/.jobtrack/logs/`; panel cache and usage log `~/.jobtrack/` |
| Config | `~/.jobtrack/jt.env` (OAuth paths, `GMAIL_QUERY`, model, API key), mode 600 |
| Panel | `~/SwiftBar/jt.5m.py` → symlink into production |
| Backups | `~/jobtrack-backups/jobs-<timestamp>.db/.csv`; `jt verify-backup` |
| CI | GitHub Actions: pytest + mypy on Python 3.11 and 3.12, **required on `main`**, admins included, no force pushes |

**Deploy:** `scripts/deploy.sh` (or `--dry-run`). Check what's live with `jt version`, also shown in the panel footer.

## 9. Security and privacy

- Gmail scope `gmail.readonly`; OAuth client and token outside the repo. *(Debt: token in a file, not Keychain.)*
- Labeling page: 127.0.0.1 only, Host-checked, JSON same-origin saves.
- Never committed: labels, fixtures from real mail, databases. `scripts/secret-scan.sh` runs before commits.
- **No real job-search names in the public repo.** Main and all PR text were scrubbed (#47, #48); a fixture test guards the regression file. Older commits in git history still contain names (owner's decision).
- Backups warn on cloud folders; `--no-bodies` vacuums email text out.
- The panel title never shows company names; `JT_NOTIFY_PRIVATE=1` hides them in notifications.

## 10. Known debt (ranked)

| # | Debt | Impact | Fix |
|---|---|---|---|
| 1 | **Google OAuth in Testing mode** (likely) | Sign-in expires every 7 days; caused a 6-day outage | Owner: switch to "In production" (free) |
| 2 | **Classifier evidence is thin:** 23 labeled emails | Accuracy can't be claimed yet | Labeling (PRD Now) |
| 3 | **Mac sleep stops everything** | No sync or alerts while asleep | Keep plugged in; later, a wake-aware catch-up |
| 4 | Gmail sign-in path is the least-tested code (sync 74%) | The code that actually failed in production | Tests with a fake credentials object |
| 5 | `jt.py` is a ~2,400-line module | Harder to change safely | Split by area (schema, actions, review, notify, CLI) when next touched heavily |
| 6 | Old commits contain real names | Privacy, for someone digging through history | Owner: make private, or leave |
| 7 | 14 local repo copies; Codex demo app still running | Confusion | Owner approval to delete / stop |
| 8 | OAuth token stored as a file | Weaker than Keychain | Later (P2) |
