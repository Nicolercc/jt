# jt design — revision 3

> **2026-11-13:** The current plan lives in [PRD.md](PRD.md) and the as-built system in [ARCHITECTURE.md](ARCHITECTURE.md). This file remains the **decision log** (D1–D7 and the dated log in §10). Milestone statuses here are historical; PRD §9 is the live status.

**Status:** All one-way-door decisions (D1–D7) decided 2026-11-12. Next: M1.
**Owner:** Nicole Rodriguez
**Last updated:** 2026-11-12
**Builds on:** [BUILD_PLAN.md](BUILD_PLAN.md) (revision 2). Read that first. Nothing here overrides it unless it says so.

---

## 1. Problem

During a job search, important updates are spread across Gmail, job boards, and memory. The expensive failures are quiet ones: an interview availability request that sits for three days, a deadline nobody wrote down, a follow-up sent to a company that already rejected you.

`jt` already records applications and derives status from an append-only event log. Revision 3 is about one question:

> **What should I do right now, and why should I believe it?**

### Success, measured

| Measure | Target | How we check |
|---|---|---|
| Wrong terminal-state commits (rejected, offer, accepted, withdrawn, ghosted) | **0** on the holdout set | `evals/run.py --set holdout` |
| Wrong auto-commits of any kind | ≤ 2 per 100 emails | same |
| Eval sample size | ≥ 150 labeled emails, 20% frozen holdout | `evals/fixtures.jsonl` row count |
| Daily use during the test week | Opened on ≥ 5 of 7 days, ≥ 1 action completed from it on ≥ 4 days | `~/.jobtrack/usage.log` |
| Time to answer "what now?" | Under 5 seconds from hotkey to reading the top row | Timed by hand, 5 tries |
| Gmail fallback | Count of times I still searched Gmail to find out what to do | Tally during the test week |

If the test week fails the daily-use measure, the native panel is not built (see M6).

---

## 2. Goals and non-goals

**Goals**
1. Represent more than one action per application, with real due times.
2. Never let the classifier close an application on its own until the eval shows it can.
3. Publish a stable, versioned read model (`jt today --json`) that any frontend can depend on.
4. Make every write path safe against a second writer (UI approval vs. classification).
5. Grow the eval to a size that supports the numbers quoted in the README.

**Non-goals** (each one is a decision, not an oversight)
- No phone or tablet app. No cross-device sync.
- No multi-user or multi-inbox support.
- No hosted worker. Gmail is read from this Mac only.
- No change to the append-only event log or derived status.
- No switch to Gmail history IDs (see D0.4).
- No native Mac app until M5 passes.

---

## 3. Decisions already made (do not reopen without new evidence)

| ID | Decision | Where |
|---|---|---|
| D0.1 | **The model proposes, deterministic code commits.** | BUILD_PLAN rule; `classify.py` |
| D0.2 | Events are append-only; status is derived, never stored. Corrections are new events, and the latest terminal event by id wins. | `jt.py` docstring, `derive()` |
| D0.3 | `transitions.py` is owned by a person. Agents never add transitions. | `transitions.py` docstring |
| D0.4 | Gmail sync uses a date watermark minus a 2-day overlap; UNIQUE constraints absorb duplicates. History IDs were considered and declined. | BUILD_PLAN, "On the watermark" |
| D0.5 | The classifier never creates applications. Unmatched mail goes to review or candidates. | BUILD_PLAN Phase 3 |
| D0.6 | Raw mail and OAuth tokens stay on this machine. Fixtures are redacted before anything touches git. | BUILD_PLAN Phase 2, 3.5 |

**What would reopen D0.4:** a logged case of a message that was labeled more than 2 days after it arrived and never synced. Until then, add `jt sync --since` as a manual backfill and print the scanned range on every run.

---

## 4. One-way doors

These are expensive to change once real data or a frontend depends on them. Each lists the options, the proposal, and what would change the decision.

### D1. Action model — **Decided 2026-11-12: B**

**Today:** `next_actions` uses `application_id` as its primary key, so each application holds one action. `due_on` is a date. Actions have no done or snoozed state; `--clear` deletes the row.

