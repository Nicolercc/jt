# jt — job application tracker

[![ci](https://github.com/Nicolercc/jt/actions/workflows/ci.yml/badge.svg)](https://github.com/Nicolercc/jt/actions/workflows/ci.yml)

A local-first CLI for tracking job applications, built around an append-only
event log and a hard boundary between what an LLM may *propose* and what may be
*committed*.

About 2,800 lines of Python (excluding tests). No server, no web UI, no
hosted anything. The database is one SQLite file on your laptop.

---

## Why this exists

A spreadsheet answers *"what did I apply to."* That question is easy and not
very useful. The question that actually changes your day is *"who has gone
quiet, and what do I do about it right now."* That is a state machine with a
decay function, and it is worth building.

The second reason: application confirmation emails are a **lossy** signal.
Plenty of applications produce no confirmation at all. Many name the company
but not which of three open roles you hit. Timing is whenever the ATS feels like
it. So email is not the write path here — it is the *reconciliation* path.

```
you apply    ──►  application record  (intent; authoritative for what/when)
                          │
gmail sync   ──►  raw messages  ──►  proposed events
                          │                  │
                          └── validation ────┴──►  committed events
                                   │
                                   └──────────────►  review queue
```

---

## What it looks like

Real output from a demo database with four applications (one already rejected):

```console
$ jt stale

  #  COMPANY         ROLE                STATUS     QUIET  ACTION     CONTACT
  -  --------------  ------------------  ---------  -----  ---------  -------
  2  Northwind Labs  Product Engineer    confirmed  13d    FOLLOW UP  -
  4  Northstar       Operations Analyst  applied    11d    FOLLOW UP  -
  1  Globex          Backend Engineer    screen     10d    FOLLOW UP  -

$ jt log 3 screen
error: rejected is terminal; refusing to append 'screen' to a closed application
  pass --force to log it anyway (for a deliberate manual correction)
$ echo $?
1

$ jt show 1

  #1  Globex - Backend Engineer
  lane swe | applied 2026-10-20 | status screen
  quiet 10 business days | follow-ups sent 0
  action   FOLLOW UP

  WHEN        KIND       SOURCE  EVIDENCE
  ----------  ---------  ------  -------------------
  2026-10-21  confirmed  manual
  2026-10-30  screen     manual  30 min w/ recruiter
```

The middle command is the design in one line. Status is derived from the event log, and every new event, whether you typed it or a model proposed it, goes through the same legality check in [`transitions.py`](transitions.py). The classifier gets no `--force`.

## Install

```bash
git clone https://github.com/Nicolercc/jt.git && cd jt
pipx install -e .           # or: alias jt="python3 $PWD/jt.py"
jt where                    # confirms the database path
```

The tracker itself has zero runtime dependencies. Gmail sync and classification
are optional extras:

```bash
pipx install -e ".[gmail]"             # jt-sync
pipx install -e ".[gmail,classify]"    # jt-sync + jt-classify
```

From GitHub:

```bash
pipx install "jt-tracker @ git+https://github.com/Nicolercc/jt@v0.3.1"
pipx install "jt-tracker[gmail,classify] @ git+https://github.com/Nicolercc/jt@v0.3.1"
```

## How to use jt

This is the operator's manual: what runs by itself, what needs you, and what
to do when something looks wrong. The design reasoning comes later in this
file. Setup details live in [docs/automation.md](docs/automation.md).

### What runs by itself, and what needs you

| Every hour, automatically | Needs one decision from you |
|---|---|
| Pull new job mail from Gmail (read-only) | **Add** a newly found application and pick its lane |
| Classify each email: confirmation, screen, interview, rejection, deadline… | **Approve** an email the classifier wasn't sure about |
| Record events and tasks it's sure about, on applications you already have | **Mark done** or **snooze** a task |
| Notify you: new applications, tasks due, sync trouble | **Close out** an application that has gone quiet |

**The model never creates an application or skips a legality check.** That
is a deliberate rule (DESIGN D0.5), not a missing feature. An email can only
*propose* a new application, so a misread newsletter or a lookalike email
can't put a fake one in your pipeline. Your part is one click, in the menu bar.

### One-time setup

1. Install jt and its Gmail and classifier extras (see [Install](#install)).
2. Put the OAuth client, token path and API key in `~/.jobtrack/jt.env`
   ([docs/automation.md](docs/automation.md#runtime-paths)).
3. Sign in to Gmail once: `jt-sync --dry-run` opens Google in your browser.
4. Load the hourly and daily jobs: `scripts/install-launchd.sh --load`.
5. Install the menu-bar panel ([scripts/swiftbar/README.md](scripts/swiftbar/README.md)).
6. Allow notifications: System Settings → Notifications → Script Editor → Allow.
7. **Publish the Google OAuth app** (Google Cloud → Audience → Publish app).
   In Testing mode Google cancels the sign-in every 7 days and the hourly
   sync silently stops finding mail.

Check the setup: `jt version`, then `scripts/jt-automation.sh --verify`.

### Reading the menu bar

The title is a status line you can read without opening it:

| Title | Means |
|---|---|
| `jt` | nothing due, nothing waiting |
| `jt 2` | two tasks are due today or overdue |
| `jt · 3?` | three decisions wait: emails to approve plus applications to add |
| `jt ⚠` | Gmail sync is failing. Open the menu for the reason |

Open it and work top to bottom. Each section only appears when it has something:

1. **Due**: tasks from emails ("Send availability", "Complete assessment by
   Friday") and ones you added. **Done**, **Snooze until tomorrow 9 AM**,
   **Open email**, and **Why** (the quote it came from).
2. **Needs your decision**: emails the classifier held back. Each shows the
   sender and a plain-English summary, with one-click answers such as
   "✓ Record screen for Globex · Backend Engineer", "Add Northstar ·
   Operations Analyst as a new application", or "Dismiss: not needed".
3. **New applications found**: confirmation emails for applications jt
   hasn't seen. **Add as an application → Software / Ops / Comms**, or
   **Not mine**.
4. **Suggestions**: "Follow up" after 6 quiet business days, and "Close
   out?" once an application has been silent a long time. Close-out choices:
   **Mark as no response**, **I withdrew**, or **Keep it open** (hidden 14
   days).
5. **Footer**: when Gmail last synced and the classifier last ran. If either
   is more than about an hour old, something is wrong; see
   [When something looks wrong](#when-something-looks-wrong).

### The daily routine (about 5 minutes)

1. **Morning:** open the menu. Clear **Due** first, then **Needs your
   decision**, then **New applications found**. The goal is a plain `jt`
   title.
2. **When you apply somewhere:** do nothing. Most applicant systems email a
   confirmation. Within the hour it appears under **New applications found**
   and you pick the lane. If no confirmation comes by the next day, add it
   yourself:
   ```bash
   jt add "Northstar" "Operations Analyst" --lane ops --url https://jobs.example/123
   ```
3. **When a recruiter writes, you get an interview, or a rejection lands:**
   do nothing. The classifier records it, or asks you under **Needs your
   decision**. Log by hand only what never reached email, such as a phone
   call: `jt log 3 screen --note "30 min with recruiter"`.
4. **Evening (optional):** `jt stale` for a full-pipeline view of who needs a
   nudge.

### The weekly review (about 15 minutes)

```bash
jt priority            # top 10 open applications by next-action value
jt priority --explain 3
jt stale               # every open application, quiet days, and FOLLOW UP / CLOSE OUT
jt ignored             # non-job mail kept out of review; `jt ignored restore <#>` if one was a mistake
jt backup ~/jobtrack-backups && jt verify-backup ~/jobtrack-backups/<newest file>
```

Close out anything you've stopped caring about. A pipeline of 40 live-looking
applications where 30 are dead makes every "what's next" answer wrong.

### When something looks wrong

| You see | It means | Do this |
|---|---|---|
| `⚠` and "Gmail sign-in expired" | Google cancelled jt's Gmail access (every 7 days in Testing mode) | Follow [When the Gmail sign-in expires](docs/automation.md#when-the-gmail-sign-in-expires): run `sync.py` once in Terminal to sign in again, then kick the hourly job. Then publish the OAuth app so it stops recurring. |
| An application you sent never showed up | No confirmation email, the email didn't match the Gmail search (no `jobsearch` label, not in `GMAIL_QUERY`), or sync is down | Check the footer's "synced" time. Run `jt sync --audit --since <date>` to list job mail jt hasn't imported. If the email exists, widen `GMAIL_QUERY`; otherwise `jt add` it. |
| "Classified" in the footer is hours old | The classifier is failing (API key, quota, network) | `tail ~/.jobtrack/logs/jt-classify.jsonl`, then `jt review stale` |
| The same decision keeps coming back | A review item is still open | `jt review` lists each one with the exact command that resolves it |
| A daily 8:30 notification about review health | Something has waited for a decision for too long | Clear **Needs your decision** |
| The panel says "Showing data from …" | The panel couldn't read the tracker and is showing its last copy | `jt today` in Terminal shows the real error |
| `jt version` differs from GitHub `main` | Production hasn't been deployed since the last merge | `scripts/deploy.sh --dry-run`, then `scripts/deploy.sh` |

### Command reference

```bash
jt add "Globex" "Backend Engineer" --lane swe --contact recruiter@globex.example
jt bulk < today.txt              # 'Company | Role | lane [| url]' per line
jt list                          # open pipeline; terminal hidden
jt stale                         # who needs a nudge today  ← the money command
jt priority                      # top 10 applications by next-action value
jt priority --explain 3          # why one application scored that way
jt log 3 screen --on 2026-08-19 --note "30min w/ recruiter"
jt show 3                        # full audit trail for one application
jt action add 3 "Send availability" --due tomorrow
jt action add 3 "Interview" --due 2026-08-21 --at 14:00 --tz America/New_York
jt action list                   # pending actions; done/snooze/dismiss by ACT id
jt action snooze 5 --until "2026-08-22 09:00"
jt today                         # due actions first, then follow-up suggestions
jt today --json                  # versioned read model for a menu-bar panel (DESIGN D5)
jt review                        # what needs your decision, in plain English, with exact commands
jt review approve 7 --app-id 3   # record an update; add-app 7 --lane ops adds a new application
jt ignored                       # non-job mail kept out of Review; `jt ignored restore <#>` undoes
jt candidates                    # applications found in your email; accept <id> --lane ops / reject <id>
jt notify --dry-run              # alerts the hourly job would send (sync trouble, due tasks, new apps)
jt version                       # which commit and schema this jt is running
jt rm 4                          # only works while it has zero events
jt export --lane swe --out pursuit.csv
jt backup ~/jobtrack-backups      # timestamped; add --no-bodies for cloud folders
jt verify-backup ~/jobtrack-backups/jobs-2026-08-21T200000.db
```

Lanes are `swe` (software roles), `ops` (operations, benefits, admin) and
`comms` (writing, social, communications). `--on` takes `YYYY-MM-DD`,
`today` or `yesterday`; `--due` takes `YYYY-MM-DD`, `today` or `tomorrow`.

**Deploying to the checkout launchd runs** is one command, which backs up and
verifies the database before pulling and checks the hourly job afterwards:

```bash
scripts/deploy.sh --dry-run   # what would change
scripts/deploy.sh
```

Notifications come from `jt notify`, which the hourly job runs first: once per
event for Gmail sync trouble (after 2 hours), tasks due today or overdue, and new
applications. `JT_NOTIFY=0` turns them off; `JT_NOTIFY_PRIVATE=1` hides company
names on the lock screen.

**Put the backup in cron on day one.** The likeliest catastrophic failure here
is not a race condition, it is `rm -rf ~/.jobtrack`:

```cron
0 20 * * * /usr/local/bin/jt backup ~/jobtrack-backups
```

---

## Try it with synthetic data

From the repository root, run the five-minute walkthrough with Python 3.11+:

```bash
bash scripts/demo.sh
python3 -m evals.run evals/fixtures.example.jsonl --use-expected
```

The walkthrough captures an application, routes a synthetic recruiter email
to review because two roles match, records your approval, and surfaces a
deadline. It uses a disposable database and requires no credentials.

Inspect a held email with `jt review show <id>`. Approve it with
`jt review approve <id> --app-id <application-id>` or record a dismissal
with `jt review dismiss <id> --note "reason"`. Approval rechecks the current
application stage and writes the event and decision together. A company
mismatch requires `--override-company` and an explanation in `--note`.

The draft command prints a local template for editing and sending yourself.
Classifier evals report actual writes to their disposable database.

## Gmail Sync

Create a Gmail label named `jobsearch`, place OAuth credentials outside the
repo, then run:

```bash
export GOOGLE_OAUTH_CLIENT="$HOME/.jobtrack/google_oauth_client.json"
export GOOGLE_TOKEN_PATH="$HOME/.jobtrack/token.json"
jt-sync --dry-run
jt-sync
```

`jt-sync` opens the database through `jt.connect()`, uses the read-only Gmail
scope, paginates, retries 429/5xx and 403 rate-limit responses, overlaps the watermark by two
days, and inserts with `INSERT OR IGNORE`. It does not classify anything.

The first `jt-sync` opens Google in your browser and saves the token. If a
saved token later stops working (`invalid_grant`), rerunning `jt-sync` won't
replace it. Follow [When the Gmail sign-in expires](docs/automation.md#when-the-gmail-sign-in-expires).

The default searches the configured label plus assessment and scheduling
platforms and recruiting subject phrases. Set `GMAIL_QUERY` to replace the
entire default search. The sync date bound is still added:

```bash
export GMAIL_QUERY='from:(greenhouse.io OR lever.co OR ashbyhq.com) OR subject:("thank you for applying")'
```

Run `jt sync --audit --since YYYY-MM-DD` with existing Gmail credentials
to compare the broad default search against your local tracker, even if you
have a custom query. It prints date, sender domain, and subject for missing
messages. Treat that output as private. The audit opens the database read-only
and does not import messages, update freshness, or advance the watermark.


A search can match mail a filter would not, so review the dry run first.

## Classification

Classification sends the selected email subject, sender, date, message ID,
and up to 2,500 characters of body text to Anthropic. Review the Gmail label
scope before using it with real mail.

After raw mail lands, classify one batch:

```bash
export ANTHROPIC_API_KEY="..."
export ANTHROPIC_WORKSPACE_ID="..."   # only if the key isn't scoped to a workspace
jt-classify --dry-run
jt-classify
jt review
```

The classifier output is JSON, but it is not trusted. Python validates field
types, evidence spans, company/role matching, confidence, dates, and legal
state transitions before inserting an event. The transition check runs twice:
once per proposal, and again against the live state immediately before each
insert, so two proposals in one batch can't combine into something illegal
(for example "rejected" followed by "screen"). Anything ambiguous lands in
`review_queue` and the raw message is marked processed so it cannot loop
forever.

Only one `jt-classify` process can run against a database at a time. The lock is
advisory and OS-released, so a crashed classifier will not leave a permanent
deadlock.

---

## The five invariants

These are enforced by SQLite, not by convention. An agent working in this repo
must not remove them.

**1. `events` is append-only.** Triggers abort every `UPDATE` and `DELETE`.
Fixing a mistake means appending a corrective event; the mistake stays visible.

**2. `applications` has no status column.** Status is derived from the event log
on every read. There is no status field for a classifier to write to, so a bad
classification can only ever propose.

**3. `events.gmail_msg_id` is `UNIQUE`.** The database owns idempotency — not
the Python, and definitely not the model. This is what lets Gmail sync be
deliberately sloppy about its cursor: overlap the window, let the constraint eat
the duplicates.

**4. `events.kind` is a foreign key into `event_kinds`.** A `CHECK` gives the
same guarantee but cannot be altered in SQLite without a table rebuild, which is
painful once append-only triggers exist. A lookup table makes adding a kind an
`INSERT`.

**5. `events.application_id` is `ON DELETE RESTRICT`.** An application is
deletable only while it has zero events. Past the first event it is history.

### One sharp edge, found by the test suite

`INSERT OR REPLACE` is internally a `DELETE` plus an `INSERT`. SQLite fires the
delete trigger for it **only** when `PRAGMA recursive_triggers = ON`, which is
**off by default**. On a default connection, `OR REPLACE` silently rewrites
history straight through invariant 1 — verified, not theorized:

```
WITHOUT pragma -> history silently rewritten (this is the hole)
```

`connect()` sets the pragma. The pragma is per-connection, so anything opening
this database *without* going through `connect()` reopens the hole. Always use
`INSERT OR IGNORE` on `events`.

---

## Security posture

- Read-only Gmail scope. Not politeness — it means a bug in agent-written sync
  cannot delete your inbox.
- OAuth client JSON and tokens live outside the repo. `.gitignore` was written
  before the first commit, deliberately.
- `*.db` is gitignored. The code is public; the data is not.
- Eval fixtures are **redacted before they touch git**. Thirty real emails from
  a live job search are a legible map of where you are applying and who has
  rejected you. Only `evals/fixtures.example.jsonl` is tracked.
- No credentials in GitHub Actions. CI runs unit tests against fakes; the Gmail
  and Anthropic paths are exercised locally.

---

## Measuring the classifier

Hygiene makes a project defensible. Measurement makes it interesting. Label
real emails locally ([docs/EVAL_LABELING.md](docs/EVAL_LABELING.md)), build
fixtures, and freeze a stratified dev/holdout split:

```bash
python3 -m evals.dataset export      # append unlabeled mail to the label CSV
python3 evals/label_server.py        # label at http://127.0.0.1:8765
python3 -m evals.dataset build       # labeled rows -> evals/fixtures.jsonl
python3 -m evals.dataset split       # new fixtures -> dev or holdout; old ones never move
jt-eval --set dev                    # tune against this
jt-eval --set holdout                # report this; never tune against it
```

The checked-in redacted example can exercise the harness without credentials:

```bash
jt-eval evals/fixtures.example.jsonl --use-expected
```

It reports precision and recall with 95% Wilson intervals, a per-kind table,
wrong auto-commits per 100 emails, wrong terminal-state commits (target: zero),
and the share of misclassifications that landed in the review queue rather than
the database. That last number is the entire
thesis of the design. Results on real mail aren't published here, because the
labelled fixtures are a private job search.

The failure mode worth watching is **silence, not hallucination**: an email that
should have produced a rejection, landed in review, and sat there for three
weeks while you believed you were still in play. `jt stale` prints the pending
review count at the top for exactly this reason. A growing queue means the
system is quietly rotting.

---

## Tests

```bash
pip install -e ".[dev]" && python -m mypy && python -m pytest -q
```

125 tests, run in CI on Python 3.11 and 3.12 alongside mypy. They cover the
append-only triggers, replaying the event log into an empty database, every
classifier rejection path (including batch-level legality), sync idempotency
and the eval harness. Every time-dependent assertion pins the clock: a suite
whose results change at midnight is a suite you learn to ignore.

## Live Acceptance

These require local credentials and are intentionally not run in CI:

```bash
jt-sync --dry-run
jt-sync
jt-sync        # second run should insert zero rows

jt-classify --dry-run
jt-classify
jt review

jt-eval evals/fixtures.jsonl
```

Before running them: rotate any previously committed Google OAuth client secret,
enable Gmail API read-only scope, create the Gmail `jobsearch` label/filter, set
`ANTHROPIC_API_KEY`, and create a redacted `evals/fixtures.jsonl` from real mail.

Suggested local schedule after acceptance:

```cron
0 8 * * 1-5 /usr/local/bin/jt-sync && /usr/local/bin/jt-classify
0 20 * * * /usr/local/bin/jt backup ~/jobtrack-backups
```

---

## Scope

Not built, on purpose: Postgres, web UI, Rust, Notion sync, browser extension,
calendar integration, hosted deployment. There is nothing compute-bound here and
nothing needing a deterministic verification boundary beneath an agent.

**Size rule (since 2026-11-21): every PR justifies its lines.** Each PR states
what it adds and what it cut, or why nothing could be cut. New features live in
their own modules; `jt.py` may only shrink.

This replaced a whole-project line cap. Drafts set the cap at 600, then 1,200,
then 1,600 lines; the project passed each one while building the specified
core, and is now about 4,200 lines of Python. A budget you breach every time you
build the core isn't a budget, so the discipline moved to where decisions
actually get made: the PR.
