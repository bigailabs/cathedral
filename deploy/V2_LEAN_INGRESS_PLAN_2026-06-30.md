# V2 Lean Ingress Plan

Date: 2026-06-30  
Status: architecture map  
Goal: move V2 submit admission off the Railway/FastAPI hot path while staying lean, cheap, and auditable

## TL;DR

The V2 bitset beta made submissions tiny, but the request still goes through:

```text
miner -> v2-beta.cathedral.computer -> Railway/FastAPI -> Postgres
```

That is why public submit latency is still hundreds of ms.

The next lean target is:

```text
miner -> cathedral-v2-ingress -> local durable event log -> fast receipt
                                  -> batch flusher/verifier -> V2 Postgres/audit
```

No managed worker queue is required. The "queue" is a small local append-only event log / embedded SQLite WAL owned by Cathedral.

Railway remains for:

- V2 read/admin APIs
- dashboards
- verifier workers during transition
- V2 shadow weights
- audit bundle serving

But it leaves the submit ACK path.

## Design Principles

1. **No full bodies in hot path**
   - bitset submit only for PM SAT
   - artifact/proof manifests later, not proof bytes

2. **No per-submit Postgres write before ACK**
   - local durable append first
   - Postgres gets batched writes

3. **No managed worker queues**
   - no Cloudflare Queues requirement
   - no Kafka/NATS/Redis dependency for v1
   - local WAL/SQLite event log is enough

4. **Reject garbage before durable append**
   - content-length/body cap
   - JSON shape
   - HMAC submit token
   - hotkey signature
   - bitset size/base64
   - per-hotkey/per-challenge attempt cap

5. **Auditability stays first-class**
   - append-only event ids
   - event hash chain / chunk hash
   - periodic compressed audit chunks to Hippius/object storage

6. **Keep current challenge source untouched**
   - no PM challenge rewiring in this move
   - no provider integration in this move

## What Moves

Move only V2 submit admission:

```text
POST /v2/agents/submit-bitset
GET  /v2/agents/submit-bitset/receipts/{id}
```

Optional later:

```text
POST /v2/agents/submit-artifact-manifest
GET  /v2/agents/submit-artifact-manifest/receipts/{id}
```

Keep on Railway for now:

```text
GET /v2/synthetic-boolean/per-miner/challenges
GET /v2/synthetic-boolean/per-miner/cnf
GET /v2/validator/weights/next
GET /v2/audit/epochs/{epoch}
POST /v2/admin/verify/tick
```

## Proposed Runtime

### `cathedral-v2-ingress`

A tiny dedicated service, preferably Rust.

Why Rust:

- small single binary
- low memory
- high concurrency
- fast JSON/hash/base64/HMAC
- good sr25519 ecosystem via Substrate/schnorrkel crates
- reliable local disk/WAL control

Python/FastAPI works for fast iteration, but if the goal is a well-running engine, the hot ingress should be a small compiled service.

### Responsibilities

Hot path:

```text
receive request
body cap before parse
normalize canonical body
verify submit token HMAC
verify hotkey signature
check bitset byte size/trailing bits
check idempotency key
append accepted admission event to local durable log
return receipt
```

Not in hot path:

```text
Postgres write
leaderboard query
weights composition
large artifact fetch
proof verification
provider calls
```

## Local Durable Log

Use embedded SQLite in WAL mode or a simple append-only segment log.

Recommended v0: SQLite WAL.

Rationale:

- very cheap
- easy inspect/recover
- ACID idempotency insert
- handles 100s-1000s writes/sec on small records
- no new queue service/bill
- one binary + one local volume

Tables:

```sql
submit_events_local (
  receipt_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  miner_hotkey TEXT NOT NULL,
  challenge_id TEXT NOT NULL,
  result TEXT NOT NULL DEFAULT 'sat',
  event_json TEXT NOT NULL,
  event_sha256 TEXT NOT NULL,
  received_at_iso TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'received',
  flushed_at_iso TEXT,
  verified_at_iso TEXT,
  rejection_reason TEXT
)
```

```sql
reject_rollups_local (
  bucket_iso TEXT NOT NULL,
  reason TEXT NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY(bucket_iso, reason)
)
```

