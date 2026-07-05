# Review Packet — V2 Lean Ingress Deep Review

Date: 2026-07-01  
Branch: `feat/solution-manifest-v2`  
Scope: post-0d443fc feedback updates, public replay-spam readiness, future flusher gates

## Files To Review

Primary:

```text
scaffold/publisher/v2_lean_ingress.py
scaffold/publisher/tests/test_v2_lean_ingress.py
scripts/v2_bitset_miner_e2e.py
scripts/v2_lean_ingress_e2e.py
```

Docs/context:

```text
deploy/V2_LEAN_INGRESS_SPAM_TEST_RUNBOOK_2026-06-30.md
deploy/V2_LEAN_INGRESS_DEEP_REVIEW_AND_BUG_HISTORY_2026-07-01.md
deploy/V2_BITSET_INGRESS_CONTRACT_2026-06-30.md
deploy/golden/v2_bitset_ingress_golden.json
```

## Latest Feedback-Driven Changes

### 1. Exact signed replay fast path

Previously, an expired submit token blocked idempotent replay.

Now the endpoint does:

```text
body cap
JSON parse
normalize
submitted_at skew check
hotkey signature verification
lookup existing non-rejected row by:
  miner_hotkey + challenge_id + submit_token_id
if found: return existing receipt as idempotent replay
else: verify submit token HMAC/expiry and admit new row
```

Please review whether this is safe.

Important boundaries:

- no new row is admitted without unexpired valid submit token
- replay requires valid hotkey signature
- replay must match existing submit token hash
- replay only returns non-rejected existing row

### 2. Re-admission after verifier rejection

Reviewer found F1 risk:

```text
shape-valid wrong bitset could lock a miner out of retrying the challenge
```

Now:

```text
existing non-rejected row => replay
existing rejected row => allow token/signature-valid replacement and set status back to received
```

Please review whether in-place replacement is acceptable, or whether retry should create a new receipt with parent linkage.

### 3. Unflushed index

Added:

```sql
CREATE INDEX IF NOT EXISTS idx_submit_events_local_unflushed
  ON submit_events_local(flushed_at_iso, received_at_iso);
```

Review whether this is enough for Phase 1 metrics/pressure/flusher queries.

### 4. Public spam-test boundary

Runbook now says:

- replay-spam public test is okay
- unrestricted unique-row spam is not okay yet
- registration/per-hotkey quota required before real unique-row traffic

Please review whether the boundary is clear enough for miners/operators.

## Tests Run

```bash
PYTHONPATH=. pytest -q \
  scaffold/publisher/tests/test_v2_lean_ingress.py \
  scaffold/publisher/tests/test_v2_bitset_ingress_contract.py \
  scaffold/publisher/tests/test_solution_manifest_v2.py
```

Result:

```text
25 passed
```

Local HTTP E2E:

```text
E2E_OK
status=received
idempotent_replay=True
unflushed_events=1
```

## Specific Review Questions

### Security/protocol

1. Is exact signed replay before full token-HMAC verification acceptable with the `submit_token_id` equality constraint?
2. Should replay also require `assignment_sha256` equality, or is `(hotkey, challenge, token_id)` enough?
3. Is returning an existing receipt for the same hotkey/challenge/token after token expiry acceptable?
4. Is `status='rejected'` in-place replacement safe, or should it create a new row/receipt?
5. Does the signature-before-replay lookup create any meaningful DoS exposure?

### Ops/cost

6. Is SQLite WAL + one uvicorn worker acceptable for the scoped public replay-spam test?
7. Are the default caps reasonable?

```text
MAX_UNFLUSHED_EVENTS=100000
MAX_STORAGE_BYTES=1000000000
MIN_FREE_DISK_BYTES=100000000
MAX_UNFLUSHED_AGE_SECS=0 for no-flusher Phase 1
```

8. What minimum metrics/alerts are required before exposing `v2-ingress-test.cathedral.computer`?
9. Should `/v2/ingress/metrics` be public, token-gated, or IP-restricted?

### Future flusher/verifier

10. Should the flusher write directly to `v2_submit_events` or to a new staging table?
11. Should rejected-row retry retain the same receipt ID or issue a new receipt ID linked to the rejected one?
12. Should verification consume local WAL directly or only Postgres-flushed rows?
13. What state machine should be final?

Suggested:

```text
received -> flushed -> verifying -> verified|rejected
rejected -> received on retry
```

### Abuse controls

14. Before unique-row traffic, should quota be per registered hotkey only, per IP, or both?
15. Should unregistered hotkeys be allowed to replay-spam but not unique-submit?
16. Should token minting on the challenge endpoint enforce the same quota as submit admission?

## Requested Verdict

Please give one of:

```text
APPROVE for isolated replay-spam public test
APPROVE WITH FIXES before public test
BLOCK
```

Also separately state whether the design is acceptable for the next milestone:

```text
F1: batch flusher/verifier implementation
```

## Addendum — Changes After Deep Review Blockers

The public-exposure blockers from the deep review have been addressed as follows:

### H1

Pre-auth rejects no longer write SQLite by default. Reject counts are held in memory and merged into metrics.

Please review:

```text
reject() in scaffold/publisher/v2_lean_ingress.py
```

### H2

Metrics are cached and `/v2/ingress/metrics` can be token-gated.

New env:

```text
CATHEDRAL_V2_INGRESS_METRICS_TOKEN
CATHEDRAL_V2_INGRESS_METRICS_TTL_SECS
```

Please review whether `/health/ready` using cached metrics is acceptable.

### H3

Single worker/process is now enforced two ways:

```text
WEB_CONCURRENCY/UVICORN_WORKERS/GUNICORN_WORKERS > 1 => boot failure
SQLite DB process lock sidecar file
```

Please review whether this is sufficient for a small public test host.

### Per-IP limiter

New env:

```text
CATHEDRAL_V2_INGRESS_IP_RPM
```

Please review the fixed-window limiter and whether it should live at Cloudflare instead, or both.

### F-MINT test gate

New default-off token mint allowlist on the V2 beta challenge/token service:

```text
CATHEDRAL_V2_SUBMIT_TOKEN_ALLOWLIST=<comma-separated hotkeys>
```

When set, non-allowlisted hotkeys cannot receive V2 bitset submit tokens from `/v2/synthetic-boolean/per-miner/challenges` or `/v2/synthetic-boolean/per-miner/cnf`.

Please review whether this is sufficient for the public replay-spam test, with the understanding that it is not the final registration/stake gate.

### Updated tests

```text
29 passed
```

New tests include:

```text
metrics token gate
memory-only reject accounting
IP limiter
multi-worker env fail-closed
```

## Updated Requested Verdict

Please give separate verdicts for:

1. closed replay-spam test
2. public allowlisted replay-spam test
3. unrestricted unique-row public test
4. F1 flusher/verifier milestone
