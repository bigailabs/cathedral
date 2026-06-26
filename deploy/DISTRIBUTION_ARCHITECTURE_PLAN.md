# Cathedral Distribution Architecture Plan

Status: draft for review.

Goal: replace request-driven public APIs with a publish-once, distribute-many
data plane that can support SAT, secure compute, and future agent trace flows
without every miner poll waking the application or database.

## Core Principle

Cathedral should not serve live state by recomputing it for every public
request.

Target shape:

```text
publisher / verifier / scorer
  -> materialized snapshots and immutable artifacts
  -> CDN/object distribution
  -> cheap reads by miners, dashboards, validators, and agents

submit ingress
  -> authenticated fast receipt
  -> queue
  -> async verification
  -> scoring ledger
  -> next published snapshot
```

The current Cloudflare Worker is a compatibility router. It is useful, but it is
not the final scaling layer because every compatible `api.cathedral.computer`
request still consumes Worker request quota.

## Non-Negotiables

- Reads must be cheap, cacheable, versioned, and mostly static.
- Submits must stay authenticated, rate-limited, idempotent, and non-cacheable.
- Validators must read deterministic signed scoring state.
- Miners must know which endpoint to use and why a request was rejected.
- Agent traces must be treated as artifact streams, not JSON blobs on hot APIs.
- No lane may bypass the verifier/scorer path to influence emissions directly.
- Old `api.cathedral.computer` can remain a compatibility surface, but it must
  not be the primary high-volume distribution path.

## Current State vs Target

Already present:

- split Railway roles and runbook: `deploy/ROLE_SPLIT_RUNBOOK.md`
- compatibility Worker: `deploy/edge-router`
- edge diagnosis, route-map, and soak checks
- live SAT scoring endpoints and per-miner/private endpoints
- submit backpressure and PM visibility work in the publisher path

Target, not yet built:

- object-backed published snapshots
- signed latest pointers that bind artifact hashes
- immutable CNF/artifact distribution
- transactional submit outbox
- broadcast/subscription notification layer
- agent trace artifact plane

This plan is not a replacement for the current split endpoints. It is the next
architecture after the split endpoint/edge-router compatibility layer.

## Trust And Data Invariants

These are load-bearing. A phase is not complete unless these hold.

1. **Single writer with fencing.**
   The publisher sequencer must be protected by leader election or a database
   advisory lock with a fencing token. A stale publisher cannot publish sequence
   `N+1` after losing leadership.

2. **Monotonic client state.**
   Clients keep the highest accepted sequence and reject any signed pointer with
   `sequence <= last_seen_sequence`, except for an explicit operator rollback
   mode that changes the publisher generation ID.

3. **Signed pointer binds bytes.**
   A latest pointer signature must cover the URL, sha256, size, content type,
   and kind of every referenced artifact. Clients must verify artifact bytes
   against the signed hash before solving, scoring, or displaying earnings.

4. **Snapshots are derived, not authoritative.**
   Object storage is a cache/distribution layer. Every snapshot must be a
   deterministic function of `ledger@sequence` and must be regenerable after
   bucket loss or cache corruption.

5. **Pointer flips last.**
   The publisher writes artifacts first, verifies them by HEAD/GET, then writes
   the latest pointer. Artifact 404s must use `no-store` or very short negative
   caching, and clients retry missing artifacts with bounded backoff.

6. **Receipt write and verification enqueue are atomic.**
   Submit ingress uses a transactional outbox or equivalent. Returning `202`
   means both the durable receipt and the verification job are recoverable.

7. **Verifier-authenticated ledger writes.**
   Only verifier identities may append verified ledger rows. The scorer rejects
   rows without verifier provenance and stable verifier version metadata.

8. **Public artifact writes are irreversible.**
   Redaction, size limits, malware checks, and secret scanning happen before a
   public object write. Once a public immutable artifact is distributed, assume
   it cannot be recalled.

