# v4.0.0-rc.6-v2-bitset-submit-beta

Prerelease candidate for Cathedral V2 PM-native bitset submit beta.

Code commit deployed to V2 beta: `ed1b9ec Fix V2 bitset scoring review findings`  
Release notes commit: `0f7d409 Document V2 bitset beta verification`

## Summary

This release adds an isolated V2 submit path for PM SAT that avoids full DIMACS solution bodies in the hot path.

```text
miner fetches V2 per-miner challenge + submit_token
miner solves CNF locally
miner submits tiny signed bitset assignment
V2 verifies token/signature/assignment cheaply
V2 records a verified shadow event
V2 shadow weights include the verified event
```

No current V1 rewards, payouts, or validator weights are affected.

## New beta endpoints

```text
POST /v2/agents/submit-bitset
GET  /v2/agents/submit-bitset/receipts/{receipt_id}
```

Existing V2 per-miner challenge endpoints now expose bitset-submit metadata when enabled:

```text
GET /v2/synthetic-boolean/per-miner/challenges
GET /v2/synthetic-boolean/per-miner/cnf
```

## New beta env vars

```text
CATHEDRAL_V2_SUBMIT_BITSET_ENABLED=true
CATHEDRAL_V2_SUBMIT_TOKEN_SECRET=<secret>
CATHEDRAL_V2_SUBMIT_TOKEN_TTL_SECS=300
```

Keep V2 isolated as before:

```text
CATHEDRAL_V2_ENABLED=true
CATHEDRAL_V2_DATABASE_URL=<separate-v2-db>
CATHEDRAL_V2_PERMINER_ENABLED=1
```

## Anti-cheat properties

V2 bitset submit admits only cheap-valid submissions:

- submit token is HMAC-bound to hotkey, challenge, epoch, tier, seq, nvars, CNF hash, and expiry
- hotkey signs the canonical bitset submit body
- assignment must be `bitset/v1` with exact byte size and zero trailing bits
- assignment is evaluated against the miner's deterministic CNF before a durable event is written
- payload is inline and signed, so there is no post-submit mutation path
- scoring time uses Cathedral receive time, not miner clock

Invalid shape/token/signature/witness submissions are rejected before a `v2_submit_events` row is written.

Review-fix notes:

- V2 shadow scoring dedupes by `(miner_hotkey, challenge_id)` across manifest and bitset sources, preferring bitset events so one PM challenge cannot double-count.
- V2 audit bundles include both manifest receipts and bitset events with source-tagged status counts.
- `/v2/agents/submit-bitset` is covered by the submit hot-path gate and has an explicit small body cap before JSON parsing.

## Load-test finding carried forward

Earlier V2 live-adjacent probes found:

- full-body V1 shadow storage overloaded the beta target around ~1% sample
- metadata-only live shadow held at 100% live submit traffic

Conclusion: PM V2 should use tiny bitset submits, not full-body solution ingestion.

## New scripts

```text
scripts/v2_bitset_miner_e2e.py
```

Runs the miner-facing bitset flow against V2 beta and verifies shadow weights.

## Verification

Review/fix loop completed for the reported scoring/audit/backpressure findings.

Local checks:

```text
py_compile passed
65 passed
```

Live beta E2E after review fixes:

```text
python3 scripts/v2_bitset_miner_e2e.py --base https://v2-beta.cathedral.computer --limit 1 --solver cadical153

E2E_OK
receipt_id=5134b754-acce-4645-a73e-75d902e4ee80
status=verified
shadow_weight=1.0
raw_score=1.0
admit_ms=193.0
```

Live audit check:

```text
/v2/audit/epochs/495232
count=1
status_counts={"bitset:verified": 1}
has_receipt=true
```

Live health after deploy:

```text
V1 submit: 200
V1 api_ready: 200
V1 weights: 200
V2 live: 200
V2 shadow metadata probe: last_1m.count=0
```

Config verified on `cathedral-v2-beta`:

```text
CATHEDRAL_V2_DATABASE_URL: set
CATHEDRAL_V2_SUBMIT_TOKEN_SECRET: set
CATHEDRAL_V2_SUBMIT_BITSET_ENABLED=true
CATHEDRAL_V2_SUBMIT_TOKEN_TTL_SECS=300
```

## Non-goals

- No V1 reward changes
- No V1 validator weight changes
- No live chain RPC in submit admission
- No full DIMACS body storage in the PM V2 hot path

## Rotation reminder

Cloudflare/Railway/Hippius tokens pasted during development should be rotated.
