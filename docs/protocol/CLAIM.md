# Wire format: `cathedral.polaris_agent_claim.v1`

Implements issue #2. A miner submits this JSON to the validator's `POST /v1/claim` endpoint with a `Authorization: Bearer <token>` header.

## Schema

```jsonc
{
  "type": "cathedral.polaris_agent_claim.v1",
  "version": "v1",
  "miner_hotkey": "5F...",
  "owner_wallet": "5G...",
  "work_unit": "card:eu-ai-act",
  "polaris_agent_id": "agt_01H1234567890ABCDEF",
  "polaris_deployment_id": "dep_01H...",       // optional
  "polaris_run_ids":      ["run_01H...", "..."], // optional
  "polaris_artifact_ids": ["art_01H...", "..."], // optional
  "submitted_at": "2026-05-10T18:00:00Z"
}
```

## Required fields

- `miner_hotkey` - the hotkey signing the claim
- `owner_wallet` - coldkey on file with Polaris; used to detect self-loop usage
- `work_unit` - `card:<id>` for regulatory cards (the validator extracts the id)
- `polaris_agent_id` - the agent record the validator pulls

## Validator behavior

1. Shape validation (`cathedral.types.PolarisAgentClaim`)
2. Insert into `claims` with `status='pending'` (idempotent on `(miner_hotkey, work_unit, polaris_agent_id)`)
3. Worker polls; calls `EvidenceCollector.collect(claim)`:
   1. Fetch manifest from Polaris by `polaris_agent_id`
   2. Verify manifest Ed25519 signature against the configured public key
   3. Reject if `manifest.polaris_agent_id != claim.polaris_agent_id`
   4. Fetch and verify each run, artifact, and usage record by ID
   5. Drop signature-failing run/artifact/usage records (count for telemetry)
   6. Filter usage by class (external only) and refunded/flagged flags
4. Decode the first verified artifact whose `report_hash` is JSON as a `Card`
5. Run preflight (`cathedral.cards.preflight`)
6. Run the scorer (`cathedral.cards.score_card`)
7. Persist `evidence_bundles` + `scores`; mark claim `verified`
8. Weight loop joins latest score per hotkey to metagraph uids and sets weights

## Failure modes

| Failure | Validator response |
|---|---|
| Missing required field | HTTP 400 |
| Bearer missing or wrong | HTTP 401 |
| Manifest fetch 404 | Reject claim (`collection: manifest missing`) |
| Manifest signature invalid | Reject claim (`collection: manifest signature invalid`) |
| Manifest agent_id mismatch | Reject claim (`collection: manifest mismatch`) |
| Run/artifact/usage signature invalid | Drop record only (claim continues) |
| Artifact bytes hash mismatch | Drop artifact only |
| No artifact decodes as a Card | Reject claim (`no_card_artifact`) |
| Card fails preflight | Reject claim (`preflight: <reason>`) |

## Self-loop detection

`cathedral.evidence.filter._is_self_loop` checks `consumer_wallet == owner_wallet`. The Polaris usage schema must include `consumer_wallet` for this filter to function.

## Canonical signature payload

Both Polaris (signer) and Cathedral (verifier) use:

```python
json.dumps({k: v for k, v in record if k != "signature"},
           sort_keys=True, separators=(",", ":"), default=str)
```

Any divergence breaks signature verification. See `cathedral.types.canonical_json_for_signing`.
