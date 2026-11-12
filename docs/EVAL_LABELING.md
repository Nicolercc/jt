# Labeling guide (M1)

**Goal:** at least 150 labeled emails, so the eval numbers mean something. Today there are 23.

Why it matters: with 5 holdout emails, "100% precision" has a 95% range of 51–100%. With about 30 holdout emails and no errors, the range tightens to roughly 89–100%. That's a number you can defend in an interview.

Everything below stays on your Mac. Nothing is sent anywhere except the Gmail read in step 1.

---

## 1. Pull more mail into a separate pool database (5 min)

Use a **separate database** (`evals/raw/pool.db`), not your real tracker. The hourly automation classifies whatever is in `~/.jobtrack/jobs.db`, so syncing old mail there would write events into your real search.

Run from the repo folder:

```bash
set -a; source ~/.jobtrack/jt.env; set +a
JT_DB=evals/raw/pool.db GMAIL_QUERY='subject:(application OR applying OR applied OR interview OR "next steps" OR candidacy OR position OR opportunity) OR from:(greenhouse.io OR lever.co OR ashbyhq.com OR myworkday.com OR smartrecruiters.com OR icims.com OR workable.com)' python3 sync.py --since 2026-05-01
```

The broad query is on purpose. You want job alerts, newsletters, and scams in the set too, because they're the mail that should *not* change anything.

If that finds fewer than about 130 messages, move `--since` earlier.

## 2. Add the new messages to the label file (1 min)

```bash
python3 -m evals.dataset export --db ~/.jobtrack/jobs.db --db evals/raw/pool.db
```

This only appends messages that aren't already in `evals/raw/label-me.csv`. Your existing labels are untouched, and it's safe to run again.

## 3. Label in the browser (2–3 hours, in batches)

```bash
python3 evals/label_server.py
```

Open http://127.0.0.1:8765. Press **Save** often.

### Choosing a kind

| Pick | When |
|---|---|
| **not labeled yet** | You haven't decided. These rows are skipped. |
| **not a status change** | Job alerts, newsletters, talent-community mail, "jobs you may like", scams, your own sent replies with no new information, and mail about jobs you never applied to. |
| **confirmed** | "We received your application." |
| **response** | A real person replied, but nothing is scheduled yet. |
| **screen** | Recruiter or phone-screen invite. |
| **tech** | Technical interview or take-home assignment. |
| **onsite** | Onsite or panel loop. |
| **final** | Final round. |
| **offer** | Offer. |
| **rejected** | Any "we've decided to move forward with other candidates." |

### Rules that keep the labels consistent

1. **Label what the email says, not what you know happened later.** If a recruiter emails "let's set up a call", that's `screen`, even if the call never happened.
2. **Spell company and role exactly the same every time.** The page suggests names you've already typed, so pick from the list.
3. **Use the role name from the posting,** not the email's shorthand.
4. **When unsure, write it down.** Pick your best guess and keep a list of the row numbers. Those are your hardest cases, and the most valuable ones.

### What to look for

Any classifier gets clean confirmations right. The emails that make the eval useful:

- Two roles at the same company
- Talent-community and "we'll keep your resume on file" mail
- Vague "next steps" emails that aren't clearly a screen
- Rejections written softly ("we've decided not to proceed at this time")
- Scams that look like real recruiters
- Replies in a thread where the latest message changes the status

If confirmations make up most of what you've labeled, go looking for more of these.

## 4. Build fixtures and extend the split (1 min)

```bash
python3 -m evals.dataset build --db ~/.jobtrack/jobs.db --db evals/raw/pool.db
python3 -m evals.dataset split
```

`split` gives new fixtures a side (dev or holdout) and **never moves existing ones**. About 20% of each kind goes to holdout.

## 5. Record the baseline

These call the classifier model, which sends each email's text to Anthropic (the same thing the hourly classifier already does).

```bash
python3 -m evals.run --set dev
python3 -m evals.run --set holdout
```

Copy the holdout headline numbers into the M1 row of the log in `docs/DESIGN.md`.

**The holdout rule:** use `--set dev` while changing prompts, parsers, or thresholds. Run `--set holdout` only to report results. If you tune because of a holdout number, that number stops being honest.

---

## Files

| File | What it is | In git? |
|---|---|---|
| `evals/raw/label-me.csv` | Your labels | No |
| `evals/raw/pool.db` | Extra mail used only for labeling | No |
| `evals/fixtures.jsonl` | Fixtures built from your labels | No |
| `evals/raw/split.json` | Frozen dev/holdout assignments | No |
| `evals/fixtures.example.jsonl` | Redacted examples | Yes |
