# Cathedral SAT Reliability Upgrade Plan

**Status:** Draft for action
**Owner:** Fred
**Last verified against code and live probes:** 2026-06-27
**Canonical file:** `deploy/RELIABILITY_UPGRADE_PLAN.md` (single source of truth)
**Tracking issue:** cathedralai/cathedral#309

This plan merges the live deploy runbook and the longer-term submit architecture
plan into one source of truth. It supersedes and replaces the earlier short
deploy-only draft; do not keep a second reliability plan in the repo.

## Scope

In scope:

- Live SAT lane reliability for `api.cathedral.computer`, `read.cathedral.computer`, and `submit.cathedral.computer`.
- Miner challenge fetch, CNF fetch, submit admission, receipts, verification, and status surfaces.
- Deploy configuration, Railway service health, Cloudflare edge behavior, and operator observability.
- Compatibility for existing public API paths used by miners and validators.

Out of scope for this plan:

- Bittensor mainnet governance changes.
- SN39 validator hotkey or UID200 runtime changes.
- Compute/Agent lanes outside the SAT service path.
- Decentralization and protocol-hardening work beyond the reliability changes listed here.

## Executive Summary

The reliability work has two layers:

1. **Acute live recovery**
   - Restore the read origin first.
   - Raise the submit cap from the split-deploy choke.
   - Make the bad deploy config impossible to reintroduce silently.
   - Add enough observability to know when the system is sick.

2. **Durable submit architecture**
   - Change submit from synchronous verification to durable admission.
   - Return `202 Accepted` with a stable receipt.
   - Verify SAT solutions asynchronously in workers.
   - Keep miner retries idempotent and fair.

These are not competing plans. The acute plan stops the bleeding; the durable
architecture removes the class of failure where miners lose work because the
request path was busy.

There is also a third layer, and it outranks both: **protect weight setting**.
Submit/read failures hurt miners; a weight-feed failure hurts the chain. That
section is first below because it is the highest-priority surface for mainnet.

## P0 (highest priority): Protect Weight Setting

> Added after review. The miner-facing submit/read work above is necessary but
> **not sufficient for a stable mainnet release.** The single most important
> surface is the validator weight feed. If it fails, validators cannot set
> weights - worse than any dashboard, leaderboard, or even submit outage.

### The canonical surface

```text
GET https://api.cathedral.computer/v1/validator/weights/next
```

Returns the signed final-scores + burn vector validators consume to set weights
on chain. **This is the payment source of truth and must be the most protected
route in the system.**

### Why it is currently at risk (verified in code)

- `validator_weights_next` (`scaffold/publisher/app.py:1955`) is `async` and reads
  the **cached** signed vector via `weights_mod.current_vector(...)`
  (`scaffold/publisher/weights.py:1244`). That cache (`_vector_cache`) is rebuilt
  by a **background refresh thread every `_CACHE_TTL_SECS = 60s`**
  (`weights.py:1208`). So steady-state reads return in microseconds and the route
  is *partially* shielded: it never uses the thread pool and survives submit/read
  saturation **within a live process**.
- **But the cache is in-memory and not persisted.** On a cold process
  (restart / redeploy / OOM), the cache is empty and the first call must build
  synchronously from the DB (a 5-30s build); if that build fails the handler
  **raises `503 "no vector available"`**. The route also sits behind the **same
  read origin observed returning `504` for ~10s** (see "Read Origin Outage").
- Net: today the weight feed is only as available as the live app process, and a
  restart or origin failure leaves validators with `503`/`504` and **no durable
  last-known-good to fall back to.** That is not enough for mainnet.

### Required protections (the gap this plan must close)

1. **Tier read recovery by criticality** (do not treat "reads" as one blob):
   | Tier | Surface | Requirement |
   |---|---|---|
   | **0 - critical** | `/v1/validator/weights/next` | must *always* answer, even when the app/DB is down - serve last-known-good |
   | **1 - important** | active-board / current-challenge / CNF | should work; degrade to edge cache/stale |
   | **2 - non-critical** | leaderboard, recent feed, dashboard | best-effort; may fail without paging |

