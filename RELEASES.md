# Releases

Public release notes for Cathedral operators.

This file states what changed, what operators need to do, and what remains off. It is not a full engineering changelog. Use GitHub compare views for exact code diffs.

Tags are SSH-signed. Verify with:

```bash
git -c gpg.ssh.allowedSignersFile=etc/cathedral/allowed_signers tag -v <tag>
```

## Current Release

### v1.1.27 - Remote weight validator opt-in config

Date: 2026-05-22

This is the current validator-facing release.

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

### v1.1.22 - SAT launch rails and updater hardening

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