No raw invalid rows by default.

## Idempotency

For bitset PM:

```text
idempotency_key = sha256(miner_hotkey + challenge_id + result)
```

Semantics:

- first cheap-valid submit appends event and returns new receipt
- duplicate returns existing receipt
- different assignment for same hotkey/challenge/result does not create a second scoring event

This preserves the review fix from `ed1b9ec`.

## Receipt Semantics

Two options:

### Option A — Fast received receipt

Ingress returns:

```json
{
  "status": "received",
  "terminal": false,
  "receipt_id": "..."
}
```

Verifier later marks:

```text
verified | rejected
```

Pros:

- smallest hot path
- no CNF generation/cache needed in ingress
- easiest to move off Railway quickly

Cons:

- miner does not get immediate `verified`
- invalid SAT witnesses may enter local log, but only after valid token/signature/shape and idempotency cap

### Option B — Inline SAT witness check

Ingress also verifies SAT assignment before ACK.

Requires ingress to have:

```text
PM seed secret
PM generation logic ported to Rust
CNF/assignment verifier
small LRU cache by challenge_id
```

Pros:

- invalid witnesses rejected before durable append
- receipt can be terminal `verified`

Cons:

- more code in ingress
- must exactly match Python PM generator
- bigger release risk

### Recommendation

Start with **Option A**.

Reason: it moves the latency/bill bottleneck without touching challenge generation. Add inline SAT check only after a parity test proves the Rust PM generator matches the Python one exactly.

## Hot Path Checks

Before local append:

```text
method/path allowed
Content-Length <= max_body_bytes when present
actual body <= max_body_bytes
valid JSON object
schema supported
submit_token present
submit_token HMAC verifies
submit_token not expired
submit_token hotkey/challenge/shape match body
hotkey signature verifies
assignment_b64 decodes
bitset byte length matches nvars
trailing unused bits are zero
idempotency key check/insert
```

Reject reasons are counted in rollups, not raw rows.

## Flusher / Drainer

A background task in the same binary or sidecar tails local events.

Loop:

```text
select unflushed events limit N
bulk insert/upsert into V2 Postgres
mark flushed_at_iso
publish periodic chunk hash/audit metadata
```

Batch policy:

```text
flush every 100-500ms or 500-2000 events
```

If Postgres is down:

```text
keep accepting until local backlog limit
when backlog limit exceeded: return 503 ingress_backlog_full
```

Backlog limits:

```text
max_unflushed_events
max_unflushed_bytes
max_oldest_unflushed_age_secs
```

This is backpressure without a managed queue.

## Verifier

Verifier can remain on Railway initially.

It reads from V2 Postgres after batched flush:

```text
received -> verifying -> verified/rejected
```

Later, verifier can run near ingress or on a cheap worker VM.

No need for queue service; Postgres status rows or local flushed events are enough for beta.

## Audit Publisher

To avoid permanent local-log dependence:

```text
periodically compress event chunks
publish chunk to Hippius/object storage
store chunk CID/hash in V2 audit metadata
retain local WAL for short window
```

Chunk cadence:

```text
every 1-5 minutes or 100k events
```

Event leaf covers:

```text
receipt_id
idempotency_key
miner_hotkey
challenge_id
result
assignment_sha256
received_at_iso
status
```

## DNS / Routing

Create a new endpoint first:

```text
https://v2-ingress.cathedral.computer
```

Then later point V2 submit path to it:

```text
https://v2-beta.cathedral.computer/v2/agents/submit-bitset
```

Avoid changing V1 submit routes.

Deployment options:

### Cheap VPS

```text
Cloudflare DNS/proxy -> Caddy/nginx -> cathedral-v2-ingress
```

Pros:

- predictable low bill
- no Worker queue bill
- direct control over disk/WAL

Cons:

- one region unless we add replicas
- ops responsibility: updates, monitoring, disk, backups

### Fly.io / similar small VM

Pros:

- easier deployment
- regions later
- persistent volume possible

Cons:

- can become less predictable than VPS

### Cloudflare Worker only

