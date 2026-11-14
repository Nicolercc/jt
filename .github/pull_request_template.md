## Problem

<!-- What's wrong or missing, and the evidence (a test, a log line, a real-data replay). -->

## Change

<!-- What changed and why this approach. -->

## Test plan

- [ ] Failing test written first and seen failing (or, for tests of existing behavior, a deliberate break shown to be caught)
- [ ] `pytest` and `mypy` clean; `scripts/secret-scan.sh` passes
- [ ] Classification change? Replayed on a throwaway copy of the live database, and any new live-model failure added to `tests/fixtures/live_regressions.json`

## Keep the repo honest

- [ ] **No real job-search names** (companies, roles, outcomes) in code, tests, docs or this description. The repo is public; anonymize real cases first
- [ ] `docs/PRD.md` row updated (status + PR number) if this ships or changes a requirement
- [ ] `docs/ARCHITECTURE.md` updated if behavior, schema, a flow or an invariant changed
- [ ] Runtime change? Deploy with `scripts/deploy.sh` after merge and check `jt version`