2. **Durable last-known-good signed vector (P0 for mainnet).**
   A durable last-known-good signed vector is **P0 for mainnet**, not a nice-to-have.
   On every successful vector refresh, mirror the *signed* vector to a store that
   is **independent of the live read app**: Cloudflare KV / R2 / Durable Object, or
   a static object-store/Railway artifact. If the origin fails, the edge serves the
   last good vector with an explicit marker:
   ```json
   { "...signed vector...": "...", "source": "stale_fallback", "generated_at": "<iso>", "age_seconds": 123 }
   ```
   The vector is already signed, so a stale copy is still cryptographically
   verifiable - validators (and the on-chain consumer) can decide whether to accept
   by age. Never serve an *unsigned* or re-signed-stale vector; serve the original
   signed bytes verbatim.

3. **Validator-specific release gates (must pass before/with any mainnet deploy):**
   - weights endpoint: **0x 5xx across 3 consecutive tempos**
   - signed-vector **age <= 5 min** (see Vector Freshness Thresholds below)
   - **UID200 update age <= 10 min** (our own validator is refreshing)
   - **major validators refreshing** (not stuck on a stale vector)
   - **burn snapshot matches intended policy** (no accidental burn/emission shift)

4. **Keep the feed independent of submit/read load.** It already avoids the thread
   pool; additionally ensure the background refresh thread and its DB query have
   their own pool budget so submit/read saturation can never starve the refresh.

### Vector Freshness Thresholds (concrete)

