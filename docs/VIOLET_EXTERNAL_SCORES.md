# Violet external scores

Cathedral validators do **not** change for Violet integration. Violet posts scores to the Cathedral publisher; the publisher blends/signs the final vector served at `GET /v1/validator/weights/next`.

```txt
Violet scorer -> Cathedral publisher -> Cathedral-signed vector -> Cathedral validator -> set_weights()
```

## Publisher env

```bash
# Enable POST /v1/external-scores/violet
CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_TOKEN=<shared bearer token>
# Optional raw-body HMAC verification.
CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET=<shared hmac secret>

# Include stored Violet scores in weight composition.
CATHEDRAL_EXTERNAL_SCORES_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_SOURCE=violet_audio
CATHEDRAL_EXTERNAL_SCORES_MODE=blend          # blend | external_primary
CATHEDRAL_EXTERNAL_SCORES_FRACTION=0.1         # external share (0..1); wins over the weights below
CATHEDRAL_EXTERNAL_SCORES_WINDOW_SECS=3600
# Legacy weights (used only if FRACTION is unset); effective external share is
# still hard-capped at MAX_FRACTION.
CATHEDRAL_EXTERNAL_SCORES_BASE_WEIGHT=1.0
CATHEDRAL_EXTERNAL_SCORES_WEIGHT=1.0
```

## Real-money safety (set these before enabling on mainnet)

The blend feeds the **real** signed vector, so treat it as real money:

- **Set the share explicitly.** `CATHEDRAL_EXTERNAL_SCORES_FRACTION=0.10` is 10% external / 90% base. If you leave it unset, the legacy `BASE_WEIGHT`/`WEIGHT` default to **1.0/1.0 = 50%** — almost never what you want. The effective share is hard-capped at `CATHEDRAL_EXTERNAL_SCORES_MAX_FRACTION` (default `0.5`).
- **Registration gate is on by default.** `CATHEDRAL_EXTERNAL_SCORES_REQUIRE_REGISTERED=1` (default) makes external scores pay **only hotkeys in the fresh metagraph snapshot**, and **fails closed** (does not blend) if the snapshot is unavailable. Also run `CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS=filter` for the same guarantee on the final vector.
- **The token is a real-money credential.** `CATHEDRAL_EXTERNAL_SCORES_TOKEN` (and the optional HMAC secret) let the holder direct real weight — hold it tightly and rotate it. Unauthenticated ingest is refused whenever the blend is live.
- **`external_primary` = 100% external.** It requires an explicit `CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM=true`; without it the service falls back to the capped blend.
- **Consensus:** only our validator blends; other validators do not, so the external slice can be shaved toward the network median. Confirm our stake share before relying on the full 10%.
- Observability: `GET /v1/validator/weights/next` policy metadata / the weights status surface the `effective_external_share`, `require_registered`, and score counts.

## Report shape

```json
{
  "source": "violet_audio",
  "mechanism": "violet_audio",
  "netuid": 49,
  "epoch": 12345,
  "generated_at": "2026-06-29T12:00:00.000Z",
  "scores": [
    {
      "uid": 48,
      "miner_hotkey": "5Gx...",
      "score": 0.82,
      "quality": 0.88,
      "validity": 1.0,
      "tasks_scored": 12,
      "confidence": 0.91
    }
  ]
}
```

Scores must be finite `0.0..1.0`. Cathedral stores the report and never lets it set weights directly; it only contributes to the next Cathedral-signed vector.