9. **Trust root and key lifecycle are explicit.**
   Clients verify publisher pointers and scoring snapshots against a pinned
   Cathedral publisher key set. The key set is served from the existing JWKS
   endpoint and published as a signed artifact. A key is usable only if it is
   present in the current trusted key set, not expired, and not revoked.

10. **Key rotation is overlapping.**
    New publisher keys are introduced before use, overlap with old keys for a
    defined grace window, and are removed only after active clients and signed
    snapshots have aged out. Emergency revocation publishes a new key-set
    sequence; clients reject artifacts signed by revoked key IDs.

11. **Generation changes are authenticated transitions.**
    `publisher_generation_id` changes require a signed transition artifact.
    Clients accept a lower sequence only when the transition is signed by a
    trusted operator/publisher key, references the previous generation and last
    valid sequence, names the new generation, and declares the rollback/rebuild
    reason. A random new generation ID is not enough.

## Components

### 1. Publisher Sequencer

Private service. Owns ordered state transitions and publication.

Responsibilities:

- observe challenge inventory, assignments, submit receipts, verifier results,
  scoring ledger, and chain/weight state
- assign monotonic sequence numbers
- write immutable artifacts
- write small mutable "latest pointer" documents
- emit broadcast events after durable writes
- never serve high-volume public traffic directly

Required invariant:

```text
event sequence N must point to already-written artifacts
```

Consumers can miss broadcasts and still recover by reading the latest pointer.
The sequencer writes a `publisher_generation_id` into every signed pointer so a
manual rollback or disaster recovery rebuild can be distinguished from a stale
edge replay.

Generation transitions are ordered artifacts:

```json
{
  "kind": "cathedral.publisher_generation_transition.v1",
  "from_generation_id": "2026-06-25-prod-a",
  "from_sequence": 495233,
  "to_generation_id": "2026-06-25-prod-b",
  "to_start_sequence": 1,
  "reason": "operator_rollback",
  "created_at": "2026-06-25T13:10:00Z",
  "signature": "..."
}
```

Clients reject lower sequences unless the generation transition is valid against
the trusted key set.

### 2. Artifact Store

Use an object store/CDN path for prepared data. R2 or S3-compatible storage is
fine; the important property is immutable, content-addressed artifacts plus
small latest pointers.

Public-ish SAT artifacts:

```text
/sat/latest.json
/sat/sequences/{sequence}/board.json
/sat/sequences/{sequence}/weights.json
/sat/sequences/{sequence}/leaderboard-24h.json
/sat/sequences/{sequence}/challenges/{challenge_id}/manifest.json
/sat/sequences/{sequence}/challenges/{challenge_id}/cnf.dimacs.zst
```

Private or miner-scoped artifacts:

```text
/sat/private/{assignment_epoch}/{opaque_mailbox_id}/index.json
/sat/private/{assignment_epoch}/{opaque_mailbox_id}/{assignment_id}.json
/sat/private/{assignment_epoch}/{opaque_mailbox_id}/{assignment_id}.cnf.zst
```

Private assignment mailboxes are not public CDN discovery paths. A miner first
authenticates to the submit/read service with the normal hotkey signature and
receives either:

- an opaque mailbox ID plus short-lived signed artifact URLs, or
- a direct authenticated mailbox response with private cache disabled.

Do not derive mailbox paths from public hotkeys or coldkeys. Public keys are
guessable from chain state. Private assignment artifacts must be salted/blinded
per assignment so a public CNF URL, ETag, or content hash does not reveal which
miner received which work.

Agent/trace artifacts:

```text
/agent-traces/{trace_epoch}/{trace_hash}/manifest.json
/agent-traces/{trace_epoch}/{trace_hash}/events.ndjson.zst
/agent-traces/{trace_epoch}/{trace_hash}/artifacts/{artifact_hash}
/agent-traces/{trace_epoch}/{trace_hash}/verdict.json
```

