# V2 Lean Ingress Public Spam Test Runbook

Date: 2026-06-30  
Status: proposed beta test, no production route change

## Goal

Keep all current endpoints running, but let miners hammer the new lean ingress endpoint safely.

Current V2 beta stays live:

```text
https://v2-beta.cathedral.computer
```

New isolated test endpoint:

```text
https://v2-ingress-test.cathedral.computer
```

No existing route is replaced.

## Test Shape

Miners fetch challenge/token/CNF from current V2 beta:

```text
GET https://v2-beta.cathedral.computer/v2/synthetic-boolean/per-miner/challenges
GET https://v2-beta.cathedral.computer/v2/synthetic-boolean/per-miner/cnf
```

Miners submit solved bitsets to the new lean ingress:

```text
POST https://v2-ingress-test.cathedral.computer/v2/agents/submit-bitset
GET  https://v2-ingress-test.cathedral.computer/v2/agents/submit-bitset/receipts/{receipt_id}
```

This means:

- current V2 beta submit endpoint remains available
- current V1 endpoints are untouched
- V2 shadow weights are untouched by lean-ingress rows until a flusher/verifier is added
- spam load lands on the lean ingress local SQLite WAL, not Railway/Postgres submit path

## Required Operator Config

The lean ingress test service must use the same V2 submit token secret as the V2 beta challenge service:

```text
CATHEDRAL_V2_SUBMIT_TOKEN_SECRET=<same secret as v2-beta token minting>
```

Do not expose the secret.

Required safety env on the lean ingress host:

```text
WEB_CONCURRENCY=1
CATHEDRAL_V2_INGRESS_DB_PATH=/var/lib/cathedral/v2-ingress-test.sqlite3
CATHEDRAL_V2_SUBMIT_BITSET_MAX_BODY_BYTES=16384
CATHEDRAL_V2_INGRESS_TIMESTAMP_SKEW_SECS=300
CATHEDRAL_V2_INGRESS_MAX_UNFLUSHED_EVENTS=100000
CATHEDRAL_V2_INGRESS_MAX_STORAGE_BYTES=1000000000
CATHEDRAL_V2_INGRESS_MIN_FREE_DISK_BYTES=100000000
CATHEDRAL_V2_INGRESS_MAX_UNFLUSHED_AGE_SECS=0
CATHEDRAL_V2_INGRESS_IP_RPM=6000
CATHEDRAL_V2_INGRESS_METRICS_TOKEN=<operator-only-token>
CATHEDRAL_V2_INGRESS_METRICS_TTL_SECS=1.0
```

Required safety env on the V2 beta challenge/token-mint service for an open internet test:

```text
CATHEDRAL_V2_SUBMIT_TOKEN_ALLOWLIST=<comma-separated tester hotkeys>
```

If the allowlist is unset, current behavior is unchanged: any signed hotkey can fetch V2 bitset submit tokens. That is fine for closed testing but not for unrestricted public exposure.

Notes:

- `MAX_UNFLUSHED_EVENTS` stops new unique accepted rows once the local backlog reaches the cap.
- Idempotent replay spam still returns the existing receipt even when the unique-row cap is reached.
- `MAX_UNFLUSHED_AGE_SECS=0` is intentional for Phase 1 because there is no flusher yet.
- Rejected requests are counted in memory by default, not written to SQLite, so pre-auth junk does not contend on the WAL write lock.
- `/v2/ingress/metrics` should be token-gated for public tests.
- The service asserts one worker via env and takes a process lock next to the SQLite DB.

Run command example:

```bash
PYTHONPATH=. python3 -m uvicorn scaffold.publisher.v2_lean_ingress:app \
  --host 0.0.0.0 \
  --port 8799 \
  --workers 1
```

Use one worker for SQLite WAL Phase 1. The service now fails closed if common worker-count env vars are greater than one, and it also takes a process lock on the SQLite DB path.

## DNS / Routing

