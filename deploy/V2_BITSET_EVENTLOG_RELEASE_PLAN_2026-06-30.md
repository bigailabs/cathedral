# V2 Bitset Submit + Event Log Release Plan

Date: 2026-06-30  
Branch: `feat/solution-manifest-v2`

## TL;DR

Move PM submit toward this stable shape:

```text
miner fetches assigned challenge + submit_token
miner solves locally
miner POSTs tiny signed bitset assignment
Cathedral cheaply validates token/signature/eligibility/assignment
only cheap-valid submissions are admitted
verified events feed shadow weights first, real weights later
```

For PM SAT, do **not** send/store full DIMACS solution bodies in the hot path.

## Current Findings

### Full-body shadow mirror

Path tested:

```text
live V1 submit -> Worker mirror -> V2 FastAPI -> blob write + DB row
```

Result:

- worked as a mirror
- overloaded V2 beta around ~1% main-submit sample
- bottleneck: full solution body handling + per-submit DB/blob writes

### Metadata-only live mirror

Path tested:

```text
live V1 submit -> Worker mirror -> V2 metadata-only endpoint
```

Result:

- held at 100% live submit traffic
- confirmed current live traffic around tens of thousands submits/min
- no scoring/rewards/weights affected
- still too much raw row growth for long-term use

Conclusion:

```text
The full solution body path is the problem.
Tiny metadata/submit admission is viable, but must avoid unbounded raw DB spam.
```

## Target Architecture

### PM-native V2 hot path

```text
GET  /v2/synthetic-boolean/per-miner/challenges
  -> returns challenge metadata + submit_token + required assignment encoding

POST /v2/agents/submit-bitset
  -> tiny signed bitset assignment
  -> cheap validation
  -> durable receipt/event only if cheap-valid

GET  /v2/agents/submit-bitset/receipts/{receipt_id}
  -> receipt/status
```

### Why bitset, not blob, for PM SAT?

For PM SAT, the solution can be represented as a fixed-size assignment bitset.

Example:

```text
400 vars -> 400 bits -> ~50 bytes raw -> ~68 bytes base64
```

So the PM submit payload should be tiny and inline.

Blob-backed solution manifests remain useful for future tasks where outputs are large, but PM SAT should not store full DIMACS bodies.

## New Endpoint Contract

### `POST /v2/agents/submit-bitset`

Headers:

```text
X-Cathedral-Hotkey: <ss58 hotkey>
X-Cathedral-Signature: <base64 sr25519 signature>
X-Cathedral-Submitted-At: <ISO timestamp>
```

JSON body:

```json
{
  "schema": "cathedral.v2.submit_bitset.v1",
  "card_id": "synthetic_boolean_v1",
  "challenge_id": "pm-t1-e...",
  "submit_token": "...",
  "assignment_encoding": "bitset/v1",
  "assignment_b64": "..."
}
```

Canonical signed object includes:

```text
schema
card_id
challenge_id
submit_token
assignment_encoding
assignment_b64
miner_hotkey
submitted_at
```

## Cheap-Valid Admission Rules

A submission is admitted only if all cheap checks pass.

### 1. Token check

`submit_token` is minted during challenge fetch.

Token HMAC covers:

```text
hotkey
challenge_id
epoch
tier
seq
nvars
cnf_sha256
expires_at
```

Reject before DB write if:

- missing token
- malformed token
- expired token
- wrong hotkey
- wrong challenge
- wrong CNF hash

### 2. Signature check

Verify sr25519 signature over canonical submit-bitset body.

Reject before DB write if invalid.

### 3. Cached eligibility check

Use cached registered-hotkey snapshot. No live chain RPC in submit path.

Modes:

- beta/shadow: record `eligibility_status`, but do not affect rewards
- real weights: reject or exclude ineligible hotkeys before scoring

### 4. Assignment shape check

Reject before DB write if:

- encoding is not `bitset/v1`
- base64 invalid
- decoded bit count does not match `nvars`
- trailing bits are non-zero when applicable

### 5. Attempt cap

For each `(hotkey, challenge_id)`:

- admit first cheap-valid attempt
- duplicate exact attempt returns same receipt
- invalid/extra attempts increment rollup counters, not raw rows forever

### 6. Cheap SAT verification

Evaluate assignment against the deterministic CNF.

This is not solving. It is just:

```text
for each clause: at least one literal evaluates true
```

Reject before durable scoring event if unsatisfied.

## Anti-Cheat Guarantees

### Payload cannot be updated after submit

For PM bitset:

```text
assignment_b64 is inline and signed
```

No later mutable payload exists.

For future blob tasks:

```text
signed manifest includes blob cid + sha256 + bytes
verifier checks fetched bytes match signed hash
```

### Miner timestamp cannot win races

Scoring uses Cathedral receive time:

```text
received_at_iso / edge_received_at_iso
```

not miner-provided `submitted_at`.

### Garbage does not become durable spam

Reject most invalid traffic before raw DB rows.

Store only:

- valid receipts/events
- bounded aggregate rejection rollups
- optional sampled invalid examples with TTL

## Data Model

### `v2_submit_events`

Admitted cheap-valid PM submit events.

Columns:

```text
id TEXT PRIMARY KEY
idempotency_key TEXT UNIQUE
miner_hotkey TEXT
challenge_id TEXT
card_id TEXT
epoch BIGINT
tier INTEGER
seq INTEGER
cnf_sha256 TEXT
assignment_encoding TEXT
assignment_sha256 TEXT
assignment_b64 TEXT
status TEXT                 -- received | verified | rejected
rejection_reason TEXT
eligibility_status TEXT     -- eligible | ineligible | unknown_beta
received_at_iso TEXT
submitted_at TEXT
verified_at_iso TEXT
signature TEXT
submit_token_id TEXT
weighted_score DOUBLE PRECISION
verifier_details_hash TEXT
```