Compute artifacts:

```text
/compute/offers/{offer_id}/manifest.json
/compute/evidence/{evidence_id}/attestation.json
/compute/evidence/{evidence_id}/verifier-result.json
/compute/receipts/{receipt_id}.json
```

Do not put raw secrets, raw hotkey private material, UID keys, or unpublished
exploit details in public buckets. Agent/audit artifacts default private.

Lifecycle policy:

- public SAT board/leaderboard snapshots: keep at least 7 days
- public immutable CNFs: keep at least as long as any scoring dispute window
- private assignment artifacts: keep at least scoring window plus dispute window
- raw private agent traces: keep by operator policy, default private
- public redacted agent datasets: immutable once exported

Deletes are allowed only after the published pointer no longer references the
artifact and the dispute window has passed.
Ledger and verifier metadata retention must be at least as long as any promised
snapshot regeneration window.

### 3. Latest Pointers

Small cacheable files tell clients where the current state lives.

Example:

```json
{
  "kind": "cathedral.sat.latest.v1",
  "sequence": 495233,
  "publisher_generation_id": "2026-06-25-prod-a",
  "window_start": "2026-06-24T12:40:00Z",
  "window_end": "2026-06-25T12:40:00Z",
  "artifacts": [
    {
      "kind": "board",
      "url": "/sat/sequences/495233/board.json",
      "sha256": "...",
      "size_bytes": 12345,
      "content_type": "application/json"
    },
    {
      "kind": "weights",
      "url": "/sat/sequences/495233/weights.json",
      "sha256": "...",
      "size_bytes": 23456,
      "content_type": "application/json"
    },
    {
      "kind": "leaderboard_24h",
      "url": "/sat/sequences/495233/leaderboard-24h.json",
      "sha256": "...",
      "size_bytes": 34567,
      "content_type": "application/json"
    }
  ],
  "created_at": "2026-06-25T12:40:02Z",
  "signature": "..."
}
```

Use `sequence` as the immutable artifact namespace. Do not overload `epoch` to
mean both time bucket and sequence. If time buckets are needed for storage
layout, use them as directories only and keep the sequence in the path, for
example `/sat/dt=2026-06-25/sequences/495233/board.json`.

Caching policy:

- latest pointers: short TTL, ETag, stale-while-revalidate
- immutable artifacts: long TTL, immutable
- signed scoring snapshots: explicit sequence and signature
- clients use `If-None-Match` and back off when unchanged

### 4. Broadcast / Subscription Layer

Broadcast is for notification, not source of truth.

Supported modes, in order:

1. Server-Sent Events for simple clients.
2. WebSocket for richer miner agents and dashboards.
3. Queue/webhook fanout later for heavy operator integrations.

Broadcast payloads stay tiny:

```json
{
  "kind": "cathedral.event.v1",
  "sequence": 495233,
  "lane": "sat",
  "type": "board_published",
  "latest_url": "/sat/latest.json",
  "artifact_hash": "sha256:..."
}
```

Rules:

- events are hints only
- clients resolve state through known canonical latest pointer paths, not
  arbitrary event-provided URLs
- the signed pointer and artifact hashes are the trust root, not the event frame
- broadcasts never carry full CNFs, proofs, traces, or secrets
- every event is recoverable from latest pointers
- missed events are acceptable
- clients reconnect with `Last-Event-ID` or last seen sequence
- if broadcast is down, polling latest pointers still works

Public broadcast channels announce public sequence changes. Private assignment
notifications, if added, must be scoped to an authenticated miner connection and
must still contain only hints. The miner resolves private state through the
authenticated mailbox path.

### 5. Submit Ingress

Submits are the write path and must remain a real service.

The submit service should accept:

- SAT answers
- per-miner assignment answers
- audit witnesses
- agent trace manifests
- compute offer/evidence receipts

It should return quickly:

