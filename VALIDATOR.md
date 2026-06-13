<h2 align="center">Running a Cathedral v4 validator</h2>

<p align="center">
  The v4 validator is <b>one loop</b>: fetch one signed score per miner from the
  orchestrator, verify the signature, apply it. No local scoring, no rolling
  window, no row database. ~200 lines (<code>scaffold/validator_thin.py</code>),
  replacing the 4,600-line legacy validator.
</p>

## What it does

Every tick:

```
fetch  GET /v1/validator/weights/next        (one signed number per miner + burn)
verify Ed25519 signature against the pinned key_id + public key
check  network · netuid · not-expired · finite · non-negative · not a rollback
burn   apply the signed burn share to the burn uid (the rest splits across miners)
set    map hotkeys -> uids against your metagraph, set_weights
```

Scoring lives entirely on the orchestrator side and is composed into the single
number this validator applies. That means **recency, multi-challenge
composition, burn rate, and every future scoring change happen without a
validator release** — your job is to verify the orchestrator's signature and
relay its number to chain. The per-solve feed (`/v1/leaderboard/recent`) remains
public as an independently re-checkable audit trail; it is no longer the scoring
input.

## Why it replaces the legacy path

The legacy validator pulled every per-solve row, copied them into a local
database, and computed a 7-day rolling mean itself — so scoring logic was frozen
inside a binary every operator had to upgrade in lockstep, and an idle miner kept
earning for a week off its frozen tail. v4 deletes that machinery. The
orchestrator signs the final number; the validator trusts the signature, not its
own recomputation. Chain consensus is stake-weighted, so weights converge as
validators relay the same signed vector.

## Trust model — what the signature buys you

| Guarantee | Mechanism |
|---|---|
| **You apply only what the pinned key signed** | Ed25519 over canonical JSON; `key_id` is pinned, so a key rotation you didn't opt into is rejected |
| **No stale or replayed vector** | `expires_at` enforced; `policy_version` is a monotonic fence — an older version than your last-accepted is refused (fail-closed: a corrupt fence file aborts the tick rather than resetting) |
| **No silent burn bypass** | The burn share is inside the signed payload — the orchestrator cannot route weight without it being signed |
| **Right subnet** | `network` + `netuid` must match your chain config |

## Install

Requires Python 3.11 and a registered SN39 validator wallet.

```bash
git clone -b v4 https://github.com/cathedralai/cathedral.git cathedral-v4
cd cathedral-v4
python -m venv .venv && . .venv/bin/activate
pip install -r deploy/requirements.txt   # fastapi/cryptography/bittensor-wallet + bittensor
```

## Pin the key

The vector is signed with Cathedral's published Ed25519 key — the same key that
signs the eval feed, available at `https://api.cathedral.computer/.well-known/cathedral-jwks.json`.
Pin it:

```bash
export CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY=10890a66aa752479cb3b634f366d7bd27c374324d83f88d2d6b69ab066f25e26
```

Pinning is the whole point: the validator refuses any vector not signed by
exactly this key under `key_id = cathedral-weight-policy`. Verify the value
against the live JWKS before trusting it.

## Run

**Dry-run first (default — computes and prints the uid vector, sets nothing):**

```bash
python -m scaffold.validator_thin \
  --publisher-url https://api.cathedral.computer \
  --network finney --netuid 39 \
  --wallet-name <your-coldkey> --wallet-hotkey <your-validator-hotkey> \
  --once
```

You'll see the accepted vector, the burn share, and the normalized per-uid
weights it *would* set. Confirm that looks right.

**Then broadcast (actually sets weights):**

```bash
python -m scaffold.validator_thin \
  --publisher-url https://api.cathedral.computer \
  --network finney --netuid 39 \
  --wallet-name <your-coldkey> --wallet-hotkey <your-validator-hotkey> \
  --broadcast
```

Without `--once` it loops on `--interval-secs` (default 1500s). The rollback
fence persists in `--state-file` (default `~/.cathedral/thin_validator.json`), so
a restart cannot apply an older vector than the last one you accepted.

| Flag | Default | Purpose |
|---|---|---|
| `--publisher-url` | `https://api.cathedral.computer` | where to fetch the signed vector |
| `--public-key-hex` | `$CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY` | the pinned signing key (required) |
| `--network` / `--netuid` | `finney` / `39` | your chain target |
| `--wallet-name` / `--wallet-hotkey` | — | your validator wallet |
| `--broadcast` | off (dry-run) | actually submit `set_weights` |
| `--once` | off | single tick then exit |
| `--offline` | off | verify + print only, no chain access (CI / smoke) |
| `--state-file` | `~/.cathedral/thin_validator.json` | rollback-fence persistence |

## Maturity

The orchestrator side of v4 is live in production on `api.cathedral.computer`.
This validator binary is **new**: its verify / burn / fence logic is covered by
the release gates (`publisher_verify.py`), and it has been exercised end-to-end
against the live mainnet vector. Run it in dry-run (or alongside your existing
validator) until you've confirmed the uid vector it produces matches your
expectation, then switch to `--broadcast`. Adoption is per-operator and
incremental — the network already converges as long as stake-weighted-majority
validators relay the same signed vector.
