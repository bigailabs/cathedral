# Cathedral v4.0.0-rc.5-v2-beta

This is a **V2 manifest beta prerelease** for miner E2E testing.

V2 is isolated and shadow-only: it does **not** affect current V1 production validator weights, rewards, emissions, or payout state.

## What changed

- Added isolated V2 blob-backed solution manifest lane.
- Added V2 per-miner challenge/CNF endpoints.
- Added optional beta blob upload endpoint so miners do not need Hippius accounts for testing.
- Added async verifier path for signed manifests.
- Added V2 verified receipts, shadow weights, and epoch audit bundles.
- Added V2-prefixed environment configuration to avoid mixing with current production lane.
- Added miner E2E helper script and public guide.

## Miner beta endpoint

Preferred clean beta URL:

```text
https://v2-beta.cathedral.computer
```

Operators should verify DNS is active before announcing the beta URL broadly.

## Miner E2E quick start

```bash
python3 -m pip install requests bittensor-wallet python-sat
python3 scripts/v2_miner_e2e.py --base https://v2-beta.cathedral.computer
```

Expected final line:

```text
E2E_OK ... "status":"verified", "weight":1.0
```

Guide:

```text
docs/V2_MINER_E2E_GUIDE.md
```

## Do miners need Hippius accounts?

No. For this beta, miners can upload solution bytes to:

```text
POST /v2/blobs/solutions
```

Cathedral returns a `local://solution/...` CID that can be used in the signed manifest.

Future modes can support miner-hosted Hippius/IPFS/R2/HTTPS blobs, but the protocol only requires a fetchable `solution_cid` plus matching `solution_sha256`.

## V2 endpoints

```text
GET  /v2/synthetic-boolean/per-miner/challenges
GET  /v2/synthetic-boolean/per-miner/cnf
POST /v2/blobs/solutions
POST /v2/agents/submit-manifest
GET  /v2/agents/submit-manifest/receipts/{receipt_id}
GET  /v2/validator/weights/next
GET  /v2/audit/epochs/{epoch}
```

## Verified beta smoke

A live V2 E2E smoke succeeded against the beta Railway service:

- fetched V2 PM challenge
- fetched V2 CNF
- solved locally
- uploaded `bitset/v1` solution blob
- submitted signed manifest
- receipt became `verified`
- V2 shadow weights included test hotkey with `weight=1.0`
- V2 audit bundle included the receipt

Example receipt from smoke:

```text
0fdf891c-696e-4fb8-ad63-acf1c0b3f64d
```

## Safety notes

- This is a prerelease beta.
- V2 weights are shadow-only under `/v2/validator/weights/next`.
- V1 `/v1/validator/weights/next` is unchanged.
- Current production submit path is unchanged.
- Current rewards/payout DB mutation is not part of this release.
- Rotate any development tokens used during beta setup before production use.
