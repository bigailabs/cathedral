# V2 Bitset Ingress Contract

Date: 2026-06-30  
Status: Phase 0 contract draft for lean ingress implementation  
Scope: `POST /v2/agents/submit-bitset` only

## Purpose

This document freezes the current V2 bitset submit wire contract so a future lean ingress service can accept miner submits without routing the hot ACK path through Railway/FastAPI/Postgres.

The ingress must be byte-compatible with the current Python implementation for:

- submit token payload/signature
- canonical submit bytes
- hotkey signature message
- assignment bitset encoding
- idempotency key
- receipt semantics

## Endpoint

```text
POST /v2/agents/submit-bitset
```

Required headers:

```text
X-Cathedral-Hotkey: <miner ss58 hotkey>
X-Cathedral-Signature: <base64 sr25519 signature>
X-Cathedral-Submitted-At: <ISO-8601 UTC timestamp with ms, e.g. 2026-06-30T23:30:00.000Z>
Content-Type: application/json
```

Request body max:

```text
CATHEDRAL_V2_SUBMIT_BITSET_MAX_BODY_BYTES, default 16384
```

Body cap must be enforced before JSON parse.

## Submit Body Schema

Current schema:

```text
cathedral.v2.submit_bitset.v1
```

Canonical normalized body fields:

```json
{
  "assignment_b64": "...",
  "assignment_encoding": "bitset/v1",
  "card_id": "synthetic_boolean_v1",
  "challenge_id": "...",
  "miner_hotkey": "...",
  "schema": "cathedral.v2.submit_bitset.v1",
  "submit_token": "v1.<body_b64url>.<sig_b64url>",
  "submitted_at": "2026-06-30T23:30:00.000Z"
}
```

Notes:

- `miner_hotkey` may be omitted in miner JSON; normalized value comes from header.
- `submitted_at` may be omitted in miner JSON; normalized value comes from header.
- `card_id` may be omitted in miner JSON; normalized value is `synthetic_boolean_v1`.
- `schema` may be omitted in miner JSON; normalized value is `cathedral.v2.submit_bitset.v1`.
- canonical signature bytes are computed from the normalized body, not raw JSON.

## Canonical Submit Bytes

Python equivalent:

```python
json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
```

Where `body` contains exactly the normalized fields listed above.

Example order in canonical JSON:

```text
assignment_b64
assignment_encoding
card_id
challenge_id
miner_hotkey
schema
submit_token
submitted_at
```

No whitespace.

## Submit Token

Token schema:

```text
cathedral.v2.submit_token.v1
```

Token format:

```text
v1.<base64url_no_padding(json_payload_bytes)>.<base64url_no_padding(hmac_sha256)>
```

Payload canonicalization:

```python
json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
```

Payload fields:

```json
{
  "challenge_id": "...",
  "cnf_sha256": "64 lowercase hex chars",
  "epoch": 0,
  "expires_at": "2026-06-30T23:35:00.000Z",
  "miner_hotkey": "...",
  "nvars": 0,
  "schema": "cathedral.v2.submit_token.v1",
  "seq": 0,
  "tier": 1
}
```

Token HMAC:

```text
HMAC-SHA256(secret_utf8, token_payload_bytes)
```

Verifier requirements:

- token version is `v1`
- HMAC verifies with `CATHEDRAL_V2_SUBMIT_TOKEN_SECRET`
- schema matches
- `miner_hotkey` equals header hotkey
- `challenge_id` equals normalized body challenge id
- `expires_at` is not in the past
- `epoch`, `tier`, `seq`, `nvars` parse as integers
- `cnf_sha256` is lowercase 64-char hex

## Assignment Encoding

Encoding name:

```text
bitset/v1
```

Wire field:

```text
assignment_b64 = standard base64, with padding allowed/expected
```

Bit order:

```text
Variable i is stored at bit (i - 1).
Least significant bit first within each byte.
1 = positive/true literal i
0 = negative/false literal -i
```

Expected byte length:

```text
ceil(nvars / 8)
```

Unused trailing bits in the last byte must be zero.

Example for 10 vars:

```text
assignment: [1, -2, 3, -4, 5, -6, 7, -8, 9, -10]
bits:       1, 0, 1, 0, 1, 0, 1, 0, 1, 0
bytes hex:  55 01
base64:     VQE=
```

## Hotkey Signature

Signature input:

```text
canonical_submit_bytes(normalized_submit_body)
```

Signature algorithm:

```text
sr25519, verified against X-Cathedral-Hotkey
```

Wire field:

```text
X-Cathedral-Signature = standard base64 signature bytes
```

## Required Validation Order

Current lean-ingress order:

```text
1. route/method check
2. per-IP rate-limit check, if enabled
3. Content-Length <= max, if present
4. read body with max-byte guard
5. JSON parse
6. normalize body from JSON + headers
7. verify submitted_at timestamp skew
8. verify hotkey signature over canonical submit bytes
9. exact replay lookup by miner_hotkey + challenge_id + submit_token_id
10. if replay exists and is not rejected: return existing receipt
11. otherwise verify submit token HMAC + token/body/header binding + expiry
12. decode assignment_b64 and enforce byte length/trailing bits
13. idempotency insert/update under local SQLite WAL
14. return receipt
```

