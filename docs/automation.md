# jt Automation

Local unattended automation is handled by launchd calling thin shell wrappers
around the existing jt entry points. The wrappers do not change tracker
behavior: they run sync, then classification, then `jt review stale`, then
the stale summary. Hard command failures stay launchd-visible; routine
review-health alerts stay visible in logs and in the daily notification
without making the hourly job look crashed.

## Files

- `scripts/jt-automation.sh` -- runs sync + classify + `jt review stale
  --no-fail` (+ dry-run variants). No notification side effects -- hard
  command failures surface as this script's own non-zero exit, visible to
  launchd/log inspection, while ordinary review alerts are logged without
  failing the hourly job.
- `scripts/jt-daily-stale.sh` -- runs the automation script, then `jt
  stale` and `jt review stale --no-fail`, then a single macOS notification
  if either a hard failure or a review-health alert is present. This is
  the only thing in the automation layer that actually pages a human --
  see "Alerting" below.
- `scripts/install-launchd.sh` -- renders the templates below for this
  machine and optionally loads them
- `launchd/dev.nicole.jt.sync.plist.template`
- `launchd/dev.nicole.jt.daily-stale.plist.template`

The two `scripts/*.sh` wrappers derive their own repo location from
`BASH_SOURCE` -- they work from any clone path without edits. The `.plist`
files are the one place that can't self-derive its path (launchd plists are
static XML, no shell expansion), which is why those ship as `.template`
files with `__REPO_DIR__` / `__JOBTRACK_HOME__` placeholders instead of
literal `.plist` files. `install-launchd.sh` fills those in and writes the
result to `~/Library/LaunchAgents/`, which is machine-local and not tracked.

## Fresh clone / new machine setup

```bash
git clone <repo> jt && cd jt
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[gmail,classify,dev]"

# first sign-in on this machine (no token yet): opens Google in your browser
# and saves ~/.jobtrack/token.json, even in --dry-run. Rerunning it also
# fixes an EXPIRED sign-in; see "When the Gmail sign-in expires" below.
.venv/bin/python sync.py --dry-run

# render + load the launchd jobs for this machine
scripts/install-launchd.sh --load
```

`install-launchd.sh` (no flags) only renders the plists into
`~/Library/LaunchAgents/` so you can review them before loading; pass
`--load` to also `launchctl bootstrap` them. Re-run with `--load` any time
you edit a `.template` file.

## Runtime paths

Everything defaults under `~/.jobtrack` and is overridable by environment
variable, the same pattern `jt.py` already uses for `JT_DB`:

| Purpose | Env var | Default |
|---|---|---|
| Tracker database | `JT_DB` | `~/.jobtrack/jobs.db` |
| Structured run logs (`jt-sync.jsonl`, `jt-classify.jsonl`) | `JT_LOG_DIR` | `~/.jobtrack/logs` |
| Google OAuth client secret | `GOOGLE_OAUTH_CLIENT` | (required, no default -- keep outside the repo) |
| Google OAuth token cache | `GOOGLE_TOKEN_PATH` | `~/.jobtrack/token.json` |
| Python interpreter used by the shell wrappers | `JT_PYTHON` | `<repo>/.venv/bin/python` |
| Automation's own `.jobtrack` root (used by `install-launchd.sh`) | `JT_HOME` | `~/.jobtrack` |
| Local env file read by shell wrappers | `JT_ENV_FILE` | `~/.jobtrack/jt.env` |
| Review queue size alert threshold | `JT_REVIEW_ALERT_THRESHOLD` | `1` |
| Oldest unresolved review age threshold | `JT_REVIEW_AGE_ALERT_DAYS` | `2` |
| Consecutive classify-failure alert threshold | `JT_CLASSIFY_FAILURE_ALERT_THRESHOLD` | `3` |
| Notifier used by `jt-daily-stale.sh` (override with a fake for tests) | `JT_OSASCRIPT` | `/usr/bin/osascript` |
| Your own iMessage address for phone alerts (see "Phone alerts") | `JT_PHONE` | unset (Mac banners only) |

