# Review Packet — Agent 1 Security/Protocol

Date: 2026-06-30  
Target branch: `feat/solution-manifest-v2`  
Scope: V2 lean bitset ingress Phase 1

## What Changed

A standalone lean V2 bitset ingress was added. It moves V2 bitset submit ACKs off the full Railway/FastAPI/Postgres path in a Phase-1 form.

New code:

```text
scaffold/publisher/v2_lean_ingress.py
scripts/v2_lean_ingress_e2e.py
scaffold/publisher/tests/test_v2_lean_ingress.py
scaffold/publisher/tests/test_v2_bitset_ingress_contract.py
scripts/generate_v2_bitset_ingress_golden.py
deploy/golden/v2_bitset_ingress_golden.json
deploy/V2_BITSET_INGRESS_CONTRACT_2026-06-30.md
deploy/V2_LEAN_INGRESS_PLAN_2026-06-30.md
```

## Architecture Summary

Current beta path:

```text
miner -> v2-beta Railway/FastAPI -> Postgres -> verified receipt
```

New lean ingress Phase 1:

```text
miner -> lean ingress -> local SQLite WAL/idempotency -> received receipt
```

Important: Phase 1 does **not** do inline SAT witness verification and does **not** write to Postgres. It returns a non-terminal `received` receipt after token/signature/shape validation.

## Security/Protocol Properties To Review

Please verify:

### 1. Body cap before JSON parse

File:

```text
scaffold/publisher/v2_lean_ingress.py
```

Expected order:

```text
Content-Length cap
read body
actual byte cap
then JSON parse
```

Review concern:

- no `request.json()` before max body enforcement

### 2. Submit token validation

Reuses current Python contract:

```text
v2_bitset_submit.verify_submit_token
```

Token must bind:

```text
miner_hotkey
challenge_id
epoch
tier
seq
nvars
cnf_sha256
expires_at
```

Review concern:

- token secret missing fails closed
- forged/expired/mismatched token rejected before durable event insert

### 3. Hotkey signature validation

Lean ingress verifies:

```text
sr25519 signature over canonical_submit_bytes(normalized_body)
```

Review concern:

- normalized body matches contract doc
- signature is over normalized body, not raw JSON
- mutated token/body requires a new valid signature

### 4. Bitset shape validation

Lean ingress decodes:

```text
assignment_b64
```

and enforces:

```text
byte length == ceil(nvars/8)
unused trailing bits zero
```

Review concern:

- invalid assignment shape cannot enter local WAL
- SAT witness validity is intentionally deferred; it must not score until verifier marks verified later

### 5. Idempotency

Current SAT-only key:

```text
sha256("cathedral:v2:submit-bitset:\0" + canonical({miner_hotkey, challenge_id}))
```

Review concern:

- duplicate returns existing receipt
- no multiple rows for same miner/challenge
- this remains compatible with prior review finding from bitset scoring fix

### 6. Stored event safety

Accepted events store:

```text
assignment_b64
assignment_sha256
submit_token_id
signature
event_json/event_sha256
```

Review concern:

- no raw invalid spam stored
- no token secret stored
- full submit token is not stored in event_json, only submit_token_id

### 7. Receipt semantics

New Phase-1 receipt:

```text
status=received
terminal=false
open=true
weighted_score=0.0
```

Review concern:

- miner docs/release must not claim immediate verified status when routed through lean ingress
- no rewards/weights should consume these rows until batch flusher/verifier exists

## Tests Run

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
status=received
idempotent_replay=True
unflushed_events=1
```

## Known Intentional Limits

- no Postgres flusher yet
- no verifier yet
- no production routing yet
- no current rewards/weights impact
- no artifact/proof support here
- no provider integration
- Rust port not implemented in this commit because the harness lacks Rust toolchain; contract/golden vectors are ready for cross-language port

## Requested Review Verdict

Please answer:

1. Is Phase-1 admission safe to run as a beta-only ingress endpoint?
2. Are token/signature/idempotency semantics compatible with current V2 bitset contract?
3. Is `received` / `weighted_score=0` sufficient protection until verifier/flusher exists?
4. Any blocker before implementing batch flusher to V2 Postgres?
