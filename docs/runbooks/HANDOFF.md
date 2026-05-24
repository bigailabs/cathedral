# Validator Handoff

A clean handoff is a property of the system, not a one-time event. If the validator cannot be handed to another operator with this document plus credentials, the handoff itself is broken - file an issue.

## What the incoming operator gets

1. This document
2. `docs/validator/RUNBOOK.md`
3. `config/<network>.toml` (with `network.validator_hotkey` set to the local wallet hotkey NAME, same value passed to `btcli --wallet.hotkey`, NOT the ss58; and `polaris.public_key_hex` pinned from JWKS)
4. Hotkey + coldkey files (encrypted at rest)
5. A new local validator bearer token for the `/v1/claim` endpoint (rotate on every handoff). This is the value of `CATHEDRAL_BEARER`; it is local auth, not publisher-read auth.
6. The Cathedral eval-signing pubkey hex, for `CATHEDRAL_PUBLIC_KEY_HEX` env (same across operators; pin from `kid: cathedral-eval-signing` in `https://api.cathedral.computer/.well-known/cathedral-jwks.json`)
7. The Polaris runtime-attestation pubkey hex, for `polaris.public_key_hex` in TOML (same across operators; pin from `kid: polaris-runtime-attestation` in the same JWKS document)
8. Read access to the chain, Polaris API, and any monitoring dashboards

## What does not get handed over

- Tribal knowledge. If something matters, it goes in the runbook.
- The previous operator's bearer token (rotated).
- Personal credentials of the previous operator.

## Cutover steps

1. Incoming operator stands up the validator on their host:
   ```bash
   git clone https://github.com/cathedralai/cathedral
   cd cathedral && python3.11 -m venv .venv && source .venv/bin/activate
   pip install -e .
   curl -s https://api.cathedral.computer/.well-known/cathedral-jwks.json | jq
   # pin CATHEDRAL_PUBLIC_KEY_HEX (env)         from kid=cathedral-eval-signing
   # pin polaris.public_key_hex (TOML)          from kid=polaris-runtime-attestation
   # CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX defaults in the provisioner:
   # 8d74453ac008cc7be3f0609b43d31aa4096ab4a6ded32b9e754a5c48360938fd
   export CATHEDRAL_BEARER=$(openssl rand -hex 32)
   cathedral-validator migrate --config config/<network>.toml
   ```
2. Validator runs in dry mode for at least one weight-set interval (`weights.disabled = true`).
3. `cathedral health` reports `registered: true`, `weight_status: disabled`, `stalled: false`. Logs include `pull_loop_tick fetched=... drained=true` once the initial backfill walk completes.
4. Outgoing operator stops their validator.
5. Incoming operator flips `weights.disabled = false`, restarts.
6. Watch one full weight-set cycle; confirm `weight_status: healthy`.

## Rollback

- Outgoing operator restarts their validator with the old bearer.
- File an issue with the runbook gap.
- Update `RUNBOOK.md` before retrying.