**Known bug this causes:** in `cmd_today`, an explicit action due in the future does not suppress the generic `FOLLOW UP` suggestion. You are told to chase a recruiter before the date you chose.

| Option | Pros | Cons |
|---|---|---|
| A. Keep one action per application; add `state` and `snoozed_until` | Smallest change | Can't hold "send availability" and "follow up Friday" together |
| **B. New `actions` table, many per application** | Matches reality; gives the UI stable IDs for Done/Snooze | Needs a migration from `next_actions` |
| C. Model actions as events in the append-only log | One source of truth | Actions are mutable to-dos; forcing them into an append-only log makes Done/Snooze awkward |

**Proposal: B.**

```sql
CREATE TABLE actions (
    id              INTEGER PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE RESTRICT,
    description     TEXT NOT NULL,
    due_date        DATE,             -- all-day actions
    due_at_utc      TEXT,             -- timed actions, ISO-8601 UTC
    due_tz          TEXT,             -- IANA name, e.g. America/New_York
    state           TEXT NOT NULL DEFAULT 'open'
                    CHECK (state IN ('open','done','snoozed','dismissed')),
    snoozed_until   TEXT,
    reason          TEXT,             -- why this exists, shown in the UI
    source_event_id INTEGER REFERENCES events(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at       TEXT,
    CHECK (due_date IS NULL OR due_at_utc IS NULL),
    CHECK ((due_at_utc IS NULL) = (due_tz IS NULL))
);
```

Rules:
- Migration copies each `next_actions` row to an open, all-day action, then drops `next_actions`. Runs once, inside `BEGIN IMMEDIATE`, like `_migrate_v1_to_v2`.
- Any open or snoozed action on an application suppresses the generic `FOLLOW UP` / `CLOSE OUT` suggestion.
- Done and dismissed are different: done means it happened, dismissed means it no longer applies. Snooze always has a return time.
- A terminal status hides open actions for that application in `today` but does not delete them.

**Would change it:** finding that I never have more than one action per job during the test week. That would argue for A.

### D2. Time precision — **Decided 2026-11-12: times on actions only**

**Today:** `events.occurred_on` and `next_actions.due_on` are dates only.

**Proposal:** keep events date-only, and add times only to actions (D1).

Why: an event records *what happened*, and the date is enough evidence. An action records *what to do*, and "interview at 2pm ET" must survive daylight-saving changes and a laptop in another timezone. Store UTC plus the IANA zone it was entered in, and display in local time.

**Required tests:** an action created at 2pm America/New_York the week before the November DST change still displays 2pm after it; the same action viewed with `TZ=America/Los_Angeles` displays 11am.

### D3. Terminal-state auto-commit — **Decided 2026-11-12: A now, B after M3**

**Today:** if the classifier proposes `rejected` (or another terminal kind), and it is legal, confident (≥ 0.85), matched to one application, and has verbatim evidence, it commits. Terminal events end the application, so `today` stops showing it.

| Option | Behavior |
|---|---|
| **A. Always review** | Classifier terminal proposals go to `review_queue` with reason `terminal state requires confirmation`. |
| B. Review unless a template parser matched | Deterministic Greenhouse/Lever/Ashby/Workday rejections may auto-commit; model-only ones go to review. |
| C. Confidence threshold | Auto-commit above a higher bar, e.g. 0.95. |

**Proposal: A now, B after M3 passes.** C is out: BUILD_PLAN already explains that a confidently wrong model is not caught by a threshold.

Cost: about one extra review per rejection email. That is the review workload metric to watch.

**Would change it:** holdout shows 0 wrong terminal commits from template-matched mail over ≥ 30 terminal emails. Then move to B for those templates only.

### D4. One email, one event — **Decided 2026-11-12: keep the rule**

**Today:** `events.gmail_msg_id` is UNIQUE, so one email can produce at most one event.

**Decision:** keep it. An email about two applications already fails matching and lands in review. When you resolve it, the first event keeps the `gmail_msg_id`; any second event is written with `gmail_msg_id = NULL` and its evidence notes the source message ID.

