# Cathedral Publisher Role Split Runbook

Goal: split the thin publisher into read, submit, and worker Railway services
without changing SAT scoring semantics or interrupting current miner earnings.

## Current Safe State

- `CATHEDRAL_SERVICE_ROLE=all`
- One public service can serve reads, submits, and background loops.
- This is backward-compatible for miners, but it couples dashboard reads,
  submit verification, PM CNF generation, and refill/scoring background work.

## Shared Config (all services)

These must be identical on every service:

```text
CATHEDRAL_CNF_TOKEN_SECRET=<one shared value>
```

`CATHEDRAL_CNF_TOKEN_SECRET` is the HMAC secret for CNF fetch tokens. The
active-cnf token may be issued on one replica/service and redeemed on another, so
the secret must be the same everywhere or CNF fetch fails across replicas. Never
let it drift between services.

The per-service env below is also encoded in `deploy/railway-split.ps1`, which is
a dry-run by default (`-Apply` to set live). Re-running that script restores the
safe defaults and cannot quietly reset submit to cap `1` or drop the read
statement timeout.

## Target Services

### 1. Read Service

Public domain: `https://api.cathedral.computer`

Required env:

```text
CATHEDRAL_SERVICE_ROLE=read
CATHEDRAL_REFILL_ENABLED=false
CATHEDRAL_SEED_ON_BOOT=false
WEB_CONCURRENCY=2
CATHEDRAL_PM_READ_HARD_CAP=128
CATHEDRAL_PG_STATEMENT_TIMEOUT_MS=4000
```

`CATHEDRAL_PG_STATEMENT_TIMEOUT_MS=4000` is mandatory on every read-serving
service. With no statement timeout, `/v1/leaderboard/recent` was observed running
30-46s and exhausting the connection pool, which took the read origin down. A 4s
ceiling bounds any single query so one slow board scan cannot pin pool
connections. The app logs a loud startup `WARNING` if a read-serving role
(`read` or `all`) boots with this value unset or `0`; do not ignore it.

Guarded knob (off by default): if a slow `/v1/leaderboard/recent` is
head-of-line-blocking the cheap board reads, you may raise `WEB_CONCURRENCY` so
board reads have a worker to run on while a leaderboard query is in flight:

```text
WEB_CONCURRENCY=4
```

Before raising it: confirm the read service has CPU/RAM headroom (each worker is
a full app replica) and that `WEB_CONCURRENCY * CATHEDRAL_PG_POOL_MAX` stays
within the Postgres connection ceiling (pool is per worker). This reduces
head-of-line blocking but does not isolate the board; for true isolation move
the board reads to a healthy publisher origin via the board-failover edge worker
(`deploy/edge-router/board-failover/`). Roll back by setting `WEB_CONCURRENCY=2`.

Allowed traffic:

- `GET /health`, `/health/live`, `/health/ready`
- `GET /.well-known/cathedral-jwks.json`
- `GET /v1/synthetic-boolean/active-challenges`
- `GET /v1/synthetic-boolean/challenge-broadcast`
- `GET /v1/synthetic-boolean/current-challenge`
- `GET /v1/synthetic-boolean/per-miner/status`
- `GET /v1/synthetic-boolean/per-miner/summary`
- `GET /v1/validator/weights/next`
- `GET /v1/leaderboard/recent`
- `GET /v1/leaderboard/top`
- `GET /v1/leaderboard/explain`
- `GET /v1/audit-scanner/*`

Must reject:

- `POST /v1/agents/submit`
- PM CNF hot path if it belongs to the submit service.

### 2. Submit Service

Public domain: `https://submit.cathedral.computer` or edge-routed submit paths.

Required env:

```text
CATHEDRAL_SERVICE_ROLE=submit
CATHEDRAL_REFILL_ENABLED=false
CATHEDRAL_SEED_ON_BOOT=false
CATHEDRAL_SUBMIT_HARD_CAP=8
CATHEDRAL_SUBMIT_MAX_CONCURRENCY=24
WEB_CONCURRENCY=2
CATHEDRAL_PM_READ_HARD_CAP=128
CATHEDRAL_THREADPOOL_TOKENS=32
CATHEDRAL_PG_POOL_MAX=32
CATHEDRAL_PG_STATEMENT_TIMEOUT_MS=4000
```

Do NOT set `CATHEDRAL_SUBMIT_HARD_CAP=1` here. A hard cap of `1` serializes the
whole submit lane: two overlapping submits make one miner get
`429 submit_busy_retry`. The effective cap is `min(CATHEDRAL_SUBMIT_MAX_CONCURRENCY,
CATHEDRAL_SUBMIT_HARD_CAP)`. Start at `8` and only test `16` after the service is
proven stable at `8`. Likewise do not set `CATHEDRAL_PM_READ_HARD_CAP=1`; `128` is
the safe per-miner read gate. The submit service also serves `active-cnf` reads, so
it carries the same `CATHEDRAL_PG_STATEMENT_TIMEOUT_MS=4000` ceiling.

