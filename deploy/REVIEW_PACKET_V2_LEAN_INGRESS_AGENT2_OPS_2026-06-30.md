# Review Packet — Agent 2 Ops/Performance/Cost

Date: 2026-06-30  
Target branch: `feat/solution-manifest-v2`  
Scope: V2 lean bitset ingress Phase 1

## What Changed

A standalone lean ingress service was added for the V2 PM bitset submit path.

Primary file:

```text
scaffold/publisher/v2_lean_ingress.py
```

Support files:

```text
scripts/v2_lean_ingress_e2e.py
scaffold/publisher/tests/test_v2_lean_ingress.py
deploy/V2_LEAN_INGRESS_PLAN_2026-06-30.md
deploy/V2_BITSET_INGRESS_CONTRACT_2026-06-30.md
deploy/golden/v2_bitset_ingress_golden.json
```

## Goal

Reduce V2 submit ACK path cost/latency by avoiding a per-submit Railway/Postgres transaction.

Current beta:

```text
miner -> Railway/FastAPI -> Postgres -> receipt
```

Lean ingress Phase 1:

```text
miner -> dedicated ingress -> local SQLite WAL -> receipt
```

Postgres batch flusher is intentionally next phase.

## Runtime Behavior

Endpoint:

```text
POST /v2/agents/submit-bitset
GET  /v2/agents/submit-bitset/receipts/{receipt_id}
GET  /v2/ingress/metrics
GET  /health/live
```

Env:

```text
CATHEDRAL_V2_SUBMIT_TOKEN_SECRET=<required>
CATHEDRAL_V2_INGRESS_DB_PATH=./data/v2-ingress.sqlite3
CATHEDRAL_V2_SUBMIT_BITSET_MAX_BODY_BYTES=16384
CATHEDRAL_V2_INGRESS_TIMESTAMP_SKEW_SECS=300
```

Storage:

```text
SQLite WAL
submit_events_local
reject_rollups_local
```

Accepted events are durable local rows. Invalid traffic is rolled up by reason instead of raw-row stored.

## Phase-1 Receipt Semantics

Returns:

```text
status=received
terminal=false
open=true
weighted_score=0.0
```

This is intentional. The current Railway V2 endpoint returns `verified`; lean ingress does not until verifier/flusher phase exists.

## Ops Review Focus

### 1. SQLite WAL suitability

Review:

- `PRAGMA journal_mode=WAL`
- `PRAGMA synchronous=NORMAL`
- `busy_timeout=30000`
- one transaction per accepted event for Phase 1
- idempotency via unique index

Question:

- Is this acceptable for beta canary volume before batch flusher?
- Should we use `synchronous=FULL` for stricter durability, or keep `NORMAL` for cost/latency?

### 2. Backpressure gaps

Current Phase 1 does not yet implement:

```text
max_unflushed_events
max_unflushed_bytes
disk pressure checks
oldest_unflushed_age guard
```

Review request:

- mark these as blockers before public canary if needed
- suggest default thresholds for cheap VPS disk

### 3. Metrics gaps

Current metrics:

```json
{
  "events": {"received": 1},
  "rejects": {},
  "total_events": 1,
  "unflushed_events": 1
}
```

Suggested next metrics:

```text
p50/p95 admit latency
sqlite write ms
body bytes accepted/rejected
oldest_unflushed_age_secs
db file bytes
wal file bytes
rejects per minute
idempotent replay count
```

Review request:

- identify minimum production dashboards/alerts

### 4. Deployment shape

Suggested first deployment:

```text
cheap VPS or small VM
Cloudflare proxied DNS
uvicorn/gunicorn or systemd service
local persistent disk
```

Not recommended yet:

```text
Cloudflare Queues
Durable Objects
R2 per-submit storage
Kafka/NATS/Redis
```

Review request:

- validate cheapest reliable deployment target
- identify required backup/snapshot policy

### 5. Local-log retention

Phase 1 has no pruning.

Review request:

- recommend retention policy once flusher exists
- recommend local WAL/archive cleanup process

### 6. Flusher design next

Next required code:

```text
select unflushed rows limit N
bulk insert/upsert into V2 Postgres v2_submit_events or staging table
mark flushed_at_iso
verifier processes received rows
```

Review request:

- should flusher run in same process or sidecar?
- should it write directly to existing `v2_submit_events` or a new `v2_ingress_events` staging table?

## Tests Run

Unit/integration:

```bash
PYTHONPATH=. pytest -q \
  scaffold/publisher/tests/test_v2_lean_ingress.py \
  scaffold/publisher/tests/test_v2_bitset_ingress_contract.py \
  scaffold/publisher/tests/test_solution_manifest_v2.py
```

Result:

```text
22 passed
```

Local HTTP E2E:

```bash
CATHEDRAL_V2_SUBMIT_TOKEN_SECRET=dev-v2-lean-ingress-secret-not-live \
CATHEDRAL_V2_INGRESS_DB_PATH=/tmp/ingress.sqlite3 \
PYTHONPATH=. python3 -m uvicorn scaffold.publisher.v2_lean_ingress:app \
  --host 127.0.0.1 --port 8799

PYTHONPATH=. python3 scripts/v2_lean_ingress_e2e.py \
  --base http://127.0.0.1:8799 \
  --secret dev-v2-lean-ingress-secret-not-live
```

Result:

```text
E2E_OK
receipt_id=v2in_690aa22d780948f6b8aff8867a345f1c
status=received
idempotent_replay=True
unflushed_events=1
```

## Known Limits / Non-Goals

- not deployed yet
- no production routing yet
- no Postgres flusher yet
- no verifier in ingress yet
- no real rewards/weights impact
- no managed queue dependency
- Rust port deferred because the local harness lacks Rust toolchain

## Requested Review Verdict

Please answer:

1. Is SQLite WAL an acceptable Phase-1 queue for beta ingress?
2. What backpressure thresholds are required before live canary?
3. Is same-process flusher acceptable, or should it be a sidecar?
4. Which metrics/alerts are blockers before deployment?
5. Any cost-risk that would push us back toward Railway/managed queue?
