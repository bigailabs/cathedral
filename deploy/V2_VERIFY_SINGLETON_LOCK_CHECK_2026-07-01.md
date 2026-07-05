# V2 Verify Singleton Lock Check

Date: 2026-07-01  
Branch: `feat/solution-manifest-v2`  
Status: code-level diagnosis complete; live Railway replica/log check blocked by unauthenticated CLI

## Scope

Check the reported V2 verifier symptom:

```text
[v2_verify] singleton_lock_held_elsewhere
```

and determine whether it indicates a stuck verifier, multiple worker processes, or expected singleton contention.

## Key Findings

### 1. The singleton lock is held around an infinite loop

Code path:

```text
scaffold/publisher/app.py
_start_v2_verify_worker()
_run_v2_singleton_background("v2_verify", "cathedral:v2:verify", _loop)
```

The wrapper does:

```text
with v2_store.advisory_lock("cathedral:v2:verify") as acquired:
    if acquired:
        await _loop()
```

`_loop()` is an infinite loop that repeatedly calls:

```text
v2_pipeline.process_batch(...)
```

Therefore, one process/replica can hold the Postgres advisory lock indefinitely by design.

Any sibling process/replica with the worker enabled will repeatedly fail to acquire the lock and print:

```text
[v2_verify] singleton_lock_held_elsewhere
```

This log line alone does **not** prove the verifier is stuck. It proves more than one process is attempting to run the singleton loop.

### 2. The current log is too noisy to distinguish healthy singleton from stuck holder

Before the patch, non-holder workers printed `singleton_lock_held_elsewhere` every retry interval.

This creates log spam while not answering:

- which process owns the lock
- whether the owner is processing batches
- current pending count
- oldest pending age
- recent verify rate
- recent rejection rate

### 3. Current verifier only claims `solution_manifests`

In `v2_pipeline.claim_batch()`, the worker claims:

```sql
SELECT * FROM solution_manifests
WHERE status IN ('received', 'retry')
```

It does **not** claim `v2_submit_events` today.

This is important for W2: a future lean-ingress flusher that writes bitset rows into `v2_submit_events` cannot rely on the current verifier unless W1/W2 expands verifier support for `v2_submit_events` pending rows or writes to a verifier-consumed staging/table path.

Current main-app bitset submits avoid this because they verify inline and write `status='verified'` directly.

### 4. Live Railway check was blocked

Attempted:

```text
railway status
```

Result:

```text
Unauthorized. Please run railway login again.
```

Windows-side `railway` command was not installed.

Public endpoint check:

```text
GET https://v2-beta.cathedral.computer/v2/verify/metrics
```

Result before deploy:

```text
404 Not Found
```

So live replica/worker count still needs an authenticated Railway check or deployment of the metrics endpoint below.

## Patch Added

### 1. `/v2/verify/metrics`

New endpoint:

```text
GET /v2/verify/metrics
```

Returns:

```json
{
  "schema": "cathedral.v2.verify_metrics.v1",
  "enabled": true,
  "service_role": "all|worker|...",
  "worker_id": "...",
  "lock_held_by_self": true,
  "last_lock_acquired_at": "...",
  "last_lock_contended_at": "...",
  "last_batch_at": "...",
  "last_batch_ms": 0,
  "last_batch_count": 0,
  "verified_last_60s": 0,
  "rejected_last_60s": 0,
  "processed_last_60s": 0,
  "verify_rate_per_sec": 0,
  "tick_errors_last_60s": 0,
  "pending_count": 0,
  "oldest_pending_at": null,
  "oldest_pending_age_secs": null,
  "by_source": {
    "manifest": {"pending_count": 0, "oldest_pending_at": null},
    "bitset": {"pending_count": 0, "oldest_pending_at": null}
  }
}
```

Notes:

- `manifest` pending rows are currently verifier-consumed.
- `bitset` pending rows are exposed for visibility, but current `process_batch()` does not consume them yet.

### 2. Structured batch logs

New batch log shape:

```text
[v2_verify] batch n_verified=<n> n_rejected=<n> n_total=<n> batch_ms=<ms>
```

### 3. Throttled singleton contention logs

New env:

```text
CATHEDRAL_V2_SINGLETON_CONTENDED_LOG_SECS=300
```

Non-holder workers still update metrics, but log contention at most once per interval instead of every retry loop.

## Tests

Relevant suite:

```bash
PYTHONPATH=. pytest -q \
  scaffold/publisher/tests/test_v2_lean_ingress.py \
  scaffold/publisher/tests/test_v2_bitset_ingress_contract.py \
  scaffold/publisher/tests/test_solution_manifest_v2.py
```

Result:

```text
30 passed
```

New test:

```text
test_v2_verify_metrics_endpoint_reports_pending
```

## Interpretation of Current Symptom

Most likely explanation:

```text
v2-beta has more than one web worker or replica with CATHEDRAL_V2_VERIFY_WORKER_ENABLED=true.
One process holds the advisory lock forever; the others print singleton_lock_held_elsewhere.
```

That is expected with the current code.

The unresolved question is whether the lock holder is healthy and processing, or idle/stuck.

The new `/v2/verify/metrics` endpoint answers that after deploy:

- if one instance shows `lock_held_by_self=true` and nonzero processing when pending exists, the singleton is healthy and logs were noise
- if all instances show no lock holder or pending grows with zero rate, the worker is stuck/not running
- if `bitset.pending_count` grows but `manifest.pending_count` does not, W2 must add bitset verifier consumption before relying on lean-ingress flusher

## Next Actions

1. Deploy the metrics/log-throttle patch to `cathedral-v2-beta`.
2. Check:

```text
GET https://v2-beta.cathedral.computer/v2/verify/metrics
```

3. Authenticate Railway and confirm:

```text
replica count
WEB_CONCURRENCY
CATHEDRAL_SERVICE_ROLE
CATHEDRAL_V2_VERIFY_WORKER_ENABLED
CATHEDRAL_V2_VERIFY_BATCH_SIZE
CATHEDRAL_V2_VERIFY_INTERVAL_SECS
CATHEDRAL_V2_VERIFY_LOCK_SECS
```

4. If multiple web workers/replicas are enabled, either:
   - move V2 verifier to a single dedicated worker service, or
   - keep one web worker with verifier enabled and disable verifier on the others.

5. Before W2, decide whether lean-ingress flusher writes:
   - `solution_manifests` in a verifier-compatible shape, or
   - `v2_submit_events` plus expanded `v2_pipeline.claim_batch()` support.

## Bottom Line

The singleton log spam is mostly explained by code structure: one lock holder runs forever and every other worker logs contention.

The real issue is observability: before this patch, there was no endpoint proving the lock holder was actually draining work.

The patch adds the missing W1 visibility and throttles the misleading log spam.