Allowed traffic:

- `GET /health`, `/health/live`, `/health/ready`
- `GET /.well-known/cathedral-jwks.json`
- `GET /v1/synthetic-boolean/active-cnf`
- `GET /v1/synthetic-boolean/per-miner/challenges`
- `GET /v1/synthetic-boolean/per-miner/cnf`
- `GET /v1/challenges/{challenge_id}/cnf`
- `POST /v1/agents/submit`

Must reject:

- `/v1/leaderboard/recent`
- `/v1/leaderboard/top`
- `/v1/leaderboard/explain`
- `/v1/validator/weights/next`
- active board listing endpoints.

### 3. Worker Service

Public domain: none.

Required env:

```text
CATHEDRAL_SERVICE_ROLE=worker
CATHEDRAL_REFILL_ENABLED=true
CATHEDRAL_SINGLETON_RETRY_SECS=15
CATHEDRAL_THREADPOOL_TOKENS=8
CATHEDRAL_PG_POOL_MAX=8
```

Optional after volume growth:

```text
CATHEDRAL_RETENTION_ENABLED=true
CATHEDRAL_RETENTION_EVAL_RUNS_HOURS=48
CATHEDRAL_RETENTION_SOLVE_LEDGER_HOURS=48
CATHEDRAL_RETENTION_PM_ATTEMPT_HOURS=48
CATHEDRAL_RETENTION_PM_KEEP_EPOCHS=2
CATHEDRAL_RETENTION_BATCH_SIZE=25000
```

Allowed traffic:

- health and JWKS only.

Background loops:

- refill
- seed, if explicitly enabled
- arena eval, if explicitly enabled
- arena payout, if explicitly enabled
- stale-ledger retention, if explicitly enabled

All durable background loops use Postgres advisory locks, so a second worker
replica should wait instead of double-running work.

## Cutover Rule

Do not put `api.cathedral.computer` on `CATHEDRAL_SERVICE_ROLE=read` until one
of these is true:

- miners have switched submit traffic to `submit.cathedral.computer`, or
- an edge/router layer routes submit and PM CNF paths to the submit service.

Until then, keep the current public service on `all` and run read/submit/worker
services in parallel for smoke testing.

## Acceptance Checks

Read service:

```text
GET  /health/live                         -> 200, service_role=read
GET  /v1/synthetic-boolean/active-challenges -> 200
POST /v1/agents/submit                    -> 404 route_not_served_by_read_role
```

Submit service:

```text
GET  /health/live                         -> 200, service_role=submit
GET  /v1/leaderboard/top                  -> 404 route_not_served_by_submit_role
GET  /v1/synthetic-boolean/active-cnf     -> reaches auth validation, not role guard
POST /v1/agents/submit                    -> accepts/rejects by normal submit logic
```

Worker service:

```text
GET /health/live                          -> 200, service_role=worker
GET /v1/synthetic-boolean/active-challenges -> 404 route_not_served_by_worker_role
```

Production stability:

```text
No 5xx on hot endpoints for 30 minutes.
No request over 5s on /health/live, active board, weights, recent, or explain.
Submit overload returns fast 429 submit_busy_retry, not timeouts.
Exactly one worker holds each singleton lock at a time.
```

## Rollback

Fastest rollback:

```text
CATHEDRAL_SERVICE_ROLE=all
```

This restores the old single-service behavior without changing scoring.

Do not change:

- SAT scoring mode
- tier weights
- PM/private scoring flags
- signing keys
- database URL

## Required Before Final Split

- Grow the Railway Postgres volume; production was reported at 95% full.
- Current measured main Postgres volume: about 50GB used on a 50GB volume.
- Largest tables: `eval_runs`, `per_miner_assignments`, `agent_submissions`,
  `lane_challenge_solves`, `submit_signatures`, `per_miner_witnesses`.
- Do not rely on `DELETE` alone to save a full Postgres volume. Grow the volume
  first, then enable retention so stale data stops accumulating.
- Confirm DB pool, memory, and CPU headroom after adding services.
- Confirm miners have a working submit base URL or edge path routing.
- Keep `/v1/validator/weights/next` on the read service so validator reads stay
  isolated from submit bursts.

## Edge Router Step

After `read.cathedral.computer` and `submit.cathedral.computer` are both green,
deploy the Cloudflare Worker in `deploy/edge-router`.

The Worker keeps the same public API shape while routing:

- public read routes to `read.cathedral.computer`
- submit/private-CNF routes to `submit.cathedral.computer`
- safe read routes through Cloudflare cache
- signed/private/submit traffic directly to origin with no cache
- query-string cache normalization, so random `?x=...` values do not bypass
  cache and amplify Railway load; unsupported params are rejected
