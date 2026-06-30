# Cathedral V2 Miner E2E Beta Guide

This guide lets miners test the isolated V2 manifest lane end-to-end:

```text
fetch challenge -> fetch CNF -> solve locally -> upload solution blob -> submit signed manifest -> verified receipt -> shadow weight/audit
```

V2 beta is **shadow-only**. It does **not** affect current production V1 rewards, payouts, or validator weights.

## Beta URL

Use the clean beta URL:

```text
https://v2-beta.cathedral.computer
```

If DNS is still propagating, wait and retry. Operators should avoid publishing Railway URLs to miners.

## Do miners need a Hippius account?

No.

For this beta, miners can use Cathedral's beta blob upload endpoint:

```text
POST /v2/blobs/solutions
```

That returns a `local://solution/...` CID which is accepted by the V2 verifier. Later, miners may also self-host blobs on Hippius/IPFS/R2/HTTPS and submit that CID/URL, but it is not required for the beta E2E.

## Install

From the Cathedral repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install requests bittensor-wallet python-sat
```

## Run an ephemeral-key smoke test

This creates a temporary hotkey, solves one assigned beta challenge, and verifies the result in the V2 shadow lane:

```bash
python3 scripts/v2_miner_e2e.py --base https://v2-beta.cathedral.computer
```

Expected final line:

```text
E2E_OK {"audit_count":..., "hotkey":"...", "receipt_id":"...", "status":"verified", "weight":1.0}
```

## Run with your miner hotkey

Use one of these environment variables.

Mnemonic/dev URI style:

```bash
export CATHEDRAL_MINER_URI='//YourDevMiner'
python3 scripts/v2_miner_e2e.py --base https://v2-beta.cathedral.computer
```

Seed hex style:

```bash
export CATHEDRAL_MINER_SEED_HEX='<64 hex chars, no spaces>'
python3 scripts/v2_miner_e2e.py --base https://v2-beta.cathedral.computer
```

Do not paste production key material into chat, tickets, or logs.

## What the script calls

The script uses only V2 beta endpoints:

```text
GET  /health/live
GET  /v2/synthetic-boolean/per-miner/challenges?limit=2
GET  /v2/synthetic-boolean/per-miner/cnf?challenge_id=...&tier=...&seq=...
POST /v2/blobs/solutions
POST /v2/agents/submit-manifest
GET  /v2/agents/submit-manifest/receipts/{receipt_id}
GET  /v2/validator/weights/next
GET  /v2/audit/epochs/{epoch}
```

It does not call V1 submit or V1 validator weights.

## Manual manifest shape

If integrating your own miner, the signed manifest body is:

```json
{
  "schema": "cathedral.solution_manifest.v1",
  "card_id": "synthetic_boolean_v1",
  "challenge_id": "pm-t1-e...",
  "assignment_encoding": "bitset/v1",
  "solution_cid": "local://solution/<sha256>",
  "solution_sha256": "<sha256 of packed bitset>",
  "solution_bytes": 50
}
```

The miner signs Cathedral's canonical JSON manifest bytes with the hotkey and sends:

```text
X-Cathedral-Hotkey: <ss58 hotkey>
X-Cathedral-Signature: <base64 sr25519 signature>
X-Cathedral-Submitted-At: <UTC ISO timestamp>
```

For beta upload, `POST /v2/blobs/solutions` signs a separate `cathedral.blob_upload.v1` canonical payload and sends raw bytes with:

```text
Content-Type: application/octet-stream
X-Cathedral-Blob-Sha256: <sha256 of raw blob bytes>
```

## Solution encoding: `bitset/v1`

`bitset/v1` packs a complete SAT assignment into little-endian truth bits:

- variable `i` is stored at bit `i - 1`
- `1` means positive/true
- `0` means negative/false
- byte length must be `(num_vars + 7) // 8`

For the current beta shape of 400 variables, solution blobs are 50 bytes.

## Troubleshooting

- `missing dependency`: install with `python3 -m pip install requests bittensor-wallet python-sat`.
- DNS/connection failure: confirm `https://v2-beta.cathedral.computer/health/live` returns `200`.
- `invalid hotkey signature`: check clock skew, submitted timestamp, hotkey, and that the exact submitted body was signed.
- `solution_sha256_mismatch`: uploaded bytes do not match manifest `solution_sha256`.
- `bitset_size_mismatch`: packed bitset length does not match the CNF variable count.
- receipt stays `received`: verifier worker may be backlogged; retry polling or report the `receipt_id`.
- verified receipt but no shadow weight: wait a few seconds and re-fetch `/v2/validator/weights/next`.

## Operator pre-publish DNS note

Before announcing the beta broadly, operators should verify the clean domain is active:

```bash
curl -sS https://v2-beta.cathedral.computer/health/live
```
