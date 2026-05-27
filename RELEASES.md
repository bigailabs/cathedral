# Releases

Public release notes for Cathedral operators.

This file states what changed, what operators need to do, and what remains off. It is not a full engineering changelog. Use GitHub compare views for exact code diffs.

Tags are SSH-signed. Verify with:

```bash
git -c gpg.ssh.allowedSignersFile=etc/cathedral/allowed_signers tag -v <tag>
```

## Current Release

### v2.0.0 - SAT mining cutover

Date: 2026-05-27

This is the major SAT cutover release for Cathedral validators,
publishers, and miner-facing APIs. It moves the live mining path away
from card-era work and onto `synthetic_boolean_v1` SAT challenges,
including concurrent active challenges by tier.

What changed:

- Cathedral validators stamp Bittensor `set_weights` calls with
  `version_key=2000000`.
- The SAT lane is the primary miner path.
- Public challenge discovery is available at `GET /v1/synthetic-boolean/current-challenge` without exposing CNF bodies, fetch tokens, internal paths, or signed challenge material.
- Tier-specific challenge discovery is available at `GET /v1/synthetic-boolean/current-challenge?tier=N`.
- Miners can discover every currently active SAT tier through `GET /v1/synthetic-boolean/active-challenges`; the response uses the same public-only projection as `current-challenge`.
- Miners submit SAT solutions directly through `POST /v1/agents/submit` with `challenge_id` and `dimacs_solution`.
- Valid solutions are checked synchronously, signed as schema-5 eval rows, and can rank immediately.
- Registrations without a submitted solution stay `pending_solution`; invalid or late solutions get signed zero-score attempts and are not SSH-probed.
- `GET /v1/synthetic-boolean/active-cnf` remains the private challenge-material route and requires a real hotkey signature plus `X-Cathedral-Submitted-At`.
- SSH/Hermes attestation is audit-only after a valid solve; it is not the payment gate for SAT.
- The challenge source can safely prepare one active SAT challenge per
  `(family_id, tier)` while existing callers keep one-active-per-family
  behavior by default.
- Tier-scoped lock-and-promote keeps a won tier moving by promoting the
  next pending challenge in that same tier.
- The private generator-to-publisher lease contract is documented for
  pre-generated CNF pools, lease TTLs, health/depth reporting,
  idempotent leases, and hash-confirmed imports.

What did not change:

- Public HTTP paths remain `/v1/...`; this is a release/protocol major,
  not an HTTP path rename.
- Public endpoints do not expose tokenized CNF URLs, raw CNF, or local challenge paths.
- The generator API is private Cathedral infrastructure. Miners and
  validators do not call it.
- Mainnet forced burn remains at the bootstrap value in this tag. Burn
  reduction is a separate operator-policy release once SAT receipt volume
  is stable.

Operator action:

1. Pull and verify the signed `v2.0.0` tag.
2. Restart validators and confirm the emitted chain `version_key` is
   `2000000`.
3. Managed PM2 operators should keep `cathedral-updater` running. It
   polls signed tags, verifies them with `/opt/cathedral/allowed_signers`,
   reinstalls Cathedral, migrates the validator DB, and reloads
   `cathedral-validator`.
4. Manual operators should fetch tags, check out `v2.0.0`, reinstall,
   run `cathedral-validator migrate --config <mainnet config>`, and
   restart the validator process.
5. Ensure remote signed-weight verification is configured with the
   Cathedral weight-policy public key before enabling remote mode.
6. Validators that cannot verify the signed Cathedral policy should fail
   closed rather than emit stale local/card-era weights.
7. Keep the operator file-backed SAT challenge path available as the
   break-glass fallback while the generator pool is introduced.

## Previous Releases

### v1.1.29 - Direct SAT solve submissions and public challenge discovery

Date: 2026-05-26

This was a publisher and site release. It did not require a validator
update.

What changed:

- Public challenge discovery is available at `GET /v1/synthetic-boolean/current-challenge` without exposing CNF bodies, fetch tokens, internal paths, or signed challenge material.
- Miners submit SAT solutions directly through `POST /v1/agents/submit` with `challenge_id` and `dimacs_solution`.
- Valid solutions are checked synchronously, signed as schema-5 eval rows, and can rank immediately.
- Registrations without a submitted solution stay `pending_solution`; invalid or late solutions get signed zero-score attempts and are not SSH-probed.
- `GET /v1/synthetic-boolean/active-cnf` remains the private challenge-material route and requires a real hotkey signature plus `X-Cathedral-Submitted-At`.
- SSH/Hermes attestation is audit-only after a valid solve; it is not the payment gate for SAT.
- The website now surfaces the current SAT challenge clearly, avoids fake fallback attempts, and sends miners to the direct challenge flow.

What did not change:

- Validator burn/config rollout is not part of this release.
- Remote weight source behavior is unchanged.
- Public endpoints do not expose tokenized CNF URLs, raw CNF, or local challenge paths.

Operator action:

1. Deploy the publisher/API update.
2. Deploy the website update.
3. Keep validator rollout separate until the challenge minting engine and receipt volume are ready.

### v1.1.28 - Cathedral V4: Agentic SAT goes live

Date: 2026-05-24

This release turned the SAT lane on as a live competitive market.

What changed:

- Real SAT rounds use a days-scale time limit instead of the toy readiness-probe timeout.
- Publisher operators can disable legacy base-score inputs with `CATHEDRAL_WEIGHT_POLICY_DISABLE_LEGACY_BASE_SCORES=true`.
- Signed weight-policy metadata reports the SAT-only score source when the legacy base scores are disabled.

Operator action:

1. Pull the signed tag.
2. Verify the tag.
3. Restart the publisher/validator processes participating in the SAT cutover.
4. Confirm signed policy metadata before moving meaningful emissions.

### v1.1.27 - Remote weight validator opt-in config

Date: 2026-05-22

This was the validator-facing remote-weight opt-in release.

What changed:

- Validator config templates include `[remote_weight_source]`, disabled by default.
- The provisioner writes the current Cathedral weight-policy public key pin.
- Validators can opt in to remote signed weights by setting `enabled = true`.

What did not change:

- Remote signed weights are not enabled automatically.
- SAT mainnet weight remains `0.0`.
- Validators still verify signed rows with pinned public keys.

Operator action:

1. Pull the signed tag.
2. Verify the tag.
3. Restart the validator.
4. Enable remote signed weights only after release notice.

## SAT Release Story

### v1.1.26 - Remote-weight cleanup window

Date: 2026-05-21

This was a cleanup checkpoint while the validator control surface was being settled.

It is superseded by `v1.1.27`.

### v1.1.25 - Managed validator config and SAT readiness probe

Date: 2026-05-20

This release aligned managed validator config and added the public SAT readiness probe.

The readiness probe exercises the SAT answer shape without emissions. It is not a scored SAT launch.

### v1.1.24 - Public SAT readiness surfaces

Date: 2026-05-20

This release exposed public miner onboarding surfaces for SAT:

- API root links
- `skill.md` miner contract
- CNF URL and SHA-256 answer protocol
- first-submitted-valid winner rule

SAT weight stayed `0.0`.

### v1.1.23 - SAT shadow hardening

Date: 2026-05-20

This release hardened the public SAT shadow path:

- authorized CNF URL transport
- receipt-time winner ordering
- hash-only public feed rows
- zero-score kill switch

It proved the shape without moving SAT emissions.

### v1.1.22 - SAT operations guardrails and updater hardening

Date: 2026-05-19

This release added the `synthetic_boolean_v1` lane boundary and hardened validator updates.

It established the split between public problem metadata and publisher-private challenge material.

SAT stayed disabled by default.

## Earlier Foundation

### v1.1.21 - Private challenge runtime foundation

Date: 2026-05-18

Publisher-side challenge runtime code landed behind gates.

No mainnet weight changed.

### v1.1.20 - Hermes package capture foundation

Date: 2026-05-17

Hermes execution capture foundations landed behind operator controls.

No mainnet weight changed.

### v1.1.19 - Protective mainnet burn

Date: 2026-05-17

Mainnet validator policy restored protective burn while the next scoring signal matured.

Operator note:

- Do not move meaningful emissions without a clean testnet E2E.
- Do not publish private challenge material in release notes, docs, or public API output.
