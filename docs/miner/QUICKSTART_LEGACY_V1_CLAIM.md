# Miner Quickstart (legacy `/v1/claim` path)

> **DEPRECATED 2026-05-24.** This entire flow (the `/v1/claim` CLI and the `cathedral-runtime` probe container) is legacy. New miners should follow [QUICKSTART.md](QUICKSTART.md) which uses direct SSH+Hermes. The current SAT lane scores `synthetic_boolean_v1` rows and does not require the cathedral-runtime image at all. This document is retained only for operators still on the old claim flow and will be removed in a future cleanup.

You operate a legacy Polaris evidence worker that maintains one regulatory job. Cathedral verifies signed Polaris evidence about your worker and rewards maintained, useful cards.

## Prerequisites

- Bittensor coldkey + hotkey (registered on SN39 mainnet or SN292 testnet)
- A Polaris account with a deployed agent producing a card
- Your Polaris agent ID (`agt_01H...`)
- The validator URL and bearer token from the operator running the validator

## Install

```bash
git clone https://github.com/cathedralai/cathedral
cd cathedral
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configure

Copy `config/miner.toml` and fill in:

- `miner_hotkey` - your hotkey ss58
- `owner_wallet` - your coldkey ss58 (used by Cathedral to filter self-loop usage)
- `validator_url` - the validator endpoint (e.g. `https://validator.cathedral.computer`)
- `validator_bearer_env` - env var holding your bearer token

Set the bearer:

```bash
export CATHEDRAL_VALIDATOR_BEARER=...
```

## Submit a claim

```bash
cathedral-miner submit \
  --work-unit "card:eu-ai-act" \
  --polaris-agent-id agt_01H1234567890ABCDEF \
  --polaris-run-ids run_01H...,run_01H... \
  --polaris-artifact-ids art_01H...,art_01H...
```

The validator returns `202 Accepted` if the claim shape is valid. Verification is async - check your card on cathedral.computer or query the validator's `/health` to see queue depth.

## What gets rewarded

The validator scores your card on six dimensions:

1. **Source quality** - official sources count more than secondary analysis
2. **Freshness** - refresh on the schedule from the card registry
3. **Specificity** - concrete `what_changed` and `why_it_matters`
4. **Usefulness** - action notes and risks
5. **Clarity** - readable summary
6. **Maintenance** - kept current over time

Broken sources, uncited claims, and legal-advice framing fail preflight before scoring.

Detailed rules: [../protocol/SCORING.md](../protocol/SCORING.md)