- default-deny for unmatched routes, so new mechanisms require explicit routing
- browser preflight handling for signed submit headers

This is the first step toward a true broadcast layer. It reduces origin read
pressure now while preserving the current Railway services as rollback targets.

Start with exact hot-path Cloudflare routes for SAT/read/submit paths only. Do
not route the whole host with `api.cathedral.computer/*`, and do not route the
whole synthetic, leaderboard, validator, or challenge prefixes until every
public endpoint on the monolith has been explicitly classified.

For this cutover, keep `/v1/challenges/{challenge_id}/cnf` on the existing
monolith. Cloudflare route syntax cannot safely target only that leaf without
also catching unrelated `/v1/challenges/*` generator lease endpoints.

## Retention Safety

Retention is default-off. When enabled on the worker service, it prunes only
bounded batches of stale rows and keeps more than the default 24h scoring window.

Default retained windows:

- `eval_runs`: 48h
- shared solve ledger: 48h
- PM attempts/solves: 48h
- PM assignments: current and previous epoch

Do not enable retention before the volume has enough headroom for normal
Postgres maintenance. Railway supports live volume resize from the volume
settings UI.

## Durable Submit Admission (Phase 3/4/5) — default OFF

Async/durable submit admission is implemented and ships **off**. With every flag
unset the submit endpoint keeps its exact legacy behaviour: synchronous
verification returning `200 {status: ranked|rejected, ...}`. No miner or validator
change is required and the public contract is unchanged.

### What it does when enabled

- `POST /v1/agents/submit` (PUBLIC lane only) does cheap work — parse, verify
  hotkey signature, sha256, persist a durable pending receipt keyed by
  `idempotency_key = sha256(hotkey + challenge_id + dimacs_solution_sha256)` — and
  returns `202` with a `cathedral.submit_receipt.v2` body and `receipt_url`.
- A background worker claims pending attempts in `received_at` order
  (`FOR UPDATE SKIP LOCKED` on Postgres), runs the existing DIMACS verification,
  and records `ranked`/`rejected` + the signed feed rows in the SAME atomic
  transaction the synchronous path uses (identical scoring/payout semantics).
- `GET /v1/agents/receipts/{receipt_id}` resolves the receipt; status advances
  `pending -> ranked | rejected`. Idempotent replays of the same solution return
  the same receipt (no second attempt, no double payout).

### Backpressure (Phase 3, safe to enable independently)

`CATHEDRAL_SUBMIT_BUSY_WAIT_SECS` (default `0.35`) adds a short bounded wait before
`429 submit_busy_retry` on both the dependency gate and the ASGI backpressure
middleware. The hard ceiling is preserved; transient overlaps are far less likely
to bounce a miner. Set `0` to restore the old instant-429 behaviour.

### Flags

| Env | Default | Effect |
|---|---|---|
| `CATHEDRAL_SUBMIT_BUSY_WAIT_SECS` | `0.35` | bounded wait (s) before submit_busy_retry; `0` = legacy instant 429 |
| `CATHEDRAL_SUBMIT_ASYNC_ENABLED` | `false` | PUBLIC submit returns 202 + durable receipt instead of inline 200 |
| `CATHEDRAL_ASYNC_VERIFY_ENABLED` | `false` | run the background verification worker (worker role) |
| `CATHEDRAL_ASYNC_VERIFY_POLL_SECS` | `0.5` | idle poll interval when the queue is empty |
| `CATHEDRAL_ASYNC_VERIFY_BATCH` | `8` | attempts claimed per worker tick |
| `CATHEDRAL_ASYNC_VERIFY_LOCK_SECS` | `120` | claim lock TTL; a crashed worker's row is reclaimable after this |

### Safe enable order

1. Enable `CATHEDRAL_SUBMIT_BUSY_WAIT_SECS` first (lowest risk; pure backpressure).
2. Turn on `CATHEDRAL_ASYNC_VERIFY_ENABLED` on the worker role and confirm the loop
   logs `[verify] singleton_lock_acquired` and drains an empty queue without error.
3. Only then turn on `CATHEDRAL_SUBMIT_ASYNC_ENABLED` so 202s start flowing into a
   worker that is already running. (If you flip submit async on with no worker,
   receipts stay `pending` forever — verify the worker first.)
4. Per-request escape hatch: a client can force the legacy synchronous path with
   header `X-Cathedral-Submit-Mode: sync` even while async is enabled.

### Scope / not-yet

- Only the PUBLIC SAT lane uses durable admission. The per-miner (`pm-`) lane keeps
  its existing inline path. Do not assume `pm-` submits return 202.
- The raw solution body is held in `per_miner_attempts.solution_body` until verified,
  then nulled on accept. Object-storage offload (Phase 6) is not wired yet.
