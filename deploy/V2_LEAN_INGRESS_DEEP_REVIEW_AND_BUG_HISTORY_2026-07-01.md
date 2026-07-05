# V2 Lean Ingress — Deep Review and Bug History

Date: 2026-07-01  
Branch: `feat/solution-manifest-v2`  
Status: ready for isolated public replay-spam test; not ready for unrestricted unique-submit production traffic

## Executive Summary

We ended up at the lean V2 ingress because the current architecture proved three things at live scale:

1. **V1 submit is too synchronous and body-heavy** for miner growth.
2. **Railway/FastAPI/Postgres is not the right ACK path** for very high-frequency tiny submissions.
3. **The future submit path must be small, token-bound, durable, async-verifiable, and cheap.**

The latest lean-ingress work provides exactly the next safe test step:

```text
miners fetch challenge/token/CNF from current V2 beta
miners submit/replay-spam solved bitsets to a separate lean ingress endpoint
lean ingress validates token/signature/shape
lean ingress writes one local SQLite WAL event
lean ingress returns status=received
current V1/V2 production endpoints remain untouched
```

This is ready for the scoped replay-spam test. It is not a production rewards path.

## Why We Got Here

### 1. We saw V1 submit hangs and sync saturation

The original miner path submits full solution bodies to:

```text
POST /v1/agents/submit
```

Observed behavior:

- requests could hang client-side while still committing server-side
- live submit pressure saturated the sync handler
- overload protection produced clean `429 submit_busy_retry`
- full DIMACS solution bodies created too much CPU/body/DB pressure for high-frequency miner work

The operational diagnosis was:

```text
V1 submit is reliable enough for current production, but not the scalable design for many more tasks per epoch.
```

### 2. We confirmed body mirroring is expensive

We built Cloudflare edge mirroring to shadow V1 submit traffic into V2.

Full-body mirror result:

- even around 1% live traffic, the V2 beta FastAPI/blob/DB path overloaded
- V2 health/metrics timed out or returned 502
- conclusion: full solution bodies through the V2 service are not production-scale

Metadata-only mirror result:

- metadata-only mode held 100% live submit traffic for a probe
- observed approximately 23k submits/minute
- but raw Postgres row growth was huge
- conclusion: the path can handle tiny bodies better, but permanent per-submit synchronous DB writes are still not the right final ACK design

### 3. Railway/Postgres cannot be the 50ms ACK path

Measured public responses stayed in hundreds of milliseconds:

```text
Railway/FastAPI/Postgres path: ~500-1000ms public RTT range
```

That is acceptable for a beta API, not for a high-performance submit engine.

The target architecture became:

```text
cheap admission ACK first
durable local/event log
batch flush later
async verification later
```

### 4. Miner task pagination exposed future load risk

Miners reported seeing only ~100 tasks while top miners showed hundreds or 1000+ solves.

The likely cause is endpoint pagination:

```text
/v1/synthetic-boolean/per-miner/challenges?offset=0&limit=50
```

If miners only fetch the first page, they under-utilize their per-miner task set. Telling them to page correctly is honest, but it increases load:

- more CNF fetches
- more solves
- more submits
- more V1 full-body pressure

That reinforces the need for V2 tiny submits before broadly encouraging higher per-epoch throughput.

## Bugs and Reliability Problems Encountered

### A. Weights freshness/flapping

Symptoms:

- validator/read endpoints could serve inconsistent or stale cached data
- weights freshness needed guards
- DB readiness intermittently flapped with `503`

Root issue:

```text
weights/read infrastructure needed stronger freshness/monotonic behavior and better failover semantics
```

Outcome:

- freshness/failover work was prioritized
- dashboard probes were added
- long-term target is signed shared weight vectors with edge/KV and origin fallback

### B. Submit handler saturation

Symptoms:

- `/v1/agents/submit` could block under load
- clients could time out even when server-side work had committed
- submit pressure created `submit_busy_retry`

Root issue:

```text
full body + synchronous verification/storage/DB in request path
```

Outcome:

- async-safety fixes were reviewed
- overload behavior now returns clean `429 submit_busy_retry`
- V2 work moved toward small signed manifests and bitsets

### C. Full-body V2 mirror overload

Symptoms:

- full-body mirror at small sample overloaded V2 beta
- metrics/health timed out
- blob/DB writes were too expensive