Why replay lookup happens before token-HMAC verification:

- it only returns an existing non-rejected row
- it requires a valid fresh hotkey signature
- it requires the same submit-token hash as the existing row
- it does not admit new work after token expiry

New unique submissions still require a valid unexpired HMAC submit token.

## Optional Inline SAT Verification

Current Railway V2 endpoint verifies the SAT witness inline before DB write.

Lean ingress Phase 1 may instead return `received` before SAT verification if the event is token/signature/shape valid and accepted into the local SQLite WAL.

Durability caveat: the Phase-1 SQLite setting uses `synchronous=NORMAL`, which is a practical low-cost WAL durability/performance tradeoff for beta testing. It is not a replicated durable queue. Production scoring still requires the flusher/audit pipeline.

If inline verification is enabled, ingress must also prove exact parity for:

- PM challenge ID generation
- PM CNF generation
- CNF SHA-256
- tier shape/weight
- assignment verifier

Recommendation: do not port inline SAT verification until Python/Rust golden fixtures prove PM parity.

## Idempotency

Current Python bitset idempotency key:

```python
sha256(
  b"cathedral:v2:submit-bitset:\0" +
  json.dumps(
    {"miner_hotkey": miner_hotkey, "challenge_id": challenge_id},
    sort_keys=True,
    separators=(",", ":"),
  ).encode("utf-8")
).hexdigest()
```

Current key does not include `result` because this endpoint is SAT-only. If a generic artifact/proof path is added, use:

```text
miner_hotkey + challenge_id + result
```

Semantics:

- first valid submit creates a receipt
- duplicate submit returns existing receipt
- no second scoring row for the same miner/challenge

## Receipt Contract

Current verified receipt schema:

```text
cathedral.v2.submit_bitset_receipt.v1
```

Current Railway path returns terminal verified receipts:

```json
{
  "schema": "cathedral.v2.submit_bitset_receipt.v1",
  "shadow": true,
  "status": "verified",
  "open": false,
  "terminal": true,
  "receipt_id": "...",
  "receipt_url": "/v2/agents/submit-bitset/receipts/...",
  "miner_hotkey": "...",
  "challenge_id": "...",
  "card_id": "synthetic_boolean_v1",
  "epoch": 0,
  "tier": 1,
  "seq": 0,
  "assignment_encoding": "bitset/v1",
  "assignment_sha256": "...",
  "cnf_sha256": "...",
  "eligibility_status": "unknown_beta",
  "submitted_at": "...",
  "received_at": "...",
  "verified_at": "...",
  "weighted_score": 1.0,
  "answer_hash": "...",
  "idempotent_replay": false
}
```

Lean ingress Phase 1 may return non-terminal received receipts:

```json
{
  "schema": "cathedral.v2.submit_bitset_receipt.v1",
  "shadow": true,
  "status": "received",
  "open": true,
  "terminal": false,
  "receipt_id": "...",
  "receipt_url": "/v2/agents/submit-bitset/receipts/...",
  "miner_hotkey": "...",
  "challenge_id": "...",
  "card_id": "synthetic_boolean_v1",
  "epoch": 0,
  "tier": 1,
  "seq": 0,
  "assignment_encoding": "bitset/v1",
  "assignment_sha256": "...",
  "cnf_sha256": "...",
  "eligibility_status": "unknown_beta",
  "submitted_at": "...",
  "received_at": "...",
  "weighted_score": 0.0,
  "idempotent_replay": false
}
```

If we change from immediate `verified` to `received`, miner docs must explicitly say verification is async.

## Error Reasons To Preserve

Important miner-safe errors:

```text
submit_bitset_body_too_large
invalid_content_length
invalid_json_submit_bitset
unsupported_submit_bitset_schema
unsupported_card_id
hotkey_mismatch
submitted_at_mismatch
invalid_challenge_id
missing_submit_token
unsupported_assignment_encoding
invalid_assignment_b64
invalid_submit_token
submit_token_hotkey_mismatch
submit_token_challenge_mismatch
submit_token_expired
bitset_size_mismatch
bitset_trailing_bits_nonzero
invalid hotkey signature
```

Verifier-only errors if inline verification is enabled:

```text
challenge_id_not_in_miner_set
submit_token_cnf_mismatch
submit_token_shape_mismatch
solution_incomplete_assignment
solution_non_integer_literal
solution_variable_out_of_range
solution_contradictory_assignment
solution_unsatisfied
witness_check_failed
```

## Golden Vectors

Golden vectors should include:

- deterministic dev hotkey
- fake token secret only
- token payload canonical bytes SHA-256
- full submit token
- normalized submit body
- canonical submit bytes SHA-256
- sr25519 signature
- assignment raw hex/base64
- idempotency key

Generated file target:

```text
deploy/golden/v2_bitset_ingress_golden.json
```

The vector must not include live secrets or real miner keys.
