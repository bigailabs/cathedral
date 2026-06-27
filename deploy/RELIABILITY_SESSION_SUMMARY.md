# Cathedral Reliability Session Summary

**Date:** 2026-06-27
**Branch:** `reliability-integrated`
**Reviewed code head:** `9279629` before this docs-only summary correction.
**Status:** Code-complete for review. Nothing in this branch has been merged to
`main` by this document, and no production flag should be flipped without an
explicit go-live approval.

This branch contains the reliability plan plus the integrated submit/read tracks:

- Validator weight-feed protection and release-gate tooling.
- Publisher role split hardening for read, submit, and worker services.
- Durable submit admission and async verification, default-off.
- PM/private challenge async admission, shadow mode, and queue visibility,
  default-off.
- Per-hotkey abuse limiter, default-off.
- Materialized board and leaderboard snapshots, default-off.
- Board-failover and weights-failover Cloudflare worker code, routes commented
  unless explicitly cut over.
- Miner-facing error contract and observability docs.

## Review Status

I reviewed the integrated branch in a clean worktree at:

```text
C:\Users\fred\code\cathedral-review-reliability-9279629
```

The branch is based on current `origin/main`:

- `origin/main` is an ancestor of `9279629`.
- GitHub remote `refs/heads/reliability-integrated` pointed at `9279629` when
  this review started.
- There is currently no open PR for this branch.

Relative to `main`, the branch changes 35 files:

```text
35 files changed, 8617 insertions(+), 82 deletions(-)
```

## Verified Locally

Python checks were run with the WSL venv:

```text
/mnt/c/Users/fred/code/cathedral/.venv/bin/python
pytest 9.1.1
```

Passing checks:

- `pytest scaffold -q`: 142 passed.
- Targeted reliability/submit pytest set: 139 passed.
- `pytest game/tests -q`: 15 passed.
- `publisher_verify.py`: 152 checks passed.
- `assigned_lane_verify.py`: 15 checks passed.
- `weights_verify.py`: 32 checks passed.
- `python -m compileall -q scaffold scripts`: passed.
- `deploy/edge-router/worker.test.mjs`: passed.
- `deploy/edge-router/weights-failover/worker.test.mjs`: passed.
- `deploy/edge-router/board-failover/worker.test.mjs`: passed.
- `git diff --check origin/main..HEAD`: passed.

Full `pytest -q` collected 520 tests but exceeded a 3-minute local cap before
returning useful output. Treat the bounded checks above as the verified local
evidence for this review.

## Review Fixes Included

The integrated head includes the three review fixes:

1. Shadow/live PM idempotency collision fixed:
   - Shadow keys use a `shadow\x00...` namespace.
   - Live `admit_pending()` excludes `per_miner_shadow` rows as defense in depth.
   - Tests cover shadow -> drain -> live cutover -> same payload retry -> fresh
     live `sub_` receipt -> ranked -> no double-pay.

2. Queue metrics split live from shadow:
   - Live accepted/rejected rates exclude shadow rows.
   - Shadow accepted/rejected counters are reported separately.

3. Board worker catch-all risk documented:
   - Worker remains default-deny.
   - `worker.js`, `wrangler.toml`, and `README.md` warn not to attach it as a
     catch-all route.
   - Routes remain commented by default.

## Go-Live Gates

Before any production impact:

1. Validate async submit SQL against real Postgres, especially `FOR UPDATE SKIP
   LOCKED`, `ON CONFLICT DO NOTHING`, and idempotent terminal updates.
2. Deploy a worker-role service and prove pending PM receipts drain to terminal
   ranked/rejected before enabling PM async live.
3. Run PM async in shadow first and compare divergence counters.
4. Do not attach board-failover as a catch-all Cloudflare route.
5. Keep all three validator weight URLs working:
   - `https://api.cathedral.computer/v1/validator/weights/next`
   - `https://api.cathedral.computer/api/cathedral/v1/validator/weights/next`
   - `https://read.cathedral.computer/v1/validator/weights/next`
6. Run `scripts/validator_release_gate.py` against live finney before any
   validator-facing cutover.

## Rollback

- Async submit rollback: set `CATHEDRAL_SUBMIT_ASYNC_ENABLED=false` and
  `CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED=false`.
- PM shadow rollback: set `CATHEDRAL_PM_ASYNC_SHADOW=false`.
- Materialized snapshots rollback:
  `CATHEDRAL_MATERIALIZED_SNAPSHOT_ENABLED=false`.
- Per-hotkey limiter rollback:
  `CATHEDRAL_PER_HOTKEY_LIMIT_ENABLED=false`.
- Board/weights worker rollback: remove or revert the specific Cloudflare route
  bindings; do not alter unrelated routes.

## Current Recommendation

Open a PR from `reliability-integrated` to `main` for GitHub checks and review.
Do not merge or flip live flags until the go-live gates above pass.