Root issue:

```text
V2 FastAPI/blob/Postgres was still receiving large bodies and doing too much per submit
```

Outcome:

- full-body mirror disabled
- metadata-only probe added
- design shifted toward tiny bitset submit and artifact manifests

### D. Metadata-only mirror row growth

Symptoms:

- metadata-only mode sustained live traffic
- but generated over 1.5M rows in a short test window
- logical body-byte counters grew into GBs

Root issue:

```text
per-submit Postgres writes are still a cost/storage surface even with tiny bodies
```

Outcome:

- metadata mirror paused
- conclusion: use event-log/batched persistence, not synchronous DB per ACK

### E. V2 bitset scoring/audit blockers

External review found real blockers in the first bitset beta:

1. manifest + bitset could double-count the same challenge
2. bitset events were missing from audit bundles
3. `/v2/agents/submit-bitset` was not under the same hot-path/body/backpressure controls

Fixes shipped:

- scoring dedupes by `(miner_hotkey, challenge_id)` across sources
- bitset preferred over manifest when both exist
- audit bundle includes source-tagged bitset events
- bitset endpoint got submit hot-path gates and body cap before JSON parse
- regression tests added

### F. Artifact/proof manifest review blockers

The artifact/proof plan initially had design risks:

- idempotency included `decoded_sha256`, allowing score farming by many valid artifacts for the same challenge
- no submit-token binding
- insufficient fetch safety/SSRF/decompression-bomb controls
- provider fields were too entangled with Cathedral-native PM challenges

Fixes in the design doc:

- idempotency changed to `(miner_hotkey, challenge_id, result)`
- Cathedral HMAC submit token required
- `challenge_source` split into `cathedral_pm` vs future provider-backed source
- fetch allowlist and streaming byte/decompression caps added
- LRAT admitted only as `received_unverified`, `weighted_score=0`, until checker exists

### G. Lean ingress F1 issue from latest review

Reviewer found a real future flusher bug:

```text
If Phase 1 only validates shape and defers verification, then a miner can submit a shape-valid but wrong bitset first.
Because idempotency was keyed by (hotkey, challenge), their later correct bitset would be swallowed as a replay.
When the flusher/verifier rejects the wrong row, the challenge would be dead for that miner.
```

This could not happen in the old inline verifier path because wrong witnesses were rejected before durable insert.

Feedback-driven fix now implemented:

```text
If an existing row has status='rejected', a later valid signed submit for the same (hotkey, challenge) re-admits by updating that row back to status='received'.
```

This preserves one active row per challenge while creating a retry path after verifier rejection.

### H. Lean ingress F2 griefing issue

Reviewer noted:

```text
Without registration/quota, anyone can mint fresh keypairs, fetch challenges, and fill the unique-row backlog.
```

The current guards make this safe for disk:

- max unflushed event cap
- max storage bytes
- min free disk guard
- readiness degradation
- `503 ingress_backlog_full`

But honest unique submissions could be blocked if a grief test fills the cap.

Conclusion:

```text
Replay-spam public test is fine.
Unrestricted unique-row traffic requires registration/per-hotkey quotas before real use.
```

## Latest Changes After Review Feedback

### 1. Exact signed replay fast path

Before:

```text
submit token was verified before idempotent replay lookup
expired tokens prevented duplicate replay responses
```

Now:

```text
body cap
JSON parse
normalize
fresh submitted_at header check
hotkey signature verification
exact replay lookup by hotkey + challenge + submit_token_id
if found and row is not rejected: return existing receipt
otherwise verify submit token and admit new row
```

Why this is safe:

- no new row is admitted without unexpired HMAC token
- replay requires valid hotkey signature
- replay must match the existing submit token hash
- replay only returns an existing non-rejected row

Effect:

```text
Replay-spam tests can continue after the original 300s token expires, as long as miners keep signing the replay request with a fresh submitted_at header.
```

### 2. Re-admission after verifier rejection

New behavior:

```text
existing status != rejected => replay existing receipt
existing status == rejected => allow a new signed/token-valid bitset to replace row and return status=received
```

This directly addresses the F1 future flusher bug.

### 3. Unflushed index added

Added:

```sql
CREATE INDEX IF NOT EXISTS idx_submit_events_local_unflushed
  ON submit_events_local(flushed_at_iso, received_at_iso);
```

Reason:

- pressure checks and future flusher queries scan unflushed rows
- fine at 100k either way, but this avoids obvious scaling pain

### 4. Readiness and pressure guards already in place

Current guards:

```text
CATHEDRAL_V2_INGRESS_MAX_UNFLUSHED_EVENTS
CATHEDRAL_V2_INGRESS_MAX_STORAGE_BYTES
CATHEDRAL_V2_INGRESS_MIN_FREE_DISK_BYTES
CATHEDRAL_V2_INGRESS_MAX_UNFLUSHED_AGE_SECS
```

Endpoints:

```text
GET /health/live
GET /health/ready
GET /v2/ingress/metrics
```

### 5. Runbook updated

Runbook now states:

- exact signed replays can continue after token TTL
- new unique submissions still require fresh token
- registration/quota required before unrestricted unique-row traffic
- rejected rows can be retried once future verifier marks them rejected

## Current Lean Ingress Behavior

Endpoint:

```text
POST /v2/agents/submit-bitset
```

Successful Phase-1 response:

```json
{
  "status": "received",
  "terminal": false,
  "open": true,
  "weighted_score": 0.0
}
```

Meaning:

- durable local admission succeeded
- not verified yet
- not scored
- no rewards
- no V1 weights impact
- no V2 shadow weights impact until flusher/verifier exists

## Current Test Plan

### Safe public test shape

```text
challenge_base = https://v2-beta.cathedral.computer
submit_base    = https://v2-ingress-test.cathedral.computer
```

Miner command:

```bash
python3 scripts/v2_bitset_miner_e2e.py \
  --challenge-base https://v2-beta.cathedral.computer \
  --submit-base https://v2-ingress-test.cathedral.computer \
  --limit 1 \
  --expect-status received \
  --skip-weights \
  --repeat-submit 100
```

This creates one unique row and many replay requests.

### Why this test is safe

- current V1 endpoints untouched
- current V2 beta endpoints untouched
- no route replacement
- no Postgres write in ACK path
- no Railway submit path involvement for the test submit endpoint
- local SQLite WAL bounded by safety caps
- exact duplicate spam is idempotent

## Tests Run

Latest suite:

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

New regression coverage includes:

- exact signed replay bypasses later token expiry
- existing rejected row can be re-admitted
- backpressure allows replay but blocks new unique rows
- body cap before JSON parse
- golden vector compatibility

## Remaining Risks / Gates

### Gate 1 — registration or per-hotkey quotas

Before any unrestricted unique-row test:

```text
registration eligibility or per-hotkey quota must exist
```

Otherwise a grief tester can fill the unique backlog with fresh keypairs.

### Gate 2 — flusher/verifier

Before any scoring use:

```text
local WAL -> V2 Postgres batch flusher
received -> verified/rejected verifier state machine
```

No scoring should consume `received` rows.

### Gate 3 — retention

Before longer tests:

```text
local WAL pruning after flush
archive/audit chunk publishing
retention policy
```

### Gate 4 — deployment hardening

Before public endpoint:

```text
single worker or tested SQLite multi-worker behavior
persistent disk
systemd/service restart policy
metrics scraping
Cloudflare/DNS configured separately from v2-beta
```

### Gate 5 — real production path

Before using for rewards:

```text
registration eligibility
anti-spam quotas
flusher/verifier
audit inclusion
shadow-vs-real scoring decision
validator review
```

## Deeper Review Questions For Agents

1. Is exact signed replay before token-HMAC verification acceptable when constrained by existing `submit_token_id` and non-rejected row status?
2. Is updating a rejected row in place the best retry model, or should a retry create a new receipt with parent linkage?
3. Should unique-row quotas be per hotkey, per IP, per token, or based on registration snapshot only?
4. Should Phase 1 store `assignment_b64`, or only `assignment_sha256` and raw compact bytes?
5. Should the flusher write directly into `v2_submit_events`, or use a separate `v2_ingress_events` staging table?
6. Should `status='received'` rows ever appear in audit bundles, or only after verifier terminal status?
7. Is SQLite WAL acceptable for the first public replay-spam test with one worker and hard backlog/storage caps?

## Bottom Line

