# V2 Bitset Submit Beta — Ready for Testing

Status: **ready for miner testing as preparation only**  
Base URL: `https://v2-beta.cathedral.computer`

## Important

This beta is preparation for the future V2 submit path.

It does **not** change current miner rewards, payouts, or V1 validator weights.

Current production miners can keep using V1 exactly as before.

## What miners can test

The new V2 beta path submits a tiny signed bitset assignment instead of a full DIMACS solution body.

Flow:

```text
GET  /v2/synthetic-boolean/per-miner/challenges
GET  /v2/synthetic-boolean/per-miner/cnf
read X-Cathedral-Submit-Token from the CNF response headers
POST /v2/agents/submit-bitset
GET  /v2/agents/submit-bitset/receipts/{receipt_id}
GET  /v2/validator/weights/next
```

Under the converged V2 profile, challenge pages are lazy descriptors and do not
carry per-item `submit_token` values. Fetch the CNF first, then submit using the
`X-Cathedral-Submit-Token` header returned with that exact CNF.

## Quick test

From the Cathedral repo:

```bash
python3 scripts/v2_bitset_miner_e2e.py \
  --base https://v2-beta.cathedral.computer \
  --limit 1
```

Expected success:

```text
E2E_OK
status=verified
shadow_weight=1.0
```

## What this proves

This proves the V2 protocol pieces:

- miner receives a token-bound challenge
- miner reads the submit token from the CNF response header
- miner solves locally
- miner submits a compact bitset assignment
- Cathedral verifies token, signature, assignment shape, and SAT witness
- V2 shadow receipt becomes `verified`
- V2 shadow weights include the verified solve

## What this does not do

This does **not**:

- affect V1 weights
- affect current rewards
- pay miners
- require miners to migrate now
- use private challenge-provider endpoints
- expose provider infrastructure

## Why miners should test it

This is preparation for future releases where Cathedral can ask miners to submit more work per epoch without sending large DIMACS bodies through the hot path.

The intended future direction is:

```text
tiny signed submits now
more items per epoch later
artifact/proof support later
real rewards only after explicit eligibility/scoring rollout
```

## Current limitations

- V2 beta accepts valid signatures but is still shadow-only.
- Real reward eligibility gating is not enabled here.
- Artifact/proof manifests are design-stage only, not part of this test path.
- Provider API integration is not enabled.

## Support notes

If the E2E fails, capture:

```text
base URL
hotkey
challenge_id
receipt_id if present
status code and response body
```

Do not send private keys, seeds, or tokens.