The background thread rebuilds the cache every **60s** (`_CACHE_TTL_SECS`). Thresholds
are set off that cadence and the chain tempo. **Assumption: SN39 tempo ~= 360 blocks x
12s = ~72 min** (confirm against the live chain tempo; if it differs, scale the "hard
stale ceiling" to one tempo).

| Signal | Healthy | Warn (alert) | Critical (page) |
|---|---|---|---|
| Signed-vector age (now - `generated_at`) | <= 2 min | > 5 min | > 10 min |
| Hard stale ceiling (validators should distrust) | n/a | n/a | > 1 tempo (~72 min) |
| `stale_fallback` being served | never (steady state) | any stale serve | stale age > 1 tempo |
| UID200 update age | <= 5 min | > 10 min | > 20 min |

- Healthy `<= 2 min` allows one missed 60s refresh cycle without alarm.
- Page the moment **any** `stale_fallback` is served (it means the origin is down),
  even while the stale vector is still inside the acceptable age ceiling.

Operational ordering consequence: in Phase 0, **restore the weight feed first**,
then active board, then everything else (the generic "restore read origin" step is
split accordingly below).

### Validator URL Compatibility (all three must keep working)

Validators in the wild fetch the weight feed from more than one URL. **No validator
should ever need a URL change or a binary/config change to keep setting weights.**
All of the following must stay live and return the same signed-vector semantics and
a compatible response shape:

| URL | Role | How it routes (verified in code) |
|---|---|---|
| `https://api.cathedral.computer/v1/validator/weights/next` | **Canonical** | primary public route -> `validator_weights_next` (`app.py:1955`) |
| `https://api.cathedral.computer/api/cathedral/v1/validator/weights/next` | **Legacy-prefixed alias** | ASGI middleware strips `_LEGACY_PREFIX = "/api/cathedral"` (`app.py:572-588`) so it reaches the same handler |
| `https://read.cathedral.computer/v1/validator/weights/next` | **Direct read-service URL** | read-service domain (`read.cathedral.computer`) hitting the same app/handler |

Policy:

- `api.cathedral.computer/v1/validator/weights/next` is **canonical**.
- `/api/cathedral/...` is the **legacy-prefixed alias** and must remain routed to the
  same handler (do not drop the prefix-strip middleware).
- `read.cathedral.computer/...` is the **direct read-service URL** and must remain a
  valid path to the same signed vector.
- **Validators must not need a URL or binary change.** Backward compatibility for all
  three is a hard requirement, not best-effort.
- **All three return the same signed-vector semantics and a compatible response shape.**
  The signed bytes must be identical regardless of which URL served them.
- **All three are covered by smoke tests, monitors, and the validator release gate.**
  A monitor/probe and a freshness check run against each URL, not just the canonical one.
- **Edge durability applies to both `api.cathedral.computer` routes:** if the read
  origin is unhealthy, `api.cathedral.computer/v1/validator/weights/next` **and**
  `api.cathedral.computer/api/cathedral/v1/validator/weights/next` must still serve the
  durable last-known-good signed vector (`source: stale_fallback`) from the edge.
  (The `read.cathedral.computer` direct URL may legitimately depend on read-service
  health; the canonical `api.` host is the one that must survive an origin outage.)

Compatibility test matrix (smoke + monitor + release gate):

| Check | api canonical | api legacy-prefixed | read-service direct |
|---|---|---|---|
| Returns 200 + signed vector | required | required | required |
| Same signed bytes for same tempo | required | required | required |
| Freshness within thresholds | required | required | required |
| Serves `stale_fallback` when origin down | required | required | not required |

## Confirmed Problems

### P0: Read Origin Outage

At the time this plan was written, safe live GET probes showed the read origin
timing out behind the edge:

| Path | Observed result |
| --- | --- |
| `GET /health/ready` | `504` after about 9s |
| `GET /v1/synthetic-boolean/current-challenge` | `504` after about 10s |
| `GET /v1/synthetic-boolean/active-challenges` | `504` after about 10s |

This means miners may fail before submit, because they cannot reliably fetch
fresh challenge state. Raising submit capacity does not help if challenge reads
are down.

### P0: Submit Concurrency Choked By Deploy Config

The submit app computes effective concurrency as:

```text
min(CATHEDRAL_SUBMIT_MAX_CONCURRENCY, CATHEDRAL_SUBMIT_HARD_CAP)
```

Code defaults are safer than the split deploy:

- `CATHEDRAL_SUBMIT_MAX_CONCURRENCY=24`
- `CATHEDRAL_SUBMIT_HARD_CAP=8`

The split deploy config currently documents or sets:

- `CATHEDRAL_SUBMIT_HARD_CAP=1`
- `WEB_CONCURRENCY=1`

With a hard cap of `1`, two overlapping submits cause one miner to receive:

```text
HTTP 429 submit_busy_retry
```

That is expected backpressure behavior, but the configured value serializes the
whole submit lane.

### P1: Submit Does Heavy Work Inline

Today, `POST /v1/agents/submit` does too much synchronously:

```text
Miner -> Submit API
          - parse form body
          - verify hotkey signature
          - load CNF
          - verify DIMACS solution
          - write DB rows
          - update claim/rank
          - emit signed feed rows
          - return ranked/rejected response
```

While verification runs, the request holds an in-process submit slot. Under
traffic spikes, the gate protects the origin but miners can lose a final POST
after spending real solve time.

## Reliability Principles

- Restore reads before tuning submits.
- Keep public API paths backward compatible.
- Make deploy notes executable in code, config, or smoke tests.
- Keep request admission cheap, durable, and idempotent.
- Do not remove backpressure; right-size it and make it miner-friendly.
- Rank accepted work by server `received_at`, not worker completion time.
- Prefer boring Postgres-backed durability first; add managed queues only when pressure requires it.
- Do not store raw rejected solution bodies forever.

## Target Architecture

```text
Miner
  |
  | POST /v1/agents/submit
  v
Submit Admission API
  | cheap checks
  | durable receipt
  | solution body to DB or object storage
  v
Durable Submit Queue / Log
  |
  | workers claim pending attempts
  v
Verification Workers
  | load CNF and solution
  | verify DIMACS
  | write ranked/rejected result
  v
Receipts / Score Rows / Signed Feeds
```

The first durable milestone is:

```text
POST /v1/agents/submit returns 202 pending in under 1 second, backed by a durable receipt.
```

## Phase 0: Immediate Live Triage

Goal: restore the live miner path without changing scoring semantics.

### 0a-0. Restore the validator weight feed FIRST (Tier 0)

Before the miner-facing read paths, confirm the chain's payment surface is alive:

- Probe `GET /v1/validator/weights/next` - it must return `200` with a fresh,
  signed vector (not `503 no vector available`, not `504`).
- Check the signed-vector `generated_at`/age is <= 2 min (healthy; see Vector Freshness Thresholds).
- Confirm UID200 (our validator) is consuming a fresh vector.
- If it is `503`/`504`: this is the top incident. Restoring it may share a cause
  with the read origin below, but it is verified/closed on its own before moving on.
- If the durable last-known-good fallback (P0 section 2) is not yet built, **build/enable
  it as part of this triage** - it is the difference between "origin blip" and
  "validators can't set weights".

### 0a. Restore Read Origin (Tier 1 board reads)

Do this second (after the weight feed).

Actions:

- Inspect the Railway read service.
- Check whether it is crashed, OOM-killed, health-failing, undeployed, or routed to the wrong domain.
- Confirm `read.cathedral.computer` is attached to the read service.
- Confirm the edge worker `READ_ORIGIN` points at the read service.
- Restart or redeploy the read service after logs are captured.
- Re-probe until these return `200`:
  - `GET /health/ready`
  - `GET /v1/synthetic-boolean/current-challenge`
  - `GET /v1/synthetic-boolean/active-challenges`

### 0b. Raise Submit Capacity

Start with:

```text
CATHEDRAL_SUBMIT_HARD_CAP=8
CATHEDRAL_SUBMIT_MAX_CONCURRENCY=24
WEB_CONCURRENCY=2
CATHEDRAL_PM_READ_HARD_CAP=128
CATHEDRAL_THREADPOOL_TOKENS=32
CATHEDRAL_PG_POOL_MAX=32
```

Then verify:

```text
GET /v1/admin/synthetic-boolean/submit-metrics
```

Expected:

- `hard_cap` is `8`
- `submit_busy_retry` trends down
- DB and CPU stay inside headroom

Only test `CATHEDRAL_SUBMIT_HARD_CAP=16` after the service is stable at `8`.

### 0c. Miner Guidance During Triage

Publish the short-term contract:

- `429 submit_busy_retry` means submit saturation.
- Honor `Retry-After`.
- Retry with jitter and a fresh signature.
- `504 *_origin_unavailable` is server-side, not a miner solution bug.

Done when:

- Read health is stable.
- Challenge reads work through public URLs.
- Submit metrics show the intended cap.
- Miners are no longer mostly blocked by `429`.

## Phase 1: Make Bad Config Unrepeatable

Goal: prevent the split deploy from silently reintroducing the cap-1 choke.

Actions:

- Edit `deploy/railway-split.ps1`:
  - set submit `CATHEDRAL_SUBMIT_HARD_CAP=8`
  - set `CATHEDRAL_SUBMIT_MAX_CONCURRENCY=24`
  - set `WEB_CONCURRENCY=2`
  - set `CATHEDRAL_PM_READ_HARD_CAP=128`
  - set `CATHEDRAL_THREADPOOL_TOKENS=32`
  - set `CATHEDRAL_PG_POOL_MAX=32`
- Edit `deploy/ROLE_SPLIT_RUNBOOK.md` to match.
- Add or keep startup checks for:
  - valid `CATHEDRAL_SERVICE_ROLE`
  - shared `CATHEDRAL_CNF_TOKEN_SECRET`
  - role-specific route exposure
- Land on a reviewed side branch.
- Do not merge to `main` without Fred's sign-off.

Done when:

- The repo and runbook agree on target config.
- Re-running deploy scripts cannot quietly reset submit to cap `1`.
- Submit metrics prove the live service is using the intended cap.

## Phase 2: Root-Cause Read Origin

Goal: fix the recurring unsolved read-origin failure, not just restart around it.

Investigate in order:

- Railway service state:
  - crash loop
  - OOM kill
  - failed deploy
  - bad domain binding
  - wrong service role
- Postgres pressure:
  - connection count versus pool sizing
  - slow queries
  - transaction locks
  - active-board path latency
- App hot paths:
  - recent-feed cursor scans
  - background cache builders
  - startup work that can block request handling
  - per-replica local state that should be shared
- Edge behavior:
  - stale-while-revalidate on safe read routes
  - origin timeout values
  - cache keys for active challenge shapes

Important distinction:

- Edge caching can hide a read-origin problem after one successful fill.
- Edge caching cannot be the root fix if `/health/ready` and `/health/live` do not answer.

Done when:

- Root cause is documented.
- Read service can be restarted/redeployed without long 504 windows.
- Hot read routes degrade to stale data where safe instead of hard 504.

## Phase 3: Better Backpressure

Goal: stop turning every small overlap into an instant miner-facing reject.

Actions:

- Keep a hard submit ceiling.
- Add a short bounded wait before returning `429`, for example `0.25s` to `0.5s`.
- Avoid double-counting one submit across both ASGI and dependency gates if possible.
- Keep `Retry-After` on overload responses.
- Document client jitter/backoff.
- Add per-hotkey abuse limits separately from global saturation limits.

Normal pressure levels:

1. Normal: accept submit.
2. Queue high: accept submit and include estimated delay when receipts exist.
3. Queue critical: accept only one pending submit per hotkey/challenge.
4. Admission emergency: return `503 submit_admission_unavailable`.

Avoid `submit_busy_retry` as a normal outcome once durable admission exists.

## Phase 4: Durable Submit Admission

Goal: make submit cheap, durable, and retry-safe.

The submit endpoint should do only cheap work:

1. capture server `received_at` at handler entry
2. enforce max request/body size
3. parse required fields
4. verify hotkey signature
5. compute `dimacs_solution_sha256`
6. compute idempotency key
7. persist pending receipt
8. persist or upload solution body
9. enqueue verification work
10. return `202 Accepted`

The submit endpoint should not verify the SAT solution inline.

### Miner Submit Contract

`POST /v1/agents/submit` usually returns:

```http
202 Accepted
```

```json
{
  "schema": "cathedral.submit_receipt.v2",
  "status": "pending",
  "receipt_id": "sub_...",
  "challenge_id": "pm-t1-...",
  "miner_hotkey": "...",
  "received_at": "2026-06-27T...Z",
  "dimacs_solution_sha256": "...",
  "receipt_url": "/v1/agents/receipts/sub_..."
}
```

### Idempotency

Use:

```text
sha256(miner_hotkey + challenge_id + dimacs_solution_sha256)
```

If the same miner resubmits the same solution, return the existing
receipt/result.

### Fairness Timestamp

Capture:

```text
received_at = server time at beginning of submit handler
```

Store separately:

```text
received_at
verified_at
```

Rank/order by `received_at`, not worker completion time.

### Durable attempts table (reconcile with existing code - not greenfield)

**Verified in code:** the service already has a `/v1/agents/receipts/{receipt_id}`
endpoint (`scaffold/publisher/app.py:3819`) and a `per_miner_attempts` table that
`agents_submit` writes to inline. So this phase **extends what exists** rather than
standing up a parallel system:

- Reuse / evolve `per_miner_attempts` (and the existing receipts endpoint) instead
  of adding a second, divergent attempts table. If a rename to `submit_attempts`
  is preferred, migrate the existing table - do not run both.
- The fields below are the target shape to converge on; treat columns the current
  table already has as "keep", and the rest as "add".
- Goal: one attempts table is the source of truth for both admission and
  verification, surfaced through the one existing receipts endpoint.

Target schema sketch (superset to converge on):

```sql
CREATE TABLE submit_attempts (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE NOT NULL,
  miner_hotkey TEXT NOT NULL,
  challenge_id TEXT NOT NULL,
  challenge_kind TEXT NOT NULL,
  dimacs_solution_sha256 TEXT NOT NULL,
  solution_storage_kind TEXT NOT NULL,
  solution_storage_key TEXT,
  solution_bytes INTEGER,
  signature TEXT NOT NULL,
  submitted_at TEXT,
  received_at_iso TEXT NOT NULL,
  status TEXT NOT NULL,
  rejection_reason TEXT,
  solve_rank INTEGER,
  weighted_score REAL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at_iso TEXT,
  locked_by TEXT,
  locked_until_iso TEXT,
  created_at_iso TEXT NOT NULL,
  updated_at_iso TEXT NOT NULL
);
```

Important indexes:

```sql
CREATE INDEX submit_attempts_status_next_attempt_idx
  ON submit_attempts(status, next_attempt_at_iso);

CREATE INDEX submit_attempts_challenge_miner_idx
  ON submit_attempts(challenge_id, miner_hotkey);

CREATE INDEX submit_attempts_miner_created_idx
  ON submit_attempts(miner_hotkey, created_at_iso);

CREATE INDEX submit_attempts_received_idx
  ON submit_attempts(received_at_iso);
```

### Solution Body Storage

Short-term:

- Store `dimacs_solution` in Postgres for speed.

Preferred:

- Store solution body in object storage.
- Store only metadata and object key in Postgres.

Example key:

```text
r2://cathedral-submits/YYYY/MM/DD/{receipt_id}.dimacs
```

Done when:

- `/submit` returns `202 pending` quickly.
- retries return the same receipt/result.
- accepted receipts are durable once returned.
- SAT verification no longer runs inline in the request path.

## Phase 5: Async Verification Workers

Goal: scale verification horizontally without blocking submit admission.

Worker claim sketch:

```sql
UPDATE submit_attempts
SET status = 'verifying',
    locked_by = $worker_id,
    locked_until_iso = $deadline,
    attempt_count = attempt_count + 1,
    updated_at_iso = $now
WHERE id IN (
  SELECT id
  FROM submit_attempts
  WHERE status IN ('pending', 'failed_retryable')
    AND (next_attempt_at_iso IS NULL OR next_attempt_at_iso <= $now)
  ORDER BY received_at_iso, id
  LIMIT $batch_size
  FOR UPDATE SKIP LOCKED
)
RETURNING *;
```

Worker steps:

1. claim pending attempts
2. load solution body from object storage or DB
3. load CNF by `challenge_id`
4. run existing DIMACS verification
5. atomically record result:
   - `ranked`
   - `rejected`
6. preserve existing rejection vocabulary
7. emit signed feed rows if accepted
8. apply retention policy to raw solution body

Crash safety:

- row remains `verifying`
- `locked_until_iso` expires
- another worker can reclaim it

Duplicate safety:

- use DB constraints to prevent duplicate payouts
- preserve existing per-mode uniqueness semantics

Done when:

- multiple workers can run safely.
- worker crash does not lose submits.
- retries do not duplicate payouts.
- queue depth and verification latency are observable.

## Phase 6: Cost And Scale Hardening

Recommended retention:

| Data | Retention |
| --- | ---: |
| Pending solution body | until verified |
| Rejected raw solution | 1-24 hours |
| Accepted raw solution | 1-7 days |
| Accepted witness hash / answer hash | forever |
| Receipt metadata | 30-90 days |
| Aggregate scoring rows | forever or archived |

After verification, compact accepted solves to:

```text
answer_hash
verifier_details_hash
dimacs_solution_sha256
received_at
verified_at
rank
score
```

Scale order:

1. Postgres table as durable queue.
2. Date/epoch partitioning.
3. Retention job.
4. Object storage for raw bodies.
5. Managed queue only when Postgres queue pressure is proven.

Managed queue messages must be small:

```json
{
  "receipt_id": "...",
  "challenge_id": "...",
  "miner_hotkey": "...",
  "solution_object_key": "...",
  "dimacs_solution_sha256": "...",
  "received_at": "..."
}
```

Do not put full DIMACS solution bodies into queue messages.

## Phase 7: Observability, Alerts, And SLOs

Admission metrics:

- requests/sec
- accepted/sec
- rejected/sec by reason
- p50/p95/p99 admission latency
- body size distribution
- idempotent replay rate

Queue metrics:

- pending jobs
- oldest pending age
- jobs/sec enqueued
- jobs/sec verified
- retry count
- dead-letter count

Verification metrics:

- p50/p95/p99 verification time
- CNF load time
- solution parse time
- DB commit time
- accepted/rejected rates by reason
- per-tier verification cost

Read metrics:

- `/health/live`
- `/health/ready`
- challenge-fetch p50/p95/p99
- edge cache hit/stale/error rate
- origin timeout rate

Critical alerts (page immediately):

- **`/v1/validator/weights/next` returns any 5xx, or serves `stale_fallback` aged > 1 tempo (~72 min)** (highest severity - weight setting at risk)
- **signed-vector age > 10 min (page); > 5 min (warn)**
- **UID200 vector age exceeds threshold** (our validator stopped refreshing)
- `/health/ready` not `200` for more than 60s
- oldest pending age exceeds SLO
- admission returns emergency failures
- `submit_busy_retry` rate above threshold
- DB write latency spikes
- object storage writes fail
- worker success rate drops
- receipts stuck in `verifying` past lock timeout

Validator weight-feed metrics (Tier 0 - watch first):

- weights endpoint success rate and 5xx count per tempo
- signed-vector age (now - `generated_at`)
- `stale_fallback` serve rate (should be ~0 in steady state)
- UID200 update age; count of major validators refreshing
- burn snapshot vs intended policy

Suggested SLOs:

```text
Validator weight feed: 99.99% availability; 0x 5xx across any 3 consecutive tempos.
Signed-vector freshness: age <= 2 min healthy, page if > 10 min, hard ceiling 1 tempo (~72 min).
Last-known-good: weight feed answers even when app/DB is down (stale_fallback, signed).
Read availability: 99.5% or better.
Challenge fetch p95: under 2s.
Submit admission: 99.9% of valid submits receive a receipt within 1s.
Verification: 95% within 30s, 99% within 5m.
Durability: no accepted submit receipt is lost after 202.
Retry: same idempotency key returns same receipt/result.
```

### Validator release gate (must pass before/with any mainnet-affecting deploy)

```text
[ ] weights endpoint: 0x 5xx across 3 consecutive tempos
[ ] signed-vector age <= 5 min
[ ] UID200 update age <= 10 min
[ ] major validators refreshing (not stuck on a stale vector)
[ ] burn snapshot matches intended policy
[ ] last-known-good fallback verified (kill app, feed still serves signed stale vector)
[ ] all three validator URLs pass (canonical + legacy-prefixed + read-service direct): 200, same signed bytes, fresh; both api.* routes serve stale_fallback when origin down
```

## Miner-Facing Error Contract

| HTTP / reason | Meaning | Miner action |
| --- | --- | --- |
| `429 submit_busy_retry` | submit service saturated | retry with jitter/backoff, honor `Retry-After` |
| `503 submit_admission_unavailable` | durable admission unavailable | back off; server-side incident |
| `409 challenge_not_active` | stale or retired challenge | refetch active challenge and CNF |
| `409 challenge_already_locked` | locked or retired during race | refetch |
| `409 already_solved` | this hotkey already solved it | move to next challenge |
| `401 invalid hotkey signature` | signing payload mismatch | fix client signing |
| `400 solution_*` | invalid DIMACS or solver output | fix solution format |
| `404` on CNF URL | CNF token expired or invalid | refetch active CNF |
| `504 *_origin_unavailable` | origin or edge timeout | server-side; not a miner bug |

## Target Configuration Reference

| Variable | Submit now | Submit target | Read target | Notes |
| --- | --- | --- | --- | --- |
| `CATHEDRAL_SUBMIT_HARD_CAP` | `1` in split deploy | `8`, then `16` if headroom | n/a | current choke |
| `CATHEDRAL_SUBMIT_MAX_CONCURRENCY` | default `24` | `24` | n/a | effective cap uses min |
| `WEB_CONCURRENCY` | `1` | `2` | `2` | watch DB connections |
| `CATHEDRAL_PM_READ_HARD_CAP` | `1` in split deploy | `128` | `128` | per-miner read gate |
| `CATHEDRAL_THREADPOOL_TOKENS` | `16` | `32` | `32` | tune after measurements |
| `CATHEDRAL_PG_POOL_MAX` | `16` | `32` | `16` | keep total below DB max |
| `CATHEDRAL_CNF_TOKEN_SECRET` | shared | shared | shared | must match every replica |
| `CATHEDRAL_SERVICE_ROLE` | `submit` | `submit` | `read` | fail startup on invalid role |

Edge timeouts should be tuned only after origin is healthy.

## Verification And Rollback

Verify after Phase 0:

1. `GET /v1/validator/weights/next` returns `200` with a fresh signed vector (Tier 0 - check first).
2. Kill/restart the app and confirm the weight feed still serves the last-known-good signed vector (`source: stale_fallback`) aged within 1 tempo (~72 min).
3. `GET /health/ready` returns `200`.
4. `GET /v1/synthetic-boolean/current-challenge` returns `200`.
5. `GET /v1/synthetic-boolean/active-challenges` returns `200`.
6. `GET /v1/admin/synthetic-boolean/submit-metrics` shows `hard_cap: 8`.
7. `submit_busy_retry` trends down.
8. A real miner round completes submit without normal-load `429`.

Rollback:

- Phase 0 env changes are single-variable reverts plus redeploy.
- If Postgres saturates, step `HARD_CAP` down to `4` before reverting fully.
- If DB connections spike, reduce `PG_POOL_MAX` and/or `WEB_CONCURRENCY`.
- Read-origin restart is non-destructive, but capture logs before restarting.

## Implementation Order

### Now: Stop Bleeding

1. **Protect the validator weight feed (Tier 0): confirm it serves, add durable last-known-good signed-vector fallback, add the validator release gate.**
2. Restore read origin (board reads).
3. Raise submit cap to `8`.
4. Fix split deploy config on a side branch.
5. Wire submit + weight-feed metrics into operator dashboard.
6. Publish miner error contract.

### Next: Harden The Current Request Path

1. Root-cause read origin.
2. Add bounded wait before `429`.
3. Add request body-size limits if missing.
4. Capture `received_at` at handler entry.
5. Keep public read routes snapshot/cache-first where safe.

### Then: Durable Receipts

1. Add `submit_attempts`.
2. Add idempotency key.
3. Return `202 Accepted`.
4. Extend receipt endpoint.
5. Move verification to workers.

### Later: Scale And Cost

1. Move raw bodies to object storage.
2. Add retention job.
3. Partition large tables.
4. Add worker dashboards and alerts.
5. Evaluate managed queue when Postgres becomes the bottleneck.

## What Not To Do

- Do not raise concurrency forever as the main reliability strategy.
- Do not remove submit gates entirely.
- Do not let read-origin health depend on heavy dynamic queries.
- Do not make miners solve again because the final POST was busy.
- Do not put full solution bodies into queue messages.
- Do not keep two divergent reliability plans in the repo.
- Do not store raw rejected solution bodies forever.

## Status Checklist

- [ ] **P0 (weight setting): validator feed `/v1/validator/weights/next` protected (Tier 0)**
- [ ] **P0: durable last-known-good signed vector serving `stale_fallback` when origin down**
- [ ] **P0: read recovery tiered (weights > board > leaderboard/recent)**
- [ ] **P0: validator release gate added (5xx/age/UID200/burn checks)**
- [ ] **P0: all three validator weight-feed URLs compatible (canonical + legacy-prefixed + read-service direct)**
- [ ] P0a: read origin restored
- [ ] P0b: submit cap raised and verified
- [ ] P1: split deploy config fixed
- [ ] P2: read-origin root cause documented
- [ ] P3: bounded backpressure shipped
- [ ] P4: durable submit receipts shipped
- [ ] P5: async verification workers shipped
- [ ] P6: retention and storage policy shipped
- [ ] P7: alerts and dashboards live
- [ ] Miner error contract published

## Bottom Line

The coherent plan is:

1. **Protect weight setting first** - the validator feed must always answer, with a durable last-known-good signed vector and validator-specific release gates. This is what makes the plan safe for mainnet, not just for miners.
2. Restore reads.
3. Raise submit capacity to a sane bounded value.
4. Make that config stick.
5. Root-cause the read origin.
6. Replace inline submit verification with durable `202` receipts and async workers.

Weight setting is the chain's source of truth; miner submit/read is the product
experience. Protect the first absolutely, make the second durable. Once submit
admission is durable and idempotent, verification can be slow or bursty without
breaking the miner experience.