```text
202 accepted_for_verification
400 malformed
401/403 auth failure
409 duplicate/idempotent replay
422 syntactically valid but rejected
429 retryable rate/backpressure
503 hard outage
```

Do not block submit requests on heavy verification. The ingress path should:

1. authenticate signature, nonce, and hotkey
2. validate schema and size
3. write a durable receipt
4. write an outbox row for verification in the same database transaction
5. return receipt ID

A separate dispatcher drains the outbox into the verification queue. A sweeper
retries stuck outbox rows. A returned receipt is never dependent on an in-memory
queue write.

Verification workers must be idempotent. Re-dispatching the same outbox row may
retry verification, but it must not double-append the same verified ledger row.

### 6. Submit Rate Limits

Rate limits are required because submit is non-cacheable and abuse can hurt
miners and validators.

Use layered limits:

```text
per-hotkey token bucket
per-coldkey token bucket when mapping is available
per-challenge duplicate/idempotency gate
per-assignment gate for private work
global submit concurrency
global queue depth circuit breaker
per-IP soft abuse limit, never the main miner identity limit
```

Identity-aware limits run before global queue rejection whenever the request is
authenticated enough to identify the miner. Otherwise one abusive identity can
fill the global queue and force honest miners into `503`.

Enforcement point:

- Cloudflare/WAF/DDoS handles obvious volumetric and malformed public abuse.
- Cathedral submit ingress enforces miner identity, nonce, idempotency, and
  queue admission because those require signature and challenge context.
- Do not rely on IP as the primary miner limiter; NAT/VPN/shared hosts make it
  noisy.

Recommended initial semantics:

- successful duplicate answer: `409 duplicate`, include original receipt ID
- same hotkey/challenge rapid retry: `429 rate_limited`
- system overloaded but request can be retried: `429 submit_busy_retry`
- queue too deep: `503 submit_queue_saturated`
- malformed DIMACS/private format: `422 solution_missing_status` or exact reason

Important distinction:

```text
rate_limited = miner/client behavior
submit_busy_retry = Cathedral backpressure
submit_queue_saturated = Cathedral cannot accept safely right now
```

Miners should not be punished for `submit_busy_retry` or `503` when the receipt
was not accepted.

Every retryable response must include `Retry-After`. The body must include a
stable machine-readable reason. The submit metrics stream must count
Cathedral-side backpressure separately from miner-side rate limits so scoring
and operator dashboards do not confuse them.

Idempotency and replay keys:

```text
nonce key: hotkey + nonce, TTL >= max clock skew + submit retry window
SAT answer idempotency: hotkey + challenge_id + answer_hash
per-assignment idempotency: hotkey + assignment_id + answer_hash
duplicate solve gate: challenge_id + normalized_answer_hash, when global uniqueness is required
```

Different answers to the same challenge are not the same idempotent request.
They may be rejected by policy, but must not be collapsed into the same receipt.

Legitimate epoch-rollover bursts are expected. The limiter must support short
burst capacity per identity and then refill steadily, rather than enforcing a
flat per-second ceiling that punishes synchronized assignment releases.

### 7. Verification Workers

Verification happens after ingress.

SAT verifier:

- assignment satisfies exact CNF hash
- challenge is active/eligible for the miner
- answer is unique where uniqueness is required
- score unit is computed from tier, private bonus, and policy

Agent/audit verifier:

- manifest is complete
- target commit and harness are pinned
- replay runs deterministically
- witness/proof decodes to the claimed behavior
- verdict is accepted/rejected with reason

Compute verifier:

- signed offer is valid
- hardware profile is supported
- TEE/TDX/GPU evidence is fresh and cryptographically checked when required
- provider/health/revenue receipts are bound to capacity ID

Verification output is a ledger row plus a published verdict artifact.

### 8. Scoring Publisher

The scorer reads verified ledger rows and publishes signed scoring snapshots.

For SAT:

```text
eligible unique verified solve units over trailing 24h
tier 1 = 1 unit
tier 2 = 3 units
private assignment bonus = policy multiplier/additive unit
normalized by current top miner units
```

Every scoring snapshot must include:

- ledger sequence high-watermark
- `[window_start, window_end]`
- denominator/top-miner weighted units
- tier weights and PM/private bonus policy version
- included receipt high-watermark or receipt inclusion summary
- signature over all of the above plus artifact hashes

The same ledger slice and policy version must reproduce the same weights.

Do not compute miner earnings from live activity samples. Live activity is only
debug telemetry.

For future lanes:

- compute lane must score verified usable capacity and receipts, not claims
- agent lane must score verifier-approved artifacts, not prose or raw traces
- model/distillation value should be downstream product value, not direct miner
  scoring until deterministic gates exist

## Read Surfaces

### Preferred Miner Reads

```text
GET https://read.cathedral.computer/sat/latest.json
GET https://read.cathedral.computer/sat/sequences/{sequence}/board.json
GET https://submit.cathedral.computer/v1/synthetic-boolean/per-miner/challenges
```

Public reads should come from static snapshots. Private assignment discovery
stays authenticated unless and until we have a non-guessable signed URL mailbox
scheme with acceptable leakage properties.

### Compatibility Reads

`https://api.cathedral.computer` can keep old JSON endpoints by reading the same
materialized snapshots. It should not become the canonical distribution path.

### Dashboard Reads

The dashboard should read published scoring snapshots and artifact pointers,
not scrape live submit receipts for earnings truth.

## Agent Data Flows

Future agent work needs the same shape as SAT but with larger artifacts.

Agent event stream:

```text
agent starts task
tool call event
artifact produced
witness/replay submitted
verifier verdict
scoring snapshot includes accepted artifact
distillation exporter packages accepted/rejected trace
```

Each trace must be:

- content-addressed
- chunked
- signed by miner/agent where possible
- bound to task ID, target commit, environment image, tool versions, and replay
  manifest
- private by default
- redacted before public export

The hot API should only carry manifests and receipt IDs. Raw logs, tool traces,
screenshots, large proofs, and solver artifacts belong in artifact storage.
Large agent uploads should use multipart/chunked upload with a manifest hash.
The submit API accepts the manifest and returns a receipt; it should not carry
the full trace payload inline.

## Migration Plan

### Phase 0: Keep System Alive

Current state:

- `api.cathedral.computer` compatibility Worker is healthy after Workers paid
  plan upgrade.
- `read.cathedral.computer` is the preferred read endpoint.
- `submit.cathedral.computer` is the preferred submit/private endpoint.

Keep this while building the real distribution plane.

Acceptance:

```text
node deploy/edge-router/diagnose.mjs -> healthy
node deploy/edge-router/route-map.mjs -> pass
node deploy/edge-router/soak.mjs -> pass
```

Minimum soak gate:

- 30 minutes
- no 5xx on hot paths
- no Cloudflare Worker 1027
- cached read p95 under 250ms
- submit p95 under 750ms for syntactically valid requests
- overloaded submit returns fast 429/503 with `Retry-After`

### Phase 1: Publish SAT Snapshots

Add a publisher job that writes:

- `sat/latest.json`
- board snapshot
- weights snapshot
- leaderboard/payment snapshot
- active challenge manifests

Keep old API endpoints, but serve them from snapshots where possible.

Acceptance:

- snapshot has sequence, created_at, hash, signature
- signed pointer includes hashes, sizes, and types for all artifacts
- recomputing from `ledger@sequence` reproduces the snapshot bytes
- clients reject stale sequence rollback
- old `/active-challenges` matches published board snapshot
- old `/validator/weights/next` matches published weights snapshot
- dashboard can render from snapshots only
- continuous consistency check proves old API vs snapshot max lag under 10s

### Phase 2: Publish CNF Artifacts

Move CNF bodies to immutable artifact objects.