**Would change it:** more than 3 such emails in the test week. Then add an `event_sources` join table instead of loosening the UNIQUE constraint.

### D5. `jt today --json` contract — **Decided 2026-11-12: two lists, badge counts due actions only**

This is the only thing a frontend may read. Frontends never open the database.

```json
{
  "schema_version": 1,
  "generated_at": "2026-11-12T14:03:00Z",
  "badge": {"due": 1, "reviews": 2},
  "freshness": {
    "gmail_synced_at": "2026-11-12T13:00:00Z",
    "classified_at": "2026-11-12T13:01:12Z",
    "sync_error": null
  },
  "actions": [
    {
      "action_id": 41,
      "application_id": 7,
      "company": "Example Co",
      "role": "Software Engineer",
      "description": "Send interview availability",
      "due": {"kind": "date", "value": "2026-11-12"},
      "overdue": false,
      "origin": "manual",
      "reason": "Recruiter asked for times on 2026-11-11",
      "gmail_msg_id": "18c...",
      "gmail_url": "https://mail.google.com/mail/u/0/#all/18c..."
    }
  ],
  "suggestions": [
    {"application_id": 9, "company": "Other Co", "role": "Ops Coordinator",
     "kind": "FOLLOW UP", "reason": "11 business days quiet, no follow-up sent"}
  ],
  "review": {"pending": 2, "oldest_days": 1,
             "top": {"review_id": 15, "reason": "two applications matched"}}
}
```

Rules:
- **Two lists, never merged.** `actions` holds things you committed to (`origin` is `manual`, `review`, or `migrated`). `suggestions` holds jt's computed guesses (FOLLOW UP, CLOSE OUT). The panel shows actions first and labels suggestions as suggestions.
- **The menu-bar number is `badge.due`:** open actions due today or overdue. Snoozed, done, and dismissed actions never count. Suggestions never count. Pending reviews are a separate small badge, `badge.reviews`, so the main number always means "things to do."
- An application with any open or snoozed action produces no suggestion (D1 suppression rule).
- Adding fields is fine in v1. Renaming or removing a field, or changing a type, bumps `schema_version`.
- `sync_error` is one of `null`, `"offline"`, `"auth_expired"`, `"failed"`. The UI shows it in words, not only color.
- Write paths for the UI go through commands, not the JSON: `jt action done <id>`, `jt action snooze <id> --until ...`, `jt review approve|reject <id>`. Each must accept `--json` and exit non-zero on failure.
- Contract tests use fixed fixtures for six states: healthy, empty, offline, auth expired, ambiguous match, overdue.

### D6. Write transactions — **Decided 2026-11-12: every write path**

**Today:** review resolution, candidate acceptance, and migrations use `BEGIN IMMEDIATE`. `classify.apply_verdicts` does not: its legality recheck reads before the write lock is taken. `classifier_lock` serializes classifier runs against each other, but not against a review approval.

**Decision:** one helper, `jt.write_txn(conn)`, a context manager that runs `BEGIN IMMEDIATE` … `COMMIT`, with rollback on error. Every write path uses it: `classify.apply_verdicts`, review resolution, candidate accept/reject, `add`/`bulk`/`log`/`rm`, action create/done/snooze/dismiss, migrations, and `sync.insert_rows` plus the watermark update. The existing hand-rolled `BEGIN IMMEDIATE` blocks are replaced by the helper so there is one pattern to audit. Model calls happen before the transaction opens; the recheck and insert happen inside it.

**Required test:** two connections. Connection 1 approves a review item to `rejected`; connection 2 runs `apply_verdicts` with a `screen` proposal for the same application. Exactly one wins, and the other goes to review with a recheck reason.

### D7. Backups and privacy — **Decided 2026-11-12: include bodies, warn on cloud**

**Today:** `jt backup` writes `jobs-YYYY-MM-DD.db` and `.csv`. A second backup the same day overwrites the first. The database includes full raw email bodies. The usage example in the `jt.py` docstring points at `~/Dropbox`.

