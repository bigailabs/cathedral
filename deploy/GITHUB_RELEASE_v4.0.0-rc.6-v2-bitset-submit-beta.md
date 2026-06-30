# v4.0.0-rc.6-v2-bitset-submit-beta

Prerelease candidate for Cathedral V2 PM-native bitset submit beta.

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

## Release gates

Do not promote this prerelease until all pass:

- [ ] review/fix loop complete, including Claude review
- [ ] focused V2 tests pass
- [ ] broader submit regression tests pass
- [ ] V2 beta deployed from clean commit
- [ ] live V2 bitset miner E2E passes
- [ ] V2 shadow weights include E2E miner
- [ ] live V1 submit/weights remain healthy
- [ ] shadow probes default disabled unless explicitly canarying

## Non-goals

- No V1 reward changes
- No V1 validator weight changes
- No live chain RPC in submit admission
- No full DIMACS body storage in the PM V2 hot path

## Rotation reminder

Cloudflare/Railway/Hippius tokens pasted during development should be rotated.
