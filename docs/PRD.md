# jt — product requirements

**Owner:** Nicole Rodriguez · **Last updated:** 2026-11-13 (after #49) · **Status:** v1 (for me) in progress
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
| Job emails missing from jt | 0 per week | `jt sync --audit` | 13 in 90 days before widening; search widened. A 6-day sign-in outage (Nov 6–12) now triggers an alert (#43) |
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
| R19 | Gmail sign-in path tested | ❌ Next |

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

Engineering Now items are **done**: E1–E4, R9, R11–R18 (see §7). What remains is evidence only you can produce.

| # | Owner | Task | Done when |
|---|---|---|---|
| **Now-1** | Nicole | ✅ Clear the review queue | Done: 0 pending |
| **Now-2** | Nicole | **Start the test week:** use the menu instead of Gmail; close out the 10 stale applications; note every time you fall back to Gmail and why | 5 active days logged |
| **Now-3** | Nicole | Label the 117 emails (`python3 evals/label_server.py`), deadline and assessment emails first | `evals.dataset check` shows ≥ 150 labeled |
| **Now-4** | Nicole | **Urgent, before ~Nov 19:** switch Google OAuth from Testing to In production (free) | No more weekly sign-in expiry |
| **Now-5** | Nicole | Confirm notifications reach you (System Settings → Notifications → Script Editor → Allow) and keep the Mac plugged in | A test notification is seen |
| **Now-6** | Nicole | Approve removing leftover local copies and the running demo app | ≤ 1 dev copy + production |

### NEXT (after the test week). Exit: an M6 decision recorded with evidence.
- Run the baseline: `python3 -m evals.run --set holdout --policy compare` and record it in DESIGN.md §10.
- R10 `jt explain`, R19 Gmail sign-in tests, splitting `jt.py`: chosen by what the test week shows hurts most.
- **M6 decision rule:**
  - Panel opened ≥ 4 of 5 days **and** you wanted it beside Gmail → build the native app.
  - Gmail fallbacks were caused by missing or wrong data → more classifier work.
  - Neither → stop adding UI.

### LATER (only with evidence)
- Native app (Finder launch, notifications), then Apple Reminders for phone reach.
- v2 beta: Homebrew install, OAuth in production mode, onboarding doc, ~10 Pursuit users (under Google's 100-user cap).
- Write-up and demo (M7), then build-in-public posts.
- v3 public launch checklist (signing, notarization, Google verification, model choice): see the conversation notes; start only after the v2 metrics hold.

## 11. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Labeling stalls, so accuracy is never proven | High | Small sittings; deadline emails first; the eval runs at any size, with honest intervals |
| Review fatigue makes jt feel like work | Medium | E1 + E2 cut noise; track the review-workload metric |
| Live model behaves differently from tests (as the empty-request bug did) | Medium | A live replay on throwaway copies before trusting a new feature |
| Production drifts behind `main` | Medium | E3 deploy script + version in the panel |
| Scope creep (native, phone, launch) before v1 works | High | **This file's Now list is the only queue** |
| OAuth Testing mode expires tokens weekly | Unknown | Now-4 |

## 12. Open decisions

| Decision | Recommendation | Decide by |
|---|---|---|
| Native app go / no-go | Follow the M6 rule above | End of the test week |
| Signed commits? | Unsigned is fine (not required by branch protection); keep it consistent | Now |
| Enable repo auto-merge? | Optional; CI is already required | Any time |
| Ignored-bucket sender list | Start from the ATS and assessment list in `sync.py`; tune from real misses | E2 |
