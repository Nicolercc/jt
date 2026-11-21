# jt — product requirements

**Owner:** Nicole Rodriguez · **Last updated:** 2026-11-21 (CEO review: §10.2, Now-7, Now-8) · **Status:** v1 (for me) in progress
**This is the plan.** Work that isn't in the **Now** list below doesn't start, for Claude, Codex or anyone else, until this file changes.
How it works: [ARCHITECTURE.md](ARCHITECTURE.md). Why decisions were made: [DESIGN.md](DESIGN.md).

---

## 1. TL;DR

jt is the place you look **before** searching Gmail for job-search news. It records where you applied, notices what changed, turns recruiter requests into tasks with deadlines, and never changes anything important without showing you the evidence.

**North-star metric:** explicit deadlines missed = **0**.

## 2. Problem, with evidence

Job-search updates are scattered across Gmail, assessment platforms and memory. The costly failures are quiet.

**The Northstar case** (real data): 8 Northstar emails arrived, and **4 never reached the tracker**: a CodeSignal assessment, a Hireflix video interview, an interview reminder, and an interview-complete note. Even the ones that did arrive couldn't become a task, because jt had no email → action path. The menu then showed a misleading "Northstar · 50 business days quiet". A 90-day audit found **13 missed job emails** in total.

| Today's workaround | Result |
|---|---|
| Search Gmail, remember deadlines, scan the inbox | Missed the Northstar assessment; no single view of what needs doing |
| Existing trackers (Teal, Huntr, JobFlow, Trackr…) | Track *status* columns. None show *why* a status changed or turn deadlines into tasks with evidence. |

## 3. Users

| Phase | User | Needs |
|---|---|---|
| **v1 (now)** | Nicole: Mac, Gmail, an active search | Never miss a deadline; know what needs her today; trust every change |
| v2 | ~10 Pursuit peers, technical, on Macs | Install in under 15 minutes; same value without hand-holding |
| v3 | Any Mac job seeker with Gmail | Install in under 3 minutes; no Python, no API keys, no Google Cloud setup |

## 4. Jobs to be done

1. *When a recruiter asks me to do something,* show it as a task with its deadline and a link to the email, so I don't miss it.
2. *When I glance at my menu bar,* tell me what needs me today in under 5 seconds, so I can stop checking Gmail.
3. *When jt changes an application's status,* let me see why in one click, so I can trust it.
4. *When jt isn't sure,* ask me, so a wrong guess never hides a live opportunity.
5. *When something is broken* (sync, sign-in), tell me plainly, so I don't trust an empty screen.

## 5. Goals and non-goals

**Goals (v1)**
- Every recruiter request with a deadline becomes exactly one task, linked to its email.
- Every job email that matters reaches jt (coverage).
- A glanceable menu with a verb on every row and no dead ends.
- Accuracy published with sample sizes; zero wrong closures.

**Non-goals (v1)**
- No server, no cloud sync, no phone app (phone reach comes later through Apple Reminders).
- No auto-applying to jobs, no job search or discovery.
- No multi-user support, no Outlook.
- No native app until M6 says go.

## 6. Success metrics