None of these need to be set for a single-user, single-machine setup --
they exist so a second machine, a renamed clone, or a different account
doesn't require editing source or scripts.

The usual jt environment still applies:

```bash
GOOGLE_OAUTH_CLIENT=~/.jobtrack/google_oauth_client.json
GOOGLE_TOKEN_PATH=~/.jobtrack/token.json
ANTHROPIC_API_KEY=...
```

For interactive terminal use, put those values in the repo-local `.env`
file and source it before running `sync.py` or `classify.py`.

For launchd, put the same values in a local env file outside the repo:

```bash
mkdir -p ~/.jobtrack
chmod 700 ~/.jobtrack
cat > ~/.jobtrack/jt.env <<'EOF'
GOOGLE_OAUTH_CLIENT=/Users/nicolerodriguez/.jobtrack/google_oauth_client.json
GOOGLE_TOKEN_PATH=/Users/nicolerodriguez/.jobtrack/token.json
ANTHROPIC_API_KEY=...
CLASSIFIER_MODEL=claude-sonnet-5
EOF
chmod 600 ~/.jobtrack/jt.env
```

`jt-automation.sh` and `jt-daily-stale.sh` load this file automatically
before running Python. If a different path is needed, set `JT_ENV_FILE` in
the launchd plist template and rerun `scripts/install-launchd.sh --load`.

The launchd examples set `JT_UNATTENDED=1`. In that mode, `jt-sync` fails
fast if `GOOGLE_TOKEN_PATH` is missing or invalid instead of trying to open
an interactive OAuth browser flow.

Interactive OAuth waits up to five minutes by default. Override this with
`GOOGLE_OAUTH_TIMEOUT_SECONDS`; set it to `0` to wait indefinitely.

If Google shows an `org_internal` or organization-restricted authorization
error, the local files are not the problem. Create or reconfigure the OAuth
app audience in Google Cloud so it is available to the Gmail account that
will run sync. For a personal Gmail account, use an External audience and
add that Gmail address as a test user while the app is in Testing mode.

## When the Gmail sign-in expires

**Symptoms** (any one):

- the menu bar shows `⚠` and "Gmail sign-in expired";
- `~/.jobtrack/logs/jt-sync.jsonl` ends with `Gmail token refresh failed with
  invalid_grant` or `GOOGLE_TOKEN_PATH does not exist`;
- applications you know you sent never show up under "New applications found".

**Why it happens.** Google cancels the saved token every 7 days while the
OAuth app in Google Cloud is in **Testing** mode. Changing your Google
password or removing jt's access in your Google account cancels it too.
Nothing is lost while it's expired: the next good sync looks back past the
last successful day and picks up everything it missed.

**Why the hourly job can't fix it.** The hourly job runs with
`JT_UNATTENDED=1`, so on `invalid_grant` it stops and logs the error instead
of opening a browser nobody is watching. Run sync yourself in a normal
Terminal (without `JT_UNATTENDED`) and it opens the Google sign-in for you.
The old `token.json` stays in place until the new one is saved over it, so
there is nothing to move or delete.

**Fix (about 2 minutes, on the Mac that runs jt):**

```bash
cd ~/Developer/jt

# 1. load jt's paths; do NOT set JT_UNATTENDED here
set -a; . ~/.jobtrack/jt.env; set +a

# 2. sign in and sync. Prints a Google link and usually opens it for you.
#    Waits 15 minutes for you to finish. Use jt's own Python: a bare
#    `jt-sync` may run some other install on your PATH.
GOOGLE_OAUTH_TIMEOUT_SECONDS=900 .venv/bin/python sync.py

# 3. classify what just came in now instead of waiting for the next hour
launchctl kickstart gui/$(id -u)/dev.nicole.jt.sync
```

In the browser:

1. Open the link **on this same Mac**. Google sends you back to
   `http://localhost:<port>/`, which only this Mac can answer. A phone will
   show an error.
