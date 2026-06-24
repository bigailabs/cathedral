# Cathedral Publisher Role Split Runbook

Goal: split the thin publisher into read, submit, and worker Railway services
without changing SAT scoring semantics or interrupting current miner earnings.

## Current Safe State

- `CATHEDRAL_SERVICE_ROLE=all`
- One public service can serve reads, submits, and background loops.
- This is backward-compatible for miners, but it couples dashboard reads,
  submit verification, PM CNF generation, and refill/scoring background work.

## Target Services

### 1. Read Service

Public domain: `https://api.cathedral.computer`

Required env:

```text
CATHEDRAL_SERVICE_ROLE=read
CATHEDRAL_REFILL_ENABLED=false
CATHEDRAL_SEED_ON_BOOT=false
```

Allowed traffic:

- `GET /health`, `/health/live`, `/health/ready`
- `GET /.well-known/cathedral-jwks.json`
- `GET /v1/synthetic-boolean/active-challenges`
- `GET /v1/synthetic-boolean/challenge-broadcast`
- `GET /v1/synthetic-boolean/current-challenge`
- `GET /v1/synthetic-boolean/per-miner/status`
- `GET /v1/synthetic-boolean/per-miner/summary`
- `GET /v1/validator/weights/next`
- `GET /v1/leaderboard/*`
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
CATHEDRAL_SUBMIT_HARD_CAP=1
CATHEDRAL_PM_READ_HARD_CAP=1
CATHEDRAL_THREADPOOL_TOKENS=16
CATHEDRAL_PG_POOL_MAX=16
```

Allowed traffic:

- `GET /health`, `/health/live`, `/health/ready`
- `GET /.well-known/cathedral-jwks.json`
- `GET /v1/synthetic-boolean/active-cnf`
- `GET /v1/synthetic-boolean/per-miner/challenges`
- `GET /v1/synthetic-boolean/per-miner/cnf`
- `GET /v1/challenges/{challenge_id}/cnf`
- `POST /v1/agents/submit`

Must reject:

- `/v1/leaderboard/*`
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

Allowed traffic:

- health and JWKS only.

Background loops:

- refill
- seed, if explicitly enabled
- arena eval, if explicitly enabled
- arena payout, if explicitly enabled

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
- Confirm DB pool, memory, and CPU headroom after adding services.
- Confirm miners have a working submit base URL or edge path routing.
- Keep `/v1/validator/weights/next` on the read service so validator reads stay
  isolated from submit bursts.
