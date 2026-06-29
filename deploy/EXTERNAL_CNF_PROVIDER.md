# External Active CNF Provider

Cathedral can source miner-facing SAT challenges from a private upstream that
publishes one active DIMACS CNF at a time. Miners still interact only with
Cathedral: they fetch Cathedral challenge rows/CNFs and submit solutions to
Cathedral.

## Supported Provider Shape

- `HEAD /cnf`: read active CNF metadata without downloading the body.
- `GET /cnf`: download the active DIMACS CNF.
- `POST /sol`: submit the accepted solution for the active CNF.
- Metadata headers:
  - `X-Bitwuzla-Iter`
  - `X-Bitwuzla-CNF-SHA256`
  - `X-Bitwuzla-Num-Vars`
  - `X-Bitwuzla-Num-Clauses`
  - `ETag`
  - `Last-Modified`
  - `Accept-Ranges`
- Downloads:
  - `identity`
  - `gzip`
  - byte ranges may be advertised by the upstream but Cathedral does not depend
    on them.
- Solution uploads:
  - SAT: DIMACS solution text.
  - UNSAT/LRAT: adapter can format the upstream headers, but Cathedral's current
    miner verifier only accepts SAT witnesses.

## Env Flags

- `CATHEDRAL_EXTERNAL_CNF_ENABLED=1`
- `CATHEDRAL_EXTERNAL_CNF_BASE_URL=https://...`
- `CATHEDRAL_EXTERNAL_CNF_PROVIDER_ID=private`
- `CATHEDRAL_EXTERNAL_CNF_TIER=9`
- `CATHEDRAL_EXTERNAL_CNF_TARGET_ACTIVE=1`
- `CATHEDRAL_EXTERNAL_CNF_FORWARD_SOLUTIONS=1`

Optional:

- `CATHEDRAL_EXTERNAL_CNF_CNF_PATH=/cnf`
- `CATHEDRAL_EXTERNAL_CNF_SOL_PATH=/sol`
- `CATHEDRAL_EXTERNAL_CNF_TOKEN=...`
- `CATHEDRAL_EXTERNAL_CNF_TIMEOUT_SECONDS=10`
- `CATHEDRAL_EXTERNAL_CNF_DOWNLOAD_TIMEOUT_SECONDS=120`
- `CATHEDRAL_EXTERNAL_CNF_SUBMIT_TIMEOUT_SECONDS=30`
- `CATHEDRAL_EXTERNAL_CNF_CACHE_TTL_SECONDS=15`

## Flow

1. Refill polls provider metadata.
2. If the active provider `(iter, sha256)` is new, Cathedral downloads and
   hash-verifies the CNF.
3. Cathedral mints exactly one active local challenge row for that upstream CNF.
4. Older external-CNF rows in the same tier are retired and their stored CNF body
   is cleared.
5. When Cathedral accepts a rank-1 solution, it can forward that solution to the
   upstream `/sol` endpoint.
