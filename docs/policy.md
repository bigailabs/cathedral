# How Cathedral works (draft)

Status: v0, will evolve. Published 2026-04-14.

## What this is

Cathedral (SN39) is an open compute network on Bittensor. Miners contribute GPU capacity, validators verify it, the chain distributes emissions based on validator weights.

We (Polaris) run a validator on SN39 and publish the incentive configuration it reads. The configuration lives at:

    GET https://api.polaris.computer/v1/incentive/config

Other validators running our code read from the same endpoint.

## What we commit to

While we're operating this config:

- **No forced burn.** The only burn is the chain-level residual when miner payouts don't fill the emission capacity. No operator-set tax on top.
- **Category independence.** Earnings in one GPU category don't affect another.
- **No retroactive changes.** Config updates affect earnings from the update forward. Already-vested earnings stay vested.
- **Slashing is narrow.** Only triggers on permanent node loss during an active rental. Transient failures don't slash.
- **Changes are announced.** Material config changes get posted publicly before they take effect.
- **Changelog is public.** Every change gets recorded below.

## What we don't claim

- We don't own the subnet. We run a validator and publish a config.
- We're not setting weights yet (low stake). Solving that soon.
- These commitments apply while we're operating the config. Governance of the subnet long-term is a community question.
- **We do not recommend staking to subnet 39 at this time.** There is uncertainty about the keys from the prior operators. Wait for clarity before committing stake.

## Current status

- Validator running: yes
- Setting weights: no (stake threshold)
- Miners registered: small number, growing
- Active rentals: none yet

See live dashboard: https://polaris.computer/cathedral

## How to propose changes

- Open an issue in this repo
- Post in the Cathedral Discord
- DM a maintainer

## Changelog

- 2026-04-14: v0 published.