2. Choose the Gmail account jt reads.
3. "Google hasn't verified this app" is expected for your own app: click
   **Advanced**, then **Go to (app name)**.
4. Tick the Gmail read-only box and click **Continue**.
5. The page should say "The authentication flow has completed." Step 2 then
   prints `inspected N, inserted N`.

If the link opens but the last page errors on `localhost:<port>`, step 2 has
probably timed out. The old token is still there, so just rerun step 2 and
finish within the window.

**Check it worked:**

```bash
ls -l ~/.jobtrack/token.json                   # exists, mode -rw-------
tail -1 ~/.jobtrack/logs/jt-sync.jsonl         # "event": "success"
.venv/bin/python jt.py candidates              # anything new from email
```

**Stop it recurring:** in Google Cloud Console → Google Auth Platform →
Audience, click **Publish app** to move it from Testing to **In production**.
It's free. For a personal app used by you alone, skip Google's verification:
you'll keep seeing the "unverified app" warning at sign-in, but the token
stops expiring every 7 days and lasts until you revoke it.

## Phone alerts (iMessage)

The Gmail-outage alert can also reach your phone as an iMessage from this Mac
to yourself. It names no companies. Task and interview alerts stay on the Mac
for now.

1. Make sure Messages on this Mac is signed in to iMessage with the same Apple
   ID as your phone.
2. Add your own iMessage address (phone number with country code, or Apple ID
   email) to `~/.jobtrack/jt.env`:
   ```bash
   JT_PHONE=+15555550123
   ```
3. Grant permission **once, from Terminal**. The first send makes macOS ask
   whether Terminal (or `osascript`) may control Messages; the hourly job runs
   without a screen and can't answer that prompt.
   ```bash
   osascript -e 'on run argv
     tell application "Messages" to send (item 2 of argv) to participant (item 1 of argv) of (1st account whose service type = iMessage)
   end run' "$JT_PHONE" "jt test: phone alerts work"
   ```
   Click **OK**. If it was denied earlier: System Settings → Privacy & Security
   → Automation → allow Messages.

If sending keeps failing, the alert is retried every hour, and after 3 runs the
menu bar shows "Phone alerts aren't reaching you" with what to check. Messages
you send yourself may arrive without a sound on some setups; check once that the
test message notifies on your phone.

## Verify

Run setup verification without Gmail, Anthropic, or notification side
effects:

```bash
scripts/jt-automation.sh --verify
scripts/jt-daily-stale.sh --verify
```

Run the pipeline in dry-run mode:

```bash
scripts/jt-automation.sh --dry-run
scripts/jt-daily-stale.sh --dry-run
```

`--dry-run` still reaches Gmail and the classifier, because it delegates to
the existing `sync.py --dry-run` and `classify.py --dry-run` behavior.

Run the review health gate directly:

```bash
.venv/bin/python jt.py review stale
```

`jt review stale` checks unresolved review queue size, oldest unresolved
review age, classifier exhaustion events in `jt-classify.jsonl`, and
repeated classify failures. It exits non-zero when an alert is present.
For a human-readable check that should never fail a surrounding shell
script, use `jt review stale --no-fail` -- this is what the launchd
wrappers use when they want visibility without treating queue hygiene as a
hard crash.

## Alerting

Two layers, deliberately not the same thing:

