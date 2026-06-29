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
CATHEDRAL_EXTERNAL_SCORES_BASE_WEIGHT=1.0
CATHEDRAL_EXTERNAL_SCORES_WEIGHT=1.0
CATHEDRAL_EXTERNAL_SCORES_WINDOW_SECS=3600
```

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