### `v2_submit_reject_rollups`

Bounded counters, not raw invalid rows.

Columns:

```text
bucket_iso TEXT
reason TEXT
source TEXT
count BIGINT
sample_hotkey_hash TEXT optional
```

### Retention

- raw metadata-only probe rows: short TTL / cleanup job
- rejected rollups: keep 7–30 days
- accepted verified events: keep for audit window
- audit bundles: publish compressed snapshots

## Existing Endpoint Cleanup

### Keep, but mark beta/default-off

```text
POST /v2/blobs/solutions
POST /v2/agents/submit-manifest
GET  /v2/agents/submit-manifest/receipts/{id}
POST /v2/shadow/v1/agents/submit
GET  /v2/shadow/v1/agents/submit/metrics
POST /v2/shadow/v1/agents/submit/meta
GET  /v2/shadow/v1/agents/submit/meta/metrics
```

### Add clear env gates

```text
CATHEDRAL_V2_ENABLED
CATHEDRAL_V2_SUBMIT_BITSET_ENABLED
CATHEDRAL_V2_SHADOW_V1_ENABLED
CATHEDRAL_V2_BLOB_UPLOAD_ENABLED
CATHEDRAL_V2_MANIFEST_SUBMIT_ENABLED
```

### Live probes

Default production config should be:

```text
SHADOW_V1_MIRROR_ENABLED=false
SHADOW_V1_MIRROR_SAMPLE_PERCENT=0
```

Metadata mirror is only for short load probes.

## Implementation Phases

## Phase 0 — Stop Growth / Cleanup

- [ ] Keep live mirror disabled after current test.
- [ ] Add TTL/admin cleanup for `v2_shadow_v1_submit_meta`.
- [ ] Add dashboard labels distinguishing:
  - full-body shadow
  - metadata-only shadow
  - bitset submit events
- [ ] Document all V2 beta endpoints as non-scoring.

## Phase 1 — Tokened Challenge Fetch

- [ ] Extend V2 per-miner challenge response with:
  - `submit_token`
  - `submit_token_expires_at`
  - `assignment_encoding="bitset/v1"`
  - `nvars`
  - `cnf_sha256`
- [ ] Add token mint/verify helpers.
- [ ] Use `CATHEDRAL_V2_SUBMIT_TOKEN_SECRET`, separate from V1 CNF token secret.

## Phase 2 — `submit-bitset` Admission

- [ ] Add `solution_manifest`/new module helpers for canonical bitset submit bytes.
- [ ] Add `POST /v2/agents/submit-bitset`.
- [ ] Add `GET /v2/agents/submit-bitset/receipts/{id}`.
- [ ] Decode bitset with existing `decode_bitset_assignment` logic.
- [ ] Verify token, signature, shape, cached eligibility, attempt cap.
- [ ] Evaluate SAT assignment against deterministic PM CNF.
- [ ] Insert only admitted valid events into `v2_submit_events`.
- [ ] Return `202` for new valid event, `200` for idempotent replay.

## Phase 3 — Shadow Weights from Bitset Events

- [ ] Build V2 shadow weight vector from `v2_submit_events` only.
- [ ] Ensure V1 weights/rewards are untouched.
- [ ] Publish V2 audit bundle with event ids and hashes.

## Phase 4 — Load Test

Synthetic:

- [ ] hammer `submit-bitset` with valid tiny payloads
- [ ] measure p50/p95/p99, RPS, DB rows/sec, rejection reasons

Live-adjacent:

- [ ] run real miner E2E against beta
- [ ] compare to V1 full submit behavior

Targets:

```text
server-side admission p95 < 50ms under expected load
public RTT may be higher depending on Railway/edge path
no V1 health degradation
no V1 scoring/weights impact
```

## Phase 5 — Event Log / Chunk Path

If Railway RTT remains too high for user-visible ACK, move the admission receiver to an ingress/event-store layer.

Implementation options:

```text
Cloudflare Worker + Queue/R2
or API Gateway + queue
or NATS/Kafka-style ingress
or direct event-store HTTP append
```

Core requirement:

```text
POST -> append immutable event -> fast receipt
backend verifier/scorer consumes later
```

Hippius use:

- store compressed audit/event chunks
- do not create one tiny object per submit if avoidable
- bundle events and publish chunk CIDs

## Release Plan

Cut next prerelease after Phase 0–2 pass tests:

```text
v4.0.0-rc.6-v2-bitset-submit-beta
```

Release notes must say:

- V2 remains isolated/shadow-only
- V2 submit-bitset does not affect current rewards or V1 weights
- Full-body shadow mirror found bottleneck at V2 body/storage path
- Metadata-only live probe held at 100% but is not meant for permanent raw retention
- PM-native V2 direction is tiny signed bitset + cheap validation

## Release Blockers

- [ ] live shadow probes disabled or explicitly low-sample/default-off
- [ ] no secrets committed
- [ ] tests pass
- [ ] endpoint docs updated
- [ ] beta Railway deployed from clean commit
- [ ] GitHub prerelease points to correct commit
- [ ] pasted tokens rotated

## Recommended Immediate Next Build

Implement Phase 0–2 now:

```text
cleanup + tokened V2 challenge + submit-bitset endpoint + tests
```

Do not build more full-body submit routes until this path is tested.