Create a separate DNS name only:

```text
v2-ingress-test.cathedral.computer -> lean ingress host
```

Do not change:

```text
v2-beta.cathedral.computer
submit.cathedral.computer
api.cathedral.computer
```

## Miner Test Command

From the Cathedral repo:

```bash
python3 scripts/v2_bitset_miner_e2e.py \
  --challenge-base https://v2-beta.cathedral.computer \
  --submit-base https://v2-ingress-test.cathedral.computer \
  --limit 1 \
  --expect-status received \
  --skip-weights \
  --repeat-submit 100
```

Expected:

```text
E2E_OK
status=received
repeat_submit=100
```

Important: `received` is correct for lean ingress Phase 1. This does not mean verified/scored.

## Safest Spam Mode

The command above solves one challenge and repeats the same valid submit many times.

This tests:

- public ingress latency
- body cap path
- token verification
- hotkey signature verification
- bitset shape validation
- SQLite idempotency lookup
- receipt fetch

It creates only one accepted event row because duplicates are idempotent replays.

After the feedback update, exact signed replays for an existing non-rejected row stay cheap even if the original 300s submit token has expired. New unique rows still require a valid unexpired submit token.

This is the safest first public spam test.

## Higher-Write Test Mode

For unique accepted rows, miners need unique challenges/tokens. Do this only after the safe replay spam test passes.

Suggested staged caps:

```text
stage 1: 5 miners, repeat-submit 100 replay mode
stage 2: 20 miners, repeat-submit 500 replay mode
stage 3: 50 miners, repeat-submit 1000 replay mode
stage 4: bounded unique-submit test after backpressure limits are added
```

Do not invite unrestricted unique-row spam until the Postgres flusher exists and has been tested, and until registration/per-hotkey quota is enforced.

Without a registration/quota gate, anyone can mint fresh keypairs, fetch challenge sets, and fill the unique-row backlog with shape-valid submissions. The ingress guards bound disk and keep exact replays working, but honest new unique submissions can still get `503 ingress_backlog_full` if a grief test fills the cap.

The ingress now has:

```text
max_unflushed_events
max_storage_bytes
disk free guard
optional oldest_unflushed_age guard
```

For Phase 1 public testing, use replay spam first because it validates hot-path throughput without filling the local WAL with many unique rows.

The code also supports future verifier retry semantics: if a row is later marked `rejected`, a miner can re-admit a new bitset for the same `(hotkey, challenge)` instead of being permanently locked out by the original idempotency key.

## What Miners Should Understand

This is a test endpoint only.

It does not:

- affect V1 rewards
- affect V1 validator weights
- affect V2 beta shadow weights yet
- return `verified`
- pay miners
- replace current endpoints

It only tests the future lean ACK path.

## Monitoring During Test

Before inviting miners, check:

```text
GET https://v2-ingress-test.cathedral.computer/health/live
GET https://v2-ingress-test.cathedral.computer/health/ready
GET https://v2-ingress-test.cathedral.computer/v2/ingress/metrics
```

`/health/ready` must return `200` and `status=ok`.

For metrics, include:

```text
Authorization: Bearer <CATHEDRAL_V2_INGRESS_METRICS_TOKEN>
```

Watch:

```text
total_events
unflushed_events
rejects by reason
process CPU/RSS
disk bytes for sqlite + wal
p95/p99 HTTP latency
```

Also watch current services to ensure the challenge side is not impacted:

```text
https://v2-beta.cathedral.computer/health/live
https://api.cathedral.computer/health/ready
https://submit.cathedral.computer/health/live
```

## Abort Conditions

Stop the test if:

```text
V1 submit health degrades
V1 API ready flaps
V2 beta health degrades
lean ingress disk/WAL grows unexpectedly
lean ingress p95 gets unstable
rejects spike unexpectedly
```

Rollback is simple:

```text
turn off DNS / stop lean ingress service
```

No production endpoint route needs to change.
