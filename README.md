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

  #  COMPANY  ROLE                STATUS     QUIET  ACTION     CONTACT
  -  -------  ------------------  ---------  -----  ---------  -------
  2  Linear   Product Engineer    confirmed  10d    FOLLOW UP  -
  1  Stripe   Backend Engineer    screen     8d     FOLLOW UP  -
  4  Ramp     Operations Analyst  applied    7d     FOLLOW UP  -

$ jt log 3 screen
error: rejected is terminal; refusing to append 'screen' to a closed application
  pass --force to log it anyway (for a deliberate manual correction)
$ echo $?
1

$ jt show 1

  #1  Stripe - Backend Engineer
  lane swe | applied 2026-10-20 | status screen
  quiet 8 business days | follow-ups sent 0
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

## Use

```bash
jt add "Stripe" "Backend Engineer" --lane swe --contact recruiter@stripe.com
jt bulk < today.txt              # 'Company | Role | lane [| url]' per line
jt list                          # open pipeline; terminal hidden
jt stale                         # who needs a nudge today  ← the money command
jt priority                      # top 10 applications by next-action value
jt priority --explain 3          # why one application scored that way
jt log 3 screen --on 2026-08-19 --note "30min w/ recruiter"
jt show 3                        # full audit trail for one application
jt rm 4                          # only works while it has zero events
jt export --lane swe --out pursuit.csv
jt backup ~/Dropbox/jobtrack
```

Daily rhythm, ninety seconds: `jt stale` with coffee, `jt bulk` before bed.

**Put the backup in cron on day one.** The likeliest catastrophic failure here
is not a race condition, it is `rm -rf ~/.jobtrack`:

```cron
0 20 * * * /usr/local/bin/jt backup ~/Dropbox/jobtrack
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
scope, paginates, retries 429/5xx responses, overlaps the watermark by two
days, and inserts with `INSERT OR IGNORE`. It does not classify anything.

No label filter? Set `GMAIL_QUERY` to a Gmail search instead, and it replaces
the label scope (the date bound is still added):

```bash
export GMAIL_QUERY='from:(greenhouse.io OR lever.co OR ashbyhq.com) OR subject:("thank you for applying")'
```

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
thirty emails once, then run:

```bash
jt-eval evals/fixtures.jsonl
```

The checked-in redacted example can exercise the harness without credentials:

```bash
jt-eval evals/fixtures.example.jsonl --use-expected
```

It reports precision, recall, and the share of misclassifications that landed
in the review queue rather than the database. That last number is the entire
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
0 20 * * * /usr/local/bin/jt backup ~/Dropbox/jobtrack
```

---

## Scope

Not built, on purpose: Postgres, web UI, Rust, Notion sync, browser extension,
calendar integration, hosted deployment. There is nothing compute-bound here and
nothing needing a deterministic verification boundary beneath an agent.

**Hard stop rule:** 1,600 lines of Python excluding tests, across the whole
project. More features should replace code, not accrete around it. The project
is currently about 2,800 lines, so it is over budget: the next change should
cut code, not add it.

Earlier drafts set the limit at 600, then 1,200. Both were useful warnings and
bad budgets: Phase 1 alone breached the first, and the real Gmail/classifier
implementation breached the second. A budget you breach while building the
specified core is not a budget.
