# V2 Manifest Beta Status — 2026-06-29

## Branch / commit

```text
branch: feat/solution-manifest-v2
commit: d393cbab Add isolated V2 manifest submit pipeline
```

Push status:

```text
git push failed: GitHub HTTPS auth unavailable in this shell
```

Railway deploy status:

```text
railway status -> Unauthorized
```

No live deploy was performed.

## Built

- V2 manifest intake:
  - `POST /v2/agents/submit-manifest`
  - `GET /v2/agents/submit-manifest/receipts/{receipt_id}`
- Optional beta blob upload:
  - `POST /v2/blobs/solutions`
- V2 verifier:
  - background worker gated by `CATHEDRAL_V2_VERIFY_WORKER_ENABLED`
  - manual admin tick: `POST /v2/admin/verify/tick`
- V2 shadow scoring:
  - `GET /v2/validator/weights/next`
- V2 audit bundle:
  - `GET /v2/audit/epochs/{epoch}`
- Isolated DB support:
  - `CATHEDRAL_V2_DATABASE_URL`
  - `CATHEDRAL_V2_DB_PATH`
  - uses `Store(..., prefer_env_database_url=False)` so a separate explicit V2 DB does not silently fall back to live `DATABASE_URL`
- Blob backends:
  - local content-addressed blobs: `local://...`
  - direct `https://...` fetch
  - arbitrary CID schemes via `CATHEDRAL_V2_CID_GATEWAY_TEMPLATE`

## Files in commit

```text
deploy/V2_MANIFEST_BETA_RUNBOOK.md
scaffold/publisher/app.py
scaffold/publisher/blob_store.py
scaffold/publisher/solution_manifest.py
scaffold/publisher/store.py
scaffold/publisher/tests/test_solution_manifest_v2.py
scaffold/publisher/v2_pipeline.py
scripts/bench_solution_manifest_v2.py
```

## Test results

```text
python3 -m pytest -q scaffold/publisher/tests/test_solution_manifest_v2.py \
  scaffold/publisher/tests/test_submit_admission.py \
  scaffold/publisher/tests/test_pm_submit_async.py

57 passed
```

Local benchmark after V2 additions:

```text
python3 scripts/bench_solution_manifest_v2.py --total 200 --concurrency 64

V1 full submit:
  200 requests, 89 accepted, 111 submit_busy_retry
  accepted throughput ~69.8/sec
  p95 latency ~409ms

V2 manifest:
  200 requests, 200 accepted, 0 submit_busy_retry
  accepted throughput ~289/sec
  p95 latency ~4.4ms
```

## Current production safety

This branch does **not** change current rewards unless V2 is explicitly enabled and miners are pointed at `/v2/...`.

Current live paths remain separate:

```text
/v1/agents/submit
/v1/validator/weights/next
```

V2 shadow weights are under:

```text
/v2/validator/weights/next
```

## Deploy blockers

1. GitHub push auth is missing in this shell.
2. Railway CLI is unauthorized in this shell.
3. A separate V2 DB URL must be created/provided.
4. Hippius native upload needs endpoint/bucket/API shape; current code can fetch through HTTP/gateway and supports local beta upload.
5. Secrets pasted in chat should be rotated before production use.

## Recommended live-adjacent env

```text
CATHEDRAL_SERVICE_ROLE=all
CATHEDRAL_V2_ENABLED=true
CATHEDRAL_V2_DATABASE_URL=<separate V2 DB, not live DATABASE_URL>
CATHEDRAL_V2_ADMIN_TOKEN=<admin token>
CATHEDRAL_V2_PG_POOL_MIN=1
CATHEDRAL_V2_PG_POOL_MAX=4
CATHEDRAL_V2_PERMINER_ENABLED=true
CATHEDRAL_V2_PERMINER_SEED_SECRET=<beta PM seed>
CATHEDRAL_V2_PERMINER_ALLOTMENT_T1=128
CATHEDRAL_V2_PERMINER_ALLOTMENT_T2=1
CATHEDRAL_V2_PERMINER_NVARS_T1=400
CATHEDRAL_V2_PERMINER_NCLAUSES_T1=1704
CATHEDRAL_V2_PERMINER_METHOD_T1=biased
CATHEDRAL_V2_BLOB_UPLOAD_ENABLED=true
CATHEDRAL_V2_BLOB_DIR=/data/cathedral-v2-blobs
CATHEDRAL_V2_VERIFY_WORKER_ENABLED=true
CATHEDRAL_V2_VERIFY_BATCH_SIZE=8
CATHEDRAL_V2_VERIFY_INTERVAL_SECS=1
CATHEDRAL_V2_VERIFY_MAX_BLOB_BYTES=5000000
```

If using Hippius/IPFS gateway fetch:

```text
CATHEDRAL_V2_CID_GATEWAY_TEMPLATE='https://gateway.example/fetch?cid={cid}'
```
