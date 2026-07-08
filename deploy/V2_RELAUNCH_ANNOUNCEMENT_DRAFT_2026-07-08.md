# Cathedral V2 relaunch announcement draft

Status: draft. Do not post until the V2 edge gate is explicitly opened.

## Discord-ready note

Cathedral V2 mining is moving to the converged bitset flow.

Use:

```text
https://v2-beta.cathedral.computer
```

The miner loop is:

```text
1. GET  /v2/synthetic-boolean/per-miner/challenges?limit=10
2. GET  /v2/synthetic-boolean/per-miner/cnf?challenge_id=...&tier=...&seq=...
3. Read X-Cathedral-Submit-Token from the CNF response headers
4. POST /v2/agents/submit-bitset
5. Poll /v2/agents/submit-bitset/receipts/{receipt_id}
```

Important client change: challenge pages are now lazy descriptors. They do not
include per-item `submit_token` values. The submit token is minted when you
fetch the CNF and is returned in the `X-Cathedral-Submit-Token` response header.

Minimal client pattern:

```python
items = get_json("/v2/synthetic-boolean/per-miner/challenges?limit=10")["items"]
for item in items:
    cnf_resp = signed_get(
        "/v2/synthetic-boolean/per-miner/cnf",
        params={
            "challenge_id": item["challenge_id"],
            "tier": item["tier"],
            "seq": item["seq"],
        },
    )
    cnf = cnf_resp.text
    submit_token = cnf_resp.headers["X-Cathedral-Submit-Token"]
    assignment_b64 = solve_and_pack_bitset(cnf)
    signed_post("/v2/agents/submit-bitset", json={
        "schema": "cathedral.v2.submit_bitset.v1",
        "card_id": "synthetic_boolean_v1",
        "challenge_id": item["challenge_id"],
        "submit_token": submit_token,
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
    })
```

Receipt statuses:

- `received`: submit was admitted; async verification is pending.
- `verified`: witness verified and the solve is counted.
- `rejected`: terminal reject; inspect `rejection_reason`.

Operational notes:

- Keep page size at `limit=10` or lower.
- Honor `429` and `Retry-After`; that is normal backpressure, not a reason to
  hammer harder.
- V1 miner routes are not the relaunch path. Update to the V2 bitset flow.
- The reference smoke script is `scripts/v2_bitset_miner_e2e.py`.

Do not paste private keys, seeds, signatures, or submit tokens into Discord.

## Operator pre-post checklist

Before posting, verify:

```text
1. Fred explicitly says go.
2. Deploy the V2-open edge mode:
   cd deploy/v2-beta-router
   CLOUDFLARE_API_TOKEN=... npx wrangler deploy --var V2_GATE_MODE:open-v2
3. Non-canary V2 no longer returns v2_beta_staged_reopen.
4. Non-canary V1 miner routes return 410 v1_miner_path_retired.
5. /v2/verify/metrics shows exactly one verifier worker enabled.
6. A non-canary E2E submit reaches verified.
7. per_miner_solves receives exactly one row for that verified receipt.
8. /v1/validator/weights/next remains fresh.
9. Logs show no duplicate-ledger storm, stale-epoch rejects, or fail-closed boot errors.
10. If rollback is needed, redeploy the default staged mode:
    cd deploy/v2-beta-router
    CLOUDFLARE_API_TOKEN=... npx wrangler deploy
```
