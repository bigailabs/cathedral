# V2 Manifest Beta Runbook

Goal: run the new blob-backed V2 submit path beside the current subnet without touching current rewards, current validator weights, or the live payout DB.

## What V2 does

Flow:

```text
miner -> upload solution blob or publish externally
miner -> POST tiny signed manifest
V2 DB -> durable receipt/queue row
V2 worker -> fetch blob, verify SAT witness, finalize receipt
V2 shadow weights/audit -> read only, not chain-bound
```

Current V1 remains unchanged:

```text
/v1/agents/submit -> existing live subnet path
/v1/validator/weights/next -> existing live validator path
```

V2 endpoints:

```text
POST /v2/blobs/solutions                         # optional local/beta blob upload
POST /v2/agents/submit-manifest                  # tiny signed manifest
GET  /v2/agents/submit-manifest/receipts/{id}    # V2 receipt state
POST /v2/admin/verify/tick                       # admin manual worker tick
GET  /v2/validator/weights/next                  # V2 shadow signed vector only
GET  /v2/audit/epochs/{epoch}                    # signed audit bundle
```

## Safety isolation

Set a separate DB:

```text
CATHEDRAL_V2_DATABASE_URL=<separate Postgres/Supabase/Railway DB>
```

Do **not** point this at the live Cathedral production `DATABASE_URL`.

If `CATHEDRAL_V2_DATABASE_URL` is unset, V2 uses the same app Store. That is okay for local tests only.

## Required env for a separate beta service

```text
CATHEDRAL_SERVICE_ROLE=all
CATHEDRAL_V2_ENABLED=true
CATHEDRAL_V2_DATABASE_URL=<separate V2 DB>
CATHEDRAL_V2_ADMIN_TOKEN=<admin token>
CATHEDRAL_V2_PG_POOL_MIN=1
CATHEDRAL_V2_PG_POOL_MAX=4
```

V2 PM challenge config:

```text
CATHEDRAL_V2_PERMINER_ENABLED=true
CATHEDRAL_V2_PERMINER_SEED_SECRET=<same beta/test PM seed as challenge source>
CATHEDRAL_V2_PERMINER_ALLOTMENT_T1=128
CATHEDRAL_V2_PERMINER_ALLOTMENT_T2=1
CATHEDRAL_V2_PERMINER_NVARS_T1=400
CATHEDRAL_V2_PERMINER_NCLAUSES_T1=1704
CATHEDRAL_V2_PERMINER_METHOD_T1=biased
```

Optional local blob upload:

```text
CATHEDRAL_V2_BLOB_UPLOAD_ENABLED=true
CATHEDRAL_V2_BLOB_DIR=/data/cathedral-v2-blobs
CATHEDRAL_V2_BLOB_UPLOAD_MAX_BYTES=5000000
```

Optional worker:

```text
CATHEDRAL_V2_VERIFY_WORKER_ENABLED=true
CATHEDRAL_V2_VERIFY_BATCH_SIZE=8
CATHEDRAL_V2_VERIFY_INTERVAL_SECS=1
CATHEDRAL_V2_VERIFY_LOCK_SECS=120
CATHEDRAL_V2_VERIFY_MAX_BLOB_BYTES=5000000
```

Optional decentralized/external blob fetch gateway:

```text
CATHEDRAL_V2_CID_GATEWAY_TEMPLATE='https://gateway.example/fetch?cid={cid}'
```

This lets the worker fetch manifests with `hippius://...`, `ipfs://...`, etc. via an HTTP gateway. Miners may also submit direct `https://...` solution URLs.

## Hippius note

Current code supports:

- `local://...` blobs via `/v2/blobs/solutions`
- direct `https://...` blob URLs
- arbitrary CID schemes through `CATHEDRAL_V2_CID_GATEWAY_TEMPLATE`

A native Hippius write adapter still needs the Hippius endpoint/bucket/API shape. Keep credentials in Railway/Supabase/Cloudflare secret envs only; do not commit them.

## Local verification

```bash
python3 -m pytest -q scaffold/publisher/tests/test_solution_manifest_v2.py
python3 -m pytest -q scaffold/publisher/tests/test_submit_admission.py scaffold/publisher/tests/test_pm_submit_async.py scaffold/publisher/tests/test_solution_manifest_v2.py
python3 scripts/bench_solution_manifest_v2.py --total 200 --concurrency 64
```

Expected local benchmark shape:

```text
V1 full submit: many 429 submit_busy_retry under high concurrency
V2 manifest: 100% 2xx in local benchmark, low single-digit ms handler time
```

## Live-adjacent beta deploy checklist

1. Create a separate V2 DB.
2. Set `CATHEDRAL_V2_DATABASE_URL` to that DB only.
3. Enable V2 flags.
4. Deploy a separate beta Railway service or separate beta environment.
5. Smoke:
   - `POST /v2/blobs/solutions`
   - `POST /v2/agents/submit-manifest`
   - `POST /v2/admin/verify/tick`
   - `GET /v2/agents/submit-manifest/receipts/{id}`
   - `GET /v2/validator/weights/next`
   - `GET /v2/audit/epochs/{epoch}`
6. Confirm `/v1/validator/weights/next` and current live subnet endpoints are unchanged.

## Current limitation before miner migration

This is now technically end-to-end in shadow mode, but it is not yet the live reward path. Do not point current miners at it as a replacement until:

- V2 DB is production-sized and isolated
- blob storage/gateway is finalized
- worker drain is load tested
- V2 shadow weights match expectations across a real miner canary
- validators/auditors agree on audit bundle semantics
