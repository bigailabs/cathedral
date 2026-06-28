# Cathedral Reliability Merge Packet

**Date:** 2026-06-28
**Worktree:** `C:\Users\fred\code\cathedral-review-reliability-9279629`
**Status:** merge-ready after local verification and adversarial Claude review.
**Production status:** not deployed by this document.

This packet records the reliability work completed across the six-agent review
loop. The goal was to harden Cathedral's SAT submit/read surfaces without
breaking legacy validator paths.

## Current Merge Scope

The merge scope is the reliability patch only:

- pressure telemetry for submit and PM-read failure attribution
- opt-in pre-gate abuse limiter
- deterministic PM challenge recovery that tolerates replica lag
- async submit queue states, metrics, worker heartbeat, and safe backpressure
- fast prebuilt dashboard state with public telemetry sanitized
- validator/read/submit compatibility protections
- miner error contract and deploy env documentation

Intentionally excluded from the merge:

- `logs/`
- `scripts/cathedral_endpoint_monitor.ps1`

Those were local monitoring artifacts, not product code.

## Six-Agent Completion

| Agent | Area | Result |
| --- | --- | --- |
| Kuhn | Pressure telemetry | Complete. Added bounded 429/5xx pressure attribution without raw IPs, signatures, query strings, full UAs, or raw hotkeys. |
| Bacon | Abuse limiter | Complete. Added default-off pre-gate IP and IP-scoped claimed-hotkey limiter for submit and PM-read hot paths. |
| Kepler | PM assignment/CNF stability | Complete. Added deterministic hotkey-bound `pm-*` recovery so replica-lagged submits do not false-reject. |
| Aristotle | Async submit/queue health | Complete. Added explicit receipt states, queue metrics, worker heartbeat, and default-off safe backpressure. |
| Aquinas | Dashboard/read snapshot | Complete. Added `/v1/dashboard/state` backed only by prebuilt snapshot data; public response strips operational pressure/queue timing. |
| Gauss | Validator compatibility | Complete. Verified `weights/next` compatibility and found/fixed `/leaderboard/recent` cold async risk for old validator-style clients. |

## Adversarial Review

Claude reviewed the actual worktree and relevant untracked files.

Initial verdict:

```text
MERGE READY
```

Claude identified rollout cautions before enabling:

- public dashboard exposure of endpoint pressure and queue lag
- spoofable actor-level abuse limiting
- leftmost `X-Forwarded-For` IP precedence
- queue backpressure shedding on stalled workers and shadow rows
- missing env documentation
- stale middleware-order comment

Fixes applied after review:

- public `/v1/dashboard/state` now returns `admin_only` for `endpoint_pressure`
  and `queue_lag`
- rich pressure and queue telemetry remain on auth-gated admin submit metrics
- abuse actor bucket is now scoped by `IP + claimed hotkey`
- client IP precedence is `CF-Connecting-IP`, then `X-Real-IP`, then
  `X-Forwarded-For`
- queue backpressure ignores `per_miner_shadow` rows
- queue backpressure only sheds new work when an active worker heartbeat exists
- admin health and admin submit metrics are exempt from coarse RPM limiting but
  still token-gated
- deploy env example documents new reliability flags
- middleware-order comment fixed
- regression tests added

Final Claude delta review:

```text
MERGE READY
```

No blockers remained.

## Verification Evidence

Commands passed in this worktree:

```text
PYTHONPATH=$PWD python3 -m pytest -q scaffold/publisher/tests
```

Result:

```text
165 passed
```

```text
PYTHONPATH=$PWD python3 publisher_verify.py
```

Result:

```text
PUBLISHER VERIFY: PASS all 152 checks
```

```text
PYTHONPATH=$PWD python3 weights_verify.py
```

Result:

```text
OK: 32 checks
```

```text
python3 -m compileall -q scaffold/publisher
git diff --check
```

Result:

```text
pass
```

`git diff --check` only emitted CRLF warnings.

## Compatibility Gates

The following outward surfaces must keep working:

- `GET /v1/validator/weights/next`
- `GET /api/cathedral/v1/validator/weights/next`
- `GET /v1/leaderboard/recent`
- `POST /v1/agents/submit`
- `POST /api/cathedral/v1/agents/submit`
- `GET /v1/synthetic-boolean/per-miner/challenges`
- `GET /v1/synthetic-boolean/per-miner/cnf`

Regression coverage now includes:

- legacy prefixed submit reaches the submit handler
- cold `/leaderboard/recent` returns rows synchronously by default
- PM CNF fetch recovers without assignment-row visibility
- foreign miner PM challenge IDs are rejected
- queue backpressure does not block idempotent receipt replay
- queue backpressure does not shed without active workers
- shadow queue rows do not trigger live backpressure
- public dashboard state does not expose pressure/queue timing
- abuse actor limiter cannot cross-IP lock out a victim hotkey
- Cloudflare client IP takes precedence over spoofable XFF

## Go-Live Notes

All new behavior that changes runtime pressure handling is gated.

Recommended production order:

1. Deploy code with behavior-changing flags off.
2. Confirm `weights/next`, `/leaderboard/recent`, and submit aliases are healthy.
3. Enable pressure telemetry if not already enabled.
4. Enable dashboard snapshot only after confirming public sanitized output.
5. Enable abuse limiting conservatively.
6. Enable async submit only with a live worker role and queue metrics visible.
7. Enable queue backpressure only after worker heartbeat is confirmed.

Rollback flags:

```text
CATHEDRAL_ABUSE_LIMIT_ENABLED=false
CATHEDRAL_DASHBOARD_SNAPSHOT_ENABLED=false
CATHEDRAL_SUBMIT_ASYNC_ENABLED=false
CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED=false
CATHEDRAL_SUBMIT_QUEUE_BACKPRESSURE_ENABLED=false
```

## Conclusion

The six-agent reliability work is complete, the review is coherent, the known
review findings were patched, and the current code is merge-ready pending normal
GitHub checks.