**Proposal:**
- Filenames include time: `jobs-2026-11-12T1403.db`. Write to `*.tmp`, then rename.
- Backups include `raw_messages` by default, because evidence is what makes review possible after a restore.
- Warn (don't refuse) when the destination is inside a cloud-synced folder (Dropbox, iCloud Drive, Google Drive): `backup contains raw email bodies and will sync to the cloud`.
- Add `--no-bodies`, which copies everything but replaces `raw_messages.body` with NULL.
- Change the docstring example to a local path.
- Add `jt backup verify <file>`: opens the copy read-only, runs `PRAGMA integrity_check`, and prints row counts.

---

## 5. Two-way doors (decide fast, change freely)

- Panel layout, width, hotkey, colors.
- Whether the test-week panel is SwiftBar, xbar, or Raycast. **Default: SwiftBar.**
- Native stack (SwiftUI + AppKit vs. Tauri). Decided in M6, and cheap to change because of D5.
- Prompt wording, batch size, and the model name.
- Priority weights in `WEIGHTS`.

---

## 6. Riskiest assumptions

| # | Assumption | Uncertainty | Cheapest test | Milestone |
|---|---|---|---|---|
| R1 | I will check a menu-bar panel every day | High | SwiftBar plugin over `jt today --json`, 1 real week | M5 |
| R2 | The classifier is trustworthy enough that I stop re-checking Gmail | High | 150+ labeled emails, frozen holdout | M1, M3 |
| R3 | The schema can represent my real situations | Medium | Write 10 real situations from the current search and store each one by hand | M2 (first hour) |
| R4 | Overlap sync doesn't miss late-labeled mail | Low | Log scanned ranges; count manual Gmail finds during test week | M4, M5 |

---

## 7. Pre-mortem

It's February and this failed. Why?

| Failure | Prevention |
|---|---|
| I spent four weeks on SwiftUI and stopped applying to jobs. | No native work before M6. M5 has a hard 3-day build limit. |
| It marked a live application rejected and I missed a recruiter follow-up. | D3: terminal states go to review. |
| The panel showed yesterday's data while the Mac slept, and I trusted it. | D5 `freshness` block, shown in the panel. |
| An interview time shifted by an hour after daylight saving. | D2 DST tests. |
| A UI approval and a classifier run corrupted an application's history. | D6 helper and race test. |
| I couldn't defend the eval numbers in an interview. | Frozen holdout, sample sizes and intervals printed with every metric. |
| A backup of my raw email ended up in Dropbox. | D7 warning, `--no-bodies`, local-path docstring. |
| An agent edited `transitions.py` or bypassed `connect()`. | AGENTS.md invariants; review rejects such diffs. |

---

## 8. Milestones

Each one ends with a check that passes or fails. Don't start the next one until it passes.

| # | Milestone | Owner | Exit check |
|---|---|---|---|
| **M1** | **Eval growth.** Label to ≥ 150 emails with `evals/label_server.py`; weight toward hard cases. Add a seeded, stratified 80/20 dev/holdout split that refuses to re-split. Report per-kind precision/recall, wrong auto-commits per 100, wrong terminal commits, review rate, and Wilson 95% intervals. | **Nicole** labels; Codex builds the split and report | `evals/run.py --set holdout` prints all metrics with n. Baseline recorded in this doc. |
| **M2** | **Correctness core.** D1 actions table + migration; D2 timed actions; D3 option A; D6 `write_txn` + race test; `today` suppression fix. | Codex, one PR per decision | All tests pass, plus new ones: migration preserves rows, suppression, DST, race, terminal → review. |
| **M3** | **Template parsers.** Deterministic parsing for the ATS senders that appear in M1 data. Parsers run before the model; a match still goes through `validate_proposal` and `transitions`. | Codex | Template-matched rows score 100% on dev and holdout; eval prints template coverage %. |
| **M4** | **Read model and write commands.** D5 JSON contract, `jt action done/snooze`, `jt review approve/reject --json`, freshness tracking, D7 backup changes. | Codex | Contract fixtures pass for all six states; `jt backup verify` passes on a fresh backup. |
| **M5** | **Test week.** SwiftBar plugin: menu-bar count, top 3 actions, Open Email, Done, review count, freshness. Logs every open and click to `~/.jobtrack/usage.log`. Build limit: 3 days. Then use it for 7 days during the real search. | Codex builds; **Nicole** uses | Usage log and Gmail-fallback tally recorded below. |
| **M6** | **Go / no-go.** Native panel, or spend the time on the classifier instead. | **Nicole** | Decision and evidence written in §10. |
| M7 | Demo and write-up: an ambiguous email → refusal → evidence → decision → action appears. Eval table with n and intervals. | Both | README and a short recording. |

---

## 9. Ownership

| Nicole owns | Codex owns |
|---|---|
| Labels and the holdout set | Implementation against the exit checks |
| `transitions.py` | Tests, including the failing-first test for each fix |
| Decisions D1–D7 and M6 | Migrations, under `BEGIN IMMEDIATE` |
| Confidence thresholds and `WEIGHTS` | SwiftBar plugin |
| What gets committed and when | Reports listing what it did *not* do |

Agent rules live in `AGENTS.md` (kept untracked unless I decide otherwise): open the DB only via `jt.connect()`, never `INSERT OR REPLACE` into `events`, never edit `transitions.py`, never tune against `evals/holdout/`, no new network destinations, one PR per milestone item.

---

## 10. Log

| Date | Entry |
|---|---|
| 2026-11-12 | Revision 3 drafted. Nicole decided D1 (many actions per job), D2 (times on actions only), D3 (terminal states always reviewed), D7 (backups keep bodies, warn on cloud folders). Then D4 (keep one event per email), D5 (two lists; menu-bar number = due actions only), D6 (`write_txn` on every write path). |
| 2026-11-12 | M1 tooling built: `evals/dataset.py` (export, build, split), labeling page has an explicit "not a status change" choice, eval prints per-kind table, wrong commits per 100, wrong terminal commits, review rate, and 95% intervals. Split frozen on the existing 23 fixtures (18 dev / 5 holdout). Labeling steps: [EVAL_LABELING.md](EVAL_LABELING.md). |
| 2026-11-12 | Labeling pool synced (140 emails; 117 waiting for labels). Gmail 403 rate-limit retry fixed (#5). |
| 2026-11-12 | M2/M4 work that doesn't depend on labels opened as stacked PRs: D6 `write_txn` on every write path, with a race test that fails on the old code (#6); D1+D2 actions table, timed actions, FOLLOW UP suppression (#7); D7 timestamped, atomic, verifiable backups (`jt verify-backup`, not `jt backup verify`, because `backup` takes a positional folder) (#8); D5 `jt today --json` v1 plus JSON results for action and review commands, and freshness timestamps from sync and classify (#9). D3 waits for the M1 baseline so its before/after effect can be measured. `jt today --json` runs in ~50 ms on real data, so a panel can call it directly; no background process needed for now. |
| 2026-11-13 | Day-after audit. Production (`~/Developer/jt`, the launchd checkout) was 6 commits behind: deployed after a verified backup; live DB migrated to v6; freshness now recorded. Fixed: labeling page served any Host and accepted cross-site saves (#13); `$TZ` values like `:/etc/localtime` crashed every date command (#14). |
| 2026-11-13 | D3 shipped early with a switch, as the review of the plan suggested: consequential kinds (terminal + offer) go to review by default; `CLASSIFIER_AUTO_COMMIT_TERMINAL=1` restores auto-commit. `evals.run --policy compare` scores both policies on ONE model response, so the before/after survives shipping D3 first. |
| | M1 baseline (≥ 150 labeled): _pending_ — run `python3 -m evals.run --set holdout --policy compare` |
| | M5 test week results: _pending_ |
| | M6 decision: _pending_ |

## 11. Open questions

1. Which ATS senders actually appear in my mail? (Answered by M1 data; determines M3 scope.)
2. Does `ghosted` ever come from the classifier, or only from me? If only from me, remove it from classifier output.
3. ~~Should snoozed actions count toward the menu-bar number?~~ Answered by D5: no.