Not recommended for the user's stated constraints because durable queues/DO/R2 operations can become the bill surface.

A Worker can remain as a router/cache layer, but not as the durable submit queue.

## Cost Controls

### Keep event records tiny

Do not store request headers or raw JSON forever.

Store canonical fields:

```text
receipt_id
hotkey
challenge_id
assignment_sha256
assignment_b64 or compact raw bitset
received_at
signature hash/token id
```

Expected event size:

```text
~400-1000 bytes/event before compression
```

At 25k submits/min:

```text
~417/sec
~14-36 GB/day raw local event data depending record size
~3-12 GB/day compressed chunks typical
```

With short local retention and compressed audit chunks, this stays manageable.

### Do not store invalid spam

Invalid traffic becomes rollups:

```text
bucket + reason + count
```

not raw rows.

### Batch Postgres

Postgres gets:

```text
COPY/bulk insert batches
not one transaction per submit
```

### Retention

Suggested:

```text
local flushed WAL: 24-72h
V2 raw submit events: 7-30d during beta
verified summaries/audit bundles: longer
reject rollups: 7-30d
```

## Performance Targets

Server-side ingress target:

```text
p50 < 5ms
p95 < 20ms
p99 < 50ms
```

Public RTT target depends on miner geography.

Important truth:

```text
A single cheap origin cannot guarantee <50ms globally.
```

But removing Railway/Postgres from the ACK path should reduce current public submit latency substantially.

## Failure Modes

### DB down

```text
ingress keeps accepting until local backlog limit
then 503 ingress_backlog_full
```

### Disk near full

```text
stop accepting before corruption
503 ingress_disk_pressure
```

### verifier down

```text
receipts stay received
no scoring until verifier returns
```

### duplicate submit

```text
return existing receipt
```

### invalid token/signature/body

```text
reject immediately
rollup counter only
```

## Security Notes

- Submit token secret must be distinct from V1 and shared by ingress instances.
- Hotkey sr25519 verification must be byte-identical to Python canonicalization.
- Local WAL/SQLite volume must be backed up or periodically chunk-published.
- No private provider URLs in ingress logs, responses, docs, or audit bundles.
- Ingress should not perform arbitrary artifact fetches.

## Minimal Build Plan

### Phase 0 — Spec lock

- freeze `submit-bitset` canonical JSON/signature contract
- add golden test vectors for token/signature/bitset decode
- define local event JSON schema

### Phase 1 — Local ingress prototype

- Rust service with:
  - `/health/live`
  - `POST /v2/agents/submit-bitset`
  - `GET /v2/agents/submit-bitset/receipts/{id}`
- local SQLite WAL idempotency/event table
- no Postgres flush yet
- load test locally at 1k/s, 5k/s synthetic

### Phase 2 — Batch flusher

- bulk flush to V2 Postgres `v2_submit_events`
- mark flushed
- preserve existing V2 weights/audit behavior
- compare Railway endpoint vs ingress endpoint

### Phase 3 — Beta parallel endpoint

- deploy `v2-ingress.cathedral.computer`
- run miner E2E through ingress
- keep `v2-beta` Railway endpoint as fallback

### Phase 4 — Cutover V2 bitset submit only

- route `/v2/agents/submit-bitset` to ingress
- keep reads/weights/audit on Railway
- monitor:
  - ingress p50/p95/p99
  - backlog depth
  - disk usage
  - flush lag
  - V2 verified receipts

### Phase 5 — Optional inline verifier

Only if needed:

- port PM generator to Rust
- prove byte/ID/CNF parity against Python fixtures
- reject bad witnesses before local append

## What Not To Do

Do not:

- move V1 submit in this phase
- change challenge source
- introduce managed queues before measuring local WAL
- write every invalid attempt to Postgres
- store full DIMACS solution bodies for PM
- expose provider endpoints
- make artifact/proof fetch part of ACK path

## Decision Summary

Recommended next build:

```text
single-purpose Rust v2 bitset ingress
local SQLite WAL event log
batch flusher to V2 Postgres
Railway remains read/admin/verifier for now
```

This is the leanest path that removes Railway/Postgres from submit ACKs without adding a managed queue bill.
