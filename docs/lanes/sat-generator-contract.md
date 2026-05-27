# SAT Generator Contract

This is the v1 contract between the Cathedral publisher and a private
SAT challenge generator.

The generator is a black box. Miners never connect to it. Validators
never connect to it. Cathedral asks for challenge material, imports it,
serves it through Cathedral's token-gated CNF path, verifies answers,
and signs score rows.

Cathedral verifies. The generator generates. Neither exposes what the
other should not know.

## Responsibilities

### Generator Owns

- create DIMACS CNFs
- classify each CNF by tier, kind, and CNF class
- solve each generated CNF before pool admission
- confirm the generated witness satisfies the CNF
- discard the witness before pool admission
- keep private audit metadata
- maintain a ready pool
- lease CNFs only to Cathedral

Private generator metadata includes:

- generator run id
- generator commit
- CBMC or solver version
- generation parameters
- self-test status
- generation runtime

The witness is never stored and never exposed through the API.

### Cathedral Owns

- authenticate to the generator
- lease CNFs from the generator
- fetch CNF bytes once
- verify the advertised SHA-256
- parse DIMACS metadata
- assign public Cathedral challenge ids
- import challenges into `lane_challenges`
- serve CNFs to miners through Cathedral fetch tokens
- verify miner DIMACS answers
- sign score rows and weight vectors

Cathedral owns the miner-visible `challenge_id`. The generator returns
`generator_run_id` only for private audit.

## Public Metadata

Allowed in Cathedral public challenge metadata:

- `challenge_id`
- `tier`
- `kind`
- `cnf_class`
- `num_vars`
- `num_clauses`
- `cnf_sha256`
- time limit

Private:

- generator endpoint
- generator run id
- generator commit
- generator parameters
- generated witness
- raw CNF except through Cathedral's token-gated fetch path

`kind` and `cnf_class` are public because miners need to choose the
right solver family. A random 3SAT instance and a structured SHA-256
preimage instance should not look identical to the miner.

## Lease Flow

Cathedral leases from a pre-generated pool. The generator must not run
CBMC or another expensive generator synchronously in the HTTP request
path.

```text
Cathedral -> Generator: POST /v1/challenges/lease
Generator -> Cathedral: lease id, metadata, CNF URL, SHA-256
Cathedral -> Generator: GET cnf_url
Cathedral: verify SHA-256
Cathedral: parse and durably import challenge
Cathedral -> Generator: POST /v1/challenges/lease/{lease_id}/confirm
```

If Cathedral cannot import the challenge, it releases the lease. If it
crashes, the generator auto-releases the lease after the TTL.

## Endpoints

### `POST /v1/challenges/lease`

Headers:

```text
Authorization: Bearer <cathedral-private-token>
Idempotency-Key: <uuid>
```

Request:

```json
{
  "family": "synthetic_boolean_v1",
  "tier": 1,
  "kind": "sha256_preimage_v1"
}
```

Response:

```json
{
  "lease_id": "lease_01hx...",
  "expires_at": "2026-05-26T21:30:00Z",
  "generator_run_id": "gen_01hx...",
  "tier": 1,
  "kind": "sha256_preimage_v1",
  "cnf_class": "structured_crypto",
  "cnf_url": "https://generator.internal/artifacts/gen_01hx/cnf",
  "cnf_sha256": "0123456789abcdef...",
  "num_vars": 1234,
  "num_clauses": 5678,
  "byte_size": 999999
}
```

The generator returns the same live lease when it receives the same
`Idempotency-Key` again inside its retry window. Five minutes is the
default retry window.

The response does not inline CNF text.

### `POST /v1/challenges/lease/{lease_id}/confirm`

Headers:

```text
Authorization: Bearer <cathedral-private-token>
```

Request:

```json
{
  "cathedral_challenge_id": "sat-t1-20260526-0001",
  "cnf_sha256_witnessed": "0123456789abcdef..."
}
```

The generator marks the lease consumed only if
`cnf_sha256_witnessed` matches the leased hash. If it does not match,
the generator returns `409`.

### `POST /v1/challenges/lease/{lease_id}/release`

Headers:

```text
Authorization: Bearer <cathedral-private-token>
```

Releases an unconfirmed lease back to the pool.

### `GET /v1/pool/health`

Headers:

```text
Authorization: Bearer <cathedral-private-token>
```

Response:

```json
{
  "families": [
    {
      "family": "synthetic_boolean_v1",
      "tier": 1,
      "kind": "sha256_preimage_v1",
      "ready_depth": 12,
      "leased_depth": 1,
      "oldest_lease_age_seconds": 48,
      "generation_rate_per_hour": 6.5
    }
  ]
}
```

Cathedral uses this for buffer top-up. Operators use it for capacity
planning.

## Failure Rules

- If the generator is down, Cathedral cannot launch new generated
  challenges.
- Cathedral should keep a small local pre-leased buffer per tier.
- Cathedral should keep the operator file-backed import path as the
  break-glass fallback.
- A lease is confirmed only after Cathedral has durably imported the
  challenge.
- A leased CNF that is not confirmed before `expires_at` returns to the
  pool.
- Generator auth is Cathedral-only. If a miner can lease CNFs, the pool
  is compromised.

## Auth

The lease, release, confirm, artifact, and health endpoints are private
Cathedral-to-generator endpoints.

Acceptable v1 choices:

- private bearer token rotated by deployment
- HMAC over method, path, body hash, timestamp, and nonce
- mTLS between Cathedral and the generator

Do not reuse miner hotkey signatures for this boundary. The generator
is an internal service, not a miner-facing service.