- **`jt-automation.sh` (hourly, via `dev.nicole.jt.sync`)**: no
  notifications. A classify crash makes this run exit non-zero on its own
  (the classify step's own exit status, preserved verbatim) -- that
  failure is visible within the hour via `jt-automation.log` or `launchctl
  print`/exit-status inspection, independent of any threshold. The
  `jt review stale --no-fail` step it also runs mostly catches a
  *different* thing: review-queue buildup on runs where classify itself is
  technically healthy. Those alerts are logged hourly but not treated as
  process crashes.
- **`jt-daily-stale.sh` (once/day at 8:30 AM)**: the only thing that
  actually pages via a macOS notification. It runs its own separate
  `jt-automation.sh` invocation, then unconditionally checks health and
  notifies exactly once, combining both signals into one message instead
  of firing twice:
  - if that invocation exited non-zero for any reason (classify failure,
    sync failure, a DB error -- anything), the notification fires
    regardless of what the review-alert history says, since a hard crash
    never touches `jt-classify.jsonl` history the way a review-alert
    threshold does;
  - otherwise, if `jt review stale --no-fail` reports any alert (queue
    size, queue age, exhaustion, or repeated failures read from
    `jt-classify.jsonl`'s accumulated history), the notification fires
    with wording naming which condition tripped.
  - if neither is true, no notification fires at all -- silence is the
    correct, verified default for a healthy run.

  The script is structured so a failure in the automation step still
  reaches this notification logic (it does not die before attempting to
  notify) and still exits non-zero afterward, so both the notification and
  the exit-code signal are preserved together, not one at the cost of the
  other.

**Threshold review, stated plainly:**

- `JT_REVIEW_ALERT_THRESHOLD=1` -- any single unresolved item pages daily
  until resolved. Deliberately sensitive, matching jt.py's own "a growing
  queue is the failure signal" design. Tradeoff: a low-priority item left
  unresolved on purpose repeats daily; raise the threshold if that becomes
  noise in practice.
- `JT_REVIEW_AGE_ALERT_DAYS=2` -- currently redundant at these defaults,
  since threshold `1` already alerts on every non-empty queue before age
  ever matters. It only adds signal if the size threshold is raised above
  `1` (tiered alerting: ignore 1-2 minor items, but page if anything sits
  past N days).
- `JT_CLASSIFY_FAILURE_ALERT_THRESHOLD=3` -- in practice this is mostly a
  backstop for `jt-daily-stale.sh`'s own run and for a human reading `jt
  review stale` directly; a hard classify failure already surfaces via
  `jt-automation.sh`'s own exit code (see above) well before three
  failures accumulate.
- **Detection latency**: a hard crash pages same-day (whenever
  `jt-daily-stale.sh` next runs, at most ~24h, typically sooner if you run
  it manually). A pure review-alert condition (no crash, just queue
  buildup) is also bounded by that same ~24h daily cadence -- there is no
  faster, notification-based path today. If that latency is too slow,
  the fix is adding a notification call to the hourly `jt-automation.sh`
  path, which this repo deliberately does not do yet, to keep the hourly
  job free of side effects beyond the pipeline itself and to avoid hourly
  notification spam during a multi-hour outage.

## Install / reload / uninstall launchd jobs

```bash
# render + load both jobs
scripts/install-launchd.sh --load

# reload after editing a .template (bootout + bootstrap)
scripts/install-launchd.sh --load

# kick a job manually
launchctl kickstart -k gui/$(id -u)/dev.nicole.jt.sync
launchctl kickstart -k gui/$(id -u)/dev.nicole.jt.daily-stale

# unload
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/dev.nicole.jt.sync.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/dev.nicole.jt.daily-stale.plist
```

## Schedule

- `dev.nicole.jt.sync` runs hourly and at load. Each run executes sync,
  classify, review-health checks, and then the stale summary. If classify
  exits non-zero, the wrapper preserves that exact exit status. If
  `jt review stale --no-fail` finds alerts, the wrapper logs them and
  leaves the daily notification job to page once with human-readable
  wording.
- `dev.nicole.jt.daily-stale` runs daily at 8:30 AM.

Both scripts write failures to `$JT_LOG_DIR` (default `~/.jobtrack/logs`).
`sync.py` and `classify.py` also write JSON-lines run records to
`jt-sync.jsonl` and `jt-classify.jsonl` there. See "Alerting" above for
exactly when and how `jt-daily-stale.sh` notifies -- in short, the hourly
job never notifies (log/exit-code visibility only), and the daily job
notifies at most once per run, on a hard failure or a review-health alert,
and stays silent otherwise.