The system moved here because live evidence showed full-body, synchronous, Railway/Postgres-backed submit paths will not scale cheaply.

The lean ingress is the correct next experiment:

```text
small signed request
durable local ACK
bounded storage
idempotent replay
async verification later
no managed queue bill
no production route change
```

After the latest feedback updates, it is ready for the scoped isolated replay-spam test.

It is not yet ready for unrestricted unique-row spam or production scoring.

## Addendum — Public-Exposure Hardening After Deep Review

A later deep review approved the code for a closed replay-spam test but found public-exposure blockers. These have now been addressed in code or converted into explicit deployment gates.

### H1 fixed: pre-auth rejects no longer write SQLite by default

Problem:

```text
bad JSON / bad token / oversized junk could call record_reject()
record_reject() wrote SQLite
junk flood could contend with real admits on the single SQLite write lock
```

Fix:

```text
reject counters are now in-memory by default
accepted events remain durable in SQLite WAL
invalid junk no longer creates reject_rollup SQLite writes in the hot path
```

Test coverage:

```text
test_lean_ingress_rejects_bad_token_before_event
```

This test verifies the reject appears in metrics while `reject_rollups_local` remains empty.

### H2 fixed: metrics are cached and can be token-gated

Problem:

```text
public /health/ready and /v2/ingress/metrics could repeatedly scan local SQLite tables
```

Fix:

```text
metrics payloads are cached with CATHEDRAL_V2_INGRESS_METRICS_TTL_SECS
/v2/ingress/metrics supports CATHEDRAL_V2_INGRESS_METRICS_TOKEN
/health/ready uses the cached metrics snapshot
```

Public deployment should set:

```text
CATHEDRAL_V2_INGRESS_METRICS_TOKEN=<operator-only-token>
CATHEDRAL_V2_INGRESS_METRICS_TTL_SECS=1.0
```

Test coverage:

```text
test_lean_ingress_metrics_token_gate
```

### H3 fixed: single-process/worker enforcement

Problem:

```text
multiple uvicorn/gunicorn/Railway workers could hit one SQLite WAL file and cause SQLITE_BUSY/500s
```

Fix:

```text
common worker-count env vars >1 fail closed at boot
SQLite DB path gets a POSIX process lock sidecar file
runbook pins WEB_CONCURRENCY=1 and --workers 1
```

Test coverage:

```text
test_lean_ingress_rejects_multi_worker_env
```

### Per-IP rate limiter added

For public tests, the ingress now supports:

```text
CATHEDRAL_V2_INGRESS_IP_RPM=6000
```

This is a local fixed-window per-IP limiter using `CF-Connecting-IP`, then `X-Forwarded-For`, then socket peer.

Test coverage:

```text
test_lean_ingress_ip_rate_limit_before_body_work
```

### F-MINT partially addressed with a default-off mint allowlist

Problem:

```text
V2 beta challenge/token-mint endpoint allowed any signed hotkey to mint tokens
fresh keypairs could mint many tokens and fill unique-row backlog
```

Fix added:

```text
CATHEDRAL_V2_SUBMIT_TOKEN_ALLOWLIST=<comma-separated tester hotkeys>
```

When set on the V2 beta challenge/token service, only allowlisted hotkeys receive V2 bitset submit tokens. When unset, current behavior is unchanged.

This is a test gate, not the final registration system. Before real unique-row public traffic, replace or supplement it with registration/stake eligibility and per-hotkey quotas.

### Documentation nits fixed

- `deploy/V2_BITSET_INGRESS_CONTRACT_2026-06-30.md` now matches the real validation order: signature and exact replay lookup happen before fresh token HMAC verification.
- The runbook now states that `synchronous=NORMAL` is a practical WAL durability/performance tradeoff, not the same thing as a fully replicated durable queue.

### Updated test result

Latest relevant suite:

```text
29 passed
```

Latest local HTTP E2E:

```text
E2E_OK
status=received
idempotent_replay=True
unflushed_events=1
```

### Updated verdict

Ready for:

```text
closed replay-spam test
public replay-spam test only if:
  - metrics token is set
  - IP rate limit is set
  - one worker/process is enforced
  - V2 submit token allowlist is set on the challenge/token service
```

Still not ready for:

```text
unrestricted unique-row public spam
production rewards
real validator weights
```