Acceptance:

- challenge manifest includes CNF hash, size, compression, and URL
- clients verify CNF hash before solving
- CDN/object read does not hit app database
- old CNF endpoint redirects or proxies only during transition
- manifest hash chains back to a signed latest pointer or signed assignment
  manifest

### Phase 3: Miner Assignment Mailboxes

Publish private assignment indexes by miner identity hash.

Acceptance:

- miner mailbox discovery is authenticated or uses non-guessable short-lived
  signed URLs
- assignment ID binds to miner hotkey/coldkey, epoch, CNF hash, and expiry
- old `/per-miner/challenges` remains compatible
- no public listing leaks other miners' assignment supply
- private CNF URL/content hash does not reveal public challenge reuse

### Phase 4: Submit Queue

Split submit into durable fast ingress plus async verification.

Acceptance:

- submit p95 under 500ms for accepted receipts under normal load
- overload returns fast 429/503, not 20s gateway timeouts
- duplicate submissions are idempotent
- verification workers can lag without losing accepted receipts
- scoring uses verified ledger only
- receipt write and outbox enqueue are atomic
- verifier-authenticated ledger provenance exists
- `Retry-After` and stable rejection reasons are present

### Phase 5: Broadcast

Add SSE first.

Acceptance:

- miners subscribe to board/assignment updates
- events carry sequence and latest pointer only
- clients can recover by reading latest pointers
- broadcaster outage does not stop solving
- target polling cadence falls to one pointer revalidation every 30-60s when
  connected to broadcast

### Phase 6: Agent Artifact Plane

Add agent trace manifests and private artifact ingestion.

Acceptance:

- trace upload is chunked and content-addressed
- submit API accepts trace manifest/receipt, not giant raw payloads
- verifier emits accepted/rejected verdict artifacts
- distillation exporter can build private datasets from trace manifests

## What Not To Do

- Do not make Cloudflare Worker the permanent high-volume request path.
- Do not push every miner poll through Python/FastAPI/Postgres.
- Do not make submit endpoints cacheable.
- Do not put large agent traces, proofs, or CNFs inside dashboard JSON.
- Do not score from recent activity samples.
- Do not let broadcast become the source of truth.
- Do not remove rate limits from submit just because reads become cheap.

## Immediate Next Engineering Slice

1. Add `sat/latest.json` snapshot generation to the worker/publisher loop.
2. Add a read endpoint that serves the exact same snapshot payload.
3. Add ETag and sequence metadata.
4. Update the dashboard to prefer snapshots for earnings/payment truth.
5. Add submit receipt status endpoint by receipt ID.
6. Add a submit queue depth/backpressure metric.
7. Add transactional outbox for accepted submit receipts.
8. Add a small SSE endpoint that only emits `{sequence, latest_url}` after
   snapshots are durable.

This slice moves Cathedral away from hot database reads without changing SAT
scoring or miner submit semantics.

## Current Compatibility Slice

The first mergeable slice keeps existing miner and validator URLs working while
adding the scalable read contract:

- `GET /sat/latest.json` returns a signed latest pointer with sequence, hashes,
  sizes, and artifact URLs.
- `GET /sat/sequences/{sequence}/board.json` returns the current board snapshot
  for a recent sequence persisted in the shared publisher database.
- `GET /sat/sequences/{sequence}/weights.json` returns the current signed
  weights snapshot for a recent sequence persisted in the shared publisher
  database.
- `GET /sat/events` streams SSE hints containing only sequence and latest URL.
- `GET /v1/agents/receipts/{receipt_id}` returns durable submit status without
  scraping recent activity.

This is deliberately a transition layer: it does not yet publish historical
immutable object storage, CNF artifacts, transactional outbox rows, or async
verification workers. Sequence artifact responses serve byte-exact,
hash-verifiable JSON from a bounded shared-database cache, but they use bounded
cache headers rather than claiming permanent origin availability.