| Metric | Target | Source | Current |
|---|---|---|---|
| **Explicit deadlines missed** (north star) | 0 per week | Test-week log + audit | Unknown; Northstar was missed before the fixes |
| Job emails missing from jt | 0 per week | `jt sync --audit` | 13 in 90 days before widening; search widened. A 6-day sign-in outage (Nov 6–12) now triggers an alert (#43). A second ~70-hour outage (Nov 18–21, Testing-mode expiry) went unnoticed despite that alert; see N-1 |
| Wrong auto-updates | ≤ 2 per 100 emails | Eval holdout | Unmeasured (n = 5) |
| Wrong terminal auto-commits | 0 | Eval + live | 0 (D3 caught a wrong "ghosted" live) |
| Review workload | ≤ 5 items per active day | `review_queue` | 0 pending after cleanup (non-job mail now goes to `jt ignored`) |
| Email → task latency | < 1 hour | `classified_at` vs. received | About 1 hour (hourly job) |
| Panel use | Opened on ≥ 4 of 5 active days | `usage.log` | 1 day so far (Nov 13: 28 opens, 12 clicks) |

## 7. Requirements

### P0: v1 can't ship without these
| # | Requirement | Status |
|---|---|---|
| R1 | Sync covers ATS, assessment and interview mail; audit proves coverage | ✅ #25, deployed, widened |
| R2 | Email request → one task with a validated deadline and source link | ✅ #26, #28 (live-verified on Northstar/Hireflix) |
| R3 | Consequential changes (rejection, offer…) always need confirmation | ✅ #15 |
| R4 | Menu: due tasks, reviews, suggestions; every row has a verb; failures visible | ✅ #16, #27 |
| R5 | Two roles at one company resolve automatically when the evidence allows | ✅ #30 |
| R6 | Review only shows things that need a decision (non-job mail goes to "ignored") | ✅ #32 |
| R7 | Accuracy measured on ≥ 150 labeled emails, 20% frozen holdout | ❌ **Now-3** (23 labeled) |
| R8 | One-command deploy with a version check | ✅ #33 (`scripts/deploy.sh`, `jt version`, version in panel footer) |

### P1: v1 polish
| # | Requirement | Status |
|---|---|---|
| R9 | Review in one place: plain-English summary, only legal choices, one-click Record / Dismiss / Open email in the menu bar; readable `jt review` | ✅ #36 (pulled from Next into Now after real use showed Review was unusable) |
| R10 | "Why?" for every automatic change (`jt explain`) | ❌ Next |
| R11 | Weeks-quiet applications become "Close out?" with legal choices (no "50 days quiet" nudges) | ✅ #38, #41 (no-response allowed through the final round) |
| R12 | A reminder email for an existing open task updates that task instead of adding a duplicate | ✅ #40 |

### P0 from the 2026-11-13 audit
| # | Requirement | Status |
|---|---|---|
| R13 | Tell me when Gmail sync is broken (it was silently dead Nov 6–12) | ✅ #43 (notification after 2 h, once a day) |
| R14 | Notify for tasks due today or overdue | ✅ #43 |
| R15 | New applications found in email appear in the menu (Add / Not mine) | ✅ #45 |
| R16 | An old-shaped candidates table can't crash the classifier | ✅ #44 (self-repair, rows kept) |
| R17 | No real job-search names in the public repo | ✅ #47, #48 (main + PR text); git history: owner's call |
| R18 | Everyday commands and live-model failures are pinned by tests | ✅ #46, #49 |
| R19 | Gmail sign-in path tested | ✅ #53: sign-in code 100% exercised (sync.py 74% → 89%); the revoked-token → alert chain tested end to end |
| R20 | An expired sign-in is recoverable by running `jt-sync` once, with a runbook | ✅ #54 (runbook + README guide), #55 (interactive sync replaces a revoked token), deployed b7ace33 |

### P2: after M6
Native star app (Finder launch, notifications), Apple Reminders for phone reach, Homebrew install for v2 users, Keychain for the OAuth token.

## 8. UX principles

1. **Answer "what needs me now?" first.** Due tasks, then reviews, then optional suggestions, then freshness.
2. **Every row has a verb.** No dead ends, no information-only rows.
3. **Evidence is one click away.** Every task and update links to its email and quote.
4. **Never color alone.** Pair ⚠ with words; screen-reader labels on everything; the full flow works from the keyboard.
5. **An empty screen states why it's empty:** "Nothing due · synced 6 min ago" vs. "Sync failed 3 h ago".
6. **Privacy by default.** No company names in the menu-bar title; data stays on the Mac.

## 9. Where we are in the plan

| Milestone (DESIGN.md) | Status | Notes |
|---|---|---|
| M1 Eval growth | 🟡 Tooling ✅, **labels 23 / 140** | **Blocked on Nicole** |
| M2 Correctness core (D1, D2, D3, D6) | ✅ Done | Live |
| M3 Template parsers | ⏸ Deprioritized | Real failures are role ambiguity and coverage, not templates. Revisit with label data. |
| M4 Read model, writes, backups (D5, D7) | ✅ Done | Live |
| *Added:* coverage + email → action | ✅ Done | Built because the Northstar trace demanded it; live-verified |
| M5 Test week | 🟡 Panel + alerts ready; **week not started** | 1 day of usage (Nov 13) |
| M6 Go / no-go on native | ⏳ Waiting on M5 | Native drafts #21/#22 exist ahead of this gate; frozen |
| M7 Demo + write-up | ⏳ | Outline exists: `docs/WRITEUP.md` (local) |

**Drift corrected (2026-11-13):** old drafts #17–#20 closed, #21/#22 frozen; docs refreshed with a PR checklist so they stay current. **Still open:** 14 local repo copies and a running demo app (owner approval to remove).

## 10. The plan

### NOW (this week). Exit: the test week running, labels at least 50% done, OAuth in production.

Engineering Now items E1–E4, R9, R11–R18 are **done** (see §7). The 2026-11-21 CEO review added two engineering milestones, Now-7 and Now-8, which run alongside the test week so it measures a reliable jt that sees every application. Plan of record: [§10.2](#102-ceo-review-2026-11-21-integration-first).

| # | Owner | Task | Done when |
|---|---|---|---|
| **Now-1** | Nicole | ✅ Clear the review queue | Done: 0 pending |
| **Now-2** | Nicole | **Start the test week:** use the menu instead of Gmail; close out the 10 stale applications; note every time you fall back to Gmail and why | 5 active days logged |
| **Now-3** | Nicole | Label the 117 emails (`python3 evals/label_server.py`), deadline and assessment emails first | `evals.dataset check` shows ≥ 150 labeled |
| **Now-4** | Nicole | **Overdue (it expired Nov 18):** switch Google OAuth from Testing to In production (free), then sign in once more so the token is a production one | Audience page says "In production"; no `⚠` after Nov 28 |
| **Now-5** | Nicole | Confirm notifications reach you (System Settings → Notifications → Script Editor → Allow) and keep the Mac plugged in | A test notification is seen |
| **Now-6** | Nicole | Approve removing leftover local copies and the running demo app | ≤ 1 dev copy + production |
| **Now-7** | Engineering | **Milestone R, reliability** (T1–T4 in §10.2): alerts that can't fail silently, iMessage sync-down alert, external watchdog on real health (sync + backlog + failures), Sign in again from the menu | An outage is reported to the phone within 3 hours even if the Mac is off |
| **Now-8** | Engineering + job-search | **Milestone C, complete capture** (T5–T10): versioned inbox contract from job-search, content-addressed materials that are backed up, exact application identity for both arrival orders, backfill | jt's application count equals job-search's "applied" count |

### NEXT (after the test week). Exit: an M6 decision recorded with evidence.
- **Choose the next build from measured misses**, not a schedule: job-post liveness (E2), phone alerts for tasks/interviews/deadlines (E3), interview briefs (E4), weekly report (B4), or classifier work. See §10.2.
- Run the baseline: `python3 -m evals.run --set holdout --policy compare` and record it in DESIGN.md §10.
- R10 `jt explain`, splitting `jt.py`: chosen by what the test week shows hurts most.
- **M6 decision rule:**
  - Panel opened ≥ 4 of 5 days **and** you wanted it beside Gmail → build the native app.
  - Gmail fallbacks were caused by missing or wrong data → more classifier work.
  - Neither → stop adding UI.

### LATER (only with evidence)
- Native app (Finder launch, notifications), then Apple Reminders for phone reach.
- v2 beta: Homebrew install, OAuth in production mode, onboarding doc, ~10 Pursuit users (under Google's 100-user cap).
- Write-up and demo (M7), then build-in-public posts.
- v3 public launch checklist (signing, notarization, Google verification, model choice): see the conversation notes; start only after the v2 metrics hold.

### 10.1 Roadmap beyond v1 (added 2026-11-21)

**Where jt stands (self-rating, 2026-11-21):**

| As… | Score | Why |
|---|---|---|
| An engineering artifact | 8.5 / 10 | Invariants enforced by SQLite, propose/commit boundary, 430 tests, CI, backup-first deploy, decision log |
| A tool that does the job for you | 5 / 10 | A ~70-hour silent outage; recovery needed an engineer; every new application still needs a click; reports what happened, not what works |
| Something others could use | 2 / 10 | Needs Google Cloud, an OAuth client, an API key, launchd, SwiftBar and Python |

The gap between the first and second rows is the roadmap: the engine is done; the experience is still an engineer's.

**Rules for this list.** Nothing here starts until it moves into **Now**. Each item names the evidence that unlocks it. Items marked ⚠ conflict with a §5 non-goal or a DESIGN decision; that decision has to be reopened with evidence first, not worked around.

| # | Item | What done looks like | Start when | Tier |
|---|---|---|---|---|
| **N-1** | **Never go blind silently.** A sync down for 2 hours pages the phone (ntfy, Pushover or iMessage), not only the Mac. The menu bar's sign-in warning gets a **Sign in again** button that runs the sign-in itself. A weekly heartbeat email: no email means jt is dead. | The next outage is noticed within 2 hours, and recovered without Terminal | **Now-eligible.** The Nov 18–21 outage is the evidence (R13's Mac alert fired and was missed) | Next |
| **N-2** | **Zero-click capture.** ⚠ *Reopens D0.5.* Auto-add an application when the sender is a known ATS domain, company and role are quoted, and the mail went to your address; show "Added · Undo" for 24 hours. | 0 wrong auto-adds across the next 50 confirmations, or it reverts to asking | After Now-2's test week shows how many candidate clicks there are, and N-1 has shipped | Next |
| **N-3** | **Prove the classifier.** Same as Now-3/R7, then publish precision and recall with intervals in the README. *Ambitious:* a weekly private eval run that updates an accuracy badge. | Published numbers with sample sizes | Now-3 done | Next |
| **L-1** | **Close the loop with the resume pipeline.** The tailored-resume workflow and jt are two systems today (the resume side keeps its own `tracker.csv`). Make jt the system of record: each application links to the exact resume, requirements and answers sent. Then report what works: response rate by resume variant, lane, source, ATS and day sent. | jt can answer "which version of my resume gets screens?" with numbers | ≥ 30 applications with linked materials and outcomes | Later |
| **L-2** | **Infer silent rejections.** Learn typical response time per ATS and company from your own history ("usually answers in 9 days; day 14 → likely closed"). *Ambitious:* check whether the job post is still up; a removed post means the role is filled → suggest close-out. | Close-out suggestions come with a probability and a reason | ≥ 50 applications with known outcomes | Later |
| **L-3** | **Interview prep that writes itself.** When a screen or interview is recorded, generate a one-page brief: the role's real needs, the three matching stories, likely weak spots, five questions, posted salary range. | A brief exists before every scheduled call | L-1 (needs linked materials) | Later |
| **L-4** | **Calendar and contacts.** Interviews from email become calendar events with prep blocks. Recruiters and hiring managers become contacts with history; a referral view shows who could refer you where. | No interview is entered by hand | After N-1 and N-2; calendar needs a new OAuth scope | Later |
| **L-5** | **Your phone, not just your Mac.** ⚠ *Conflicts with §5 "no phone app, no cloud sync".* Read-only phone view of due, decisions and pipeline; one-tap approve or add. Keep local-first with an encrypted copy in your own iCloud folder. | Triage a decision from the phone | M6 says yes to native and the test week shows away-from-desk misses | Later |
| **L-6** | **The whole search in one screen.** Weekly report: applications, response rate, screens, offers against last week; best and worst lanes and sources; days since last screen; next week's plan. *Ambitious:* a funnel chart across the whole search, also a portfolio piece. | A weekly report you actually read | L-1 has 4+ weeks of data | Later |
| **L-7** | **Setup without Google Cloud** (this is v3's "no Google Cloud setup"). Options in order of effort: email forwarding to a local address; IMAP with an app password; a Google-verified app (restricted-scope security assessment, a real cost). Then `brew install jt` and a setup wizard. | Install to tracking in 5 minutes for someone who has never opened Terminal | v2 metrics hold with ~10 users | Later |

**Status after the CEO review (§10.2):** N-1 → Now-7. N-2 is **superseded** without reopening D0.5: applications now arrive from job-search through the inbox, created by your action, not the classifier. L-1 → Now-8 as provenance only ("which resume works" claims wait for comparison groups). The liveness half of L-2 and all of L-3 are post-test-week candidates. L-4 to L-7 unchanged.

### 10.2 CEO review 2026-11-21: integration-first

Review mode: selective expansion, with an independent outside review (Codex). Full plan, findings and review report: `~/.gstack/projects/Nicolercc-jt/ceo-plans/2026-11-21-integration-first-roadmap.md` (local).

**Evidence that set the direction.** jt held 15 applications (8 Aug, 6 Sep, 0 Oct, 1 Nov). On 2026-11-20 the job-search app produced 5 applications and jt learned of 1, through a confirmation email. Gmail sync was silently down ~70 hours (Nov 18–21) although the Mac alert fired daily.

**Two apps.** *job-search* (finds roles, tailors materials) and *jt* (tracks what happens after) are separate products in separate repos. They connect through one versioned contract, the inbox; neither reads the other's internal files.

```
 job-search ──writes .tmp → rename──► ~/.jobtrack/inbox/applied-<external_id>.json
                                            │
 jt ingest: verify hashes → copy materials → DB transaction → move to processed/ (or failed/<reason> + alert)
            materials: ~/.jobtrack/materials/<sha256>.<ext>, included in backups
 Gmail → classify → mail matches an application by company AND role → event; otherwise review or candidate
```

**Key decisions**

| # | Decision |
|---|---|
| Alerts | iMessage to yourself; a failed send is never recorded as sent; external watchdog (healthchecks.io ping, no data) only when sync is ok, unclassified mail is < 2 h old, and nothing failed |
| Contract | Drop-folder inbox, JSON Schema v1 in `docs/contracts/`; atomic writes; replay-safe; same `external_id` with a different payload is a conflict, not a no-op |
| Identity | `external_id` is identity; ingest absorbs a matching pending candidate; mail to a company with ingested apps must match the role, else review; exact normalized company names (no substring matches) |
| Materials | Copied and content-addressed at ingest; backed up and verified |
| Resume variants | Stored as provenance (`variant_id`, lead-bullet tags); comparisons only with ≥ 5 applications per variant and a 21-day window |
| Liveness (candidate) | Public ATS APIs first; "role likely filled" only if the post is gone AND 10+ quiet business days AND stage is still applied/confirmed |
| Briefs (candidate) | On by default with `JT_BRIEFS=0`; post text treated as data; no tools; contact details stripped |
| Code size | The line cap is retired (see README §Scope): each PR states what it adds and what it cut; new features in their own modules; `jt.py` may only shrink |

**Build tasks, one PR each, in order**

| # | PR | Done when |
|---|---|---|
| T1 | Notification delivery: check the result; failed sends not recorded; warning after 3 | Tests for success, failure, 3-failure warning |
| T2 | iMessage channel (`JT_PHONE`), argv-safe, private by default | A sync-down alert reaches the phone |
| T3 | Health + external watchdog | Each unhealthy reason tested; the watchdog fires with the Mac off |
| T4 | "Sign in again" in the menu | An expired sign-in is fixed from the menu |
| T5 | Contract v1 JSON Schema + docs | An example event validates |
| T6 | Schema 9: `external_id` UNIQUE, materials store | Additive migration tested on a copy of the live DB |
| T7 | Ingest: atomic, replay-safe, conflicts, failed/ reasons, security checks | Crash test at each step |
| T8 | Application identity: role-aware matching, candidate absorption | Email-first, ingest-first, two-role and manual-app tests |
| T9 | Materials in backup + verify | verify-backup fails on a missing or altered file |
| T10 | *(job-search repo)* emitter + one-time backfill | jt's count = job-search's applied count |

**Not in scope:** auto-adding from email (N-2 as written), phone app and cloud sync (L-5), setup for other people (L-7), calendar and contacts (L-4).

## 11. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Labeling stalls, so accuracy is never proven | High | Small sittings; deadline emails first; the eval runs at any size, with honest intervals |
| Review fatigue makes jt feel like work | Medium | E1 + E2 cut noise; track the review-workload metric |
| Live model behaves differently from tests (as the empty-request bug did) | Medium | A live replay on throwaway copies before trusting a new feature |
| Production drifts behind `main` | Medium | E3 deploy script + version in the panel |
| Scope creep (native, phone, launch) before v1 works | High | **This file's Now list is the only queue** |
| OAuth Testing mode expires tokens weekly | **Happened** (Nov 6–12 and Nov 18–21) | Now-4; R20 makes recovery one command; N-1 makes it noticed |
| Roadmap ambition pulls work ahead of v1 | High | §10.1 items carry a "start when" gate and only start once moved into Now |

## 12. Open decisions

| Decision | Recommendation | Decide by |
|---|---|---|
| Native app go / no-go | Follow the M6 rule above | End of the test week |
| Signed commits? | Unsigned is fine (not required by branch protection); keep it consistent | Now |
| Enable repo auto-merge? | Optional; CI is already required | Any time |
| Ignored-bucket sender list | Start from the ATS and assessment list in `sync.py`; tune from real misses | E2 |
| Reopen D0.5 for zero-click capture (N-2)? | Yes, but only with test-week counts of candidate clicks and an undo window | After Now-2 |
| Phone reach: Apple Reminders (§5) or a phone view (L-5)? | Reminders first; phone view only if misses happen away from the Mac | At M6 |
