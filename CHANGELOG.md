# Changelog

## 2026-04-17

### Site

- **cathedral.computer is live.** Two-pane dashboard streaming validator and miner logs side by side, with cross-stream event pairing (registration, ssh-key deploy, verification) highlighted together.
- Added `/mine` quickstart — prerequisites, validator endpoint, `miner.toml`, build steps, expected logs, known limitations, plain disclaimer.
- Added `/roadmap` — three acts (`baseline` / `loops` / `ecosystem`) pulled live from GitHub issues. Acts are the arc, issues under them are subject to change.
- Added `/changelog` — this file, rendered on-site.
- Light mode + readability pass — warm paper theme, bigger log font, quieter chrome, hairline separators. Theme preference persists and respects `prefers-color-scheme`.
- Home page stanza framing the project as a commons.

### Project organization

- Created three GitHub milestones: `act 1 · baseline`, `act 2 · loops`, `act 3 · ecosystem`. Existing act-labelled issues moved into the matching milestone so the grouping is native to GitHub (#11 #12 #13 #14 #15 #16 #17 #18 #19).
- Site deploys are reproducible from the repo now — `wrangler.jsonc` checked in (previously ad-hoc).

### Backend (polariscomputer)

- New `/api/substrate/miner/logs` endpoint mirroring the validator log endpoint; powers the miner pane on the dashboard.
- Log-tail security hardening — explicit allowlist on log paths, shell-quoted path argument. Guards against operator misconfig leaking wallet or ssh key files via the log endpoint.
- CORS origins extended to cathedral.computer.

### On-chain (SN39)

- Validator (UID 123) and miner (UID 115) running end-to-end on a single Hetzner box with one A100 GPU node attached.
- First miner scored 1.0 by the validator.
- Weight-setting still parked — we're below the ~14k α permit threshold on SN39. CUs accrue; emissions flow once weights unlock.

### Known rough edges, shipped honestly

- Binary GPU attestation is off; verification runs over SSH via `nvidia-smi`. Spoofable, honor-system for now — will swap in the prover binary when ready.
- Only A100 and H100 priced today.
- No rental marketplace yet — that's act 2.

## 2026-04-16

- Project renamed from Substrate to Cathedral. Repo moved to github.com/bigailabs/cathedral (#8)
- Binaries renamed to cathedral-validator and cathedral-miner (#7)
- Fixed validator bug where verified nodes were marked offline when binary validation is disabled. SSH-based GPU discovery populates gpu_uuid_assignments so nodes stay online without the prover binary (#7)
- Rewrote docs/miner.md for Cathedral with concise quickstart and current validator endpoint (#9)
- First miner fully registered, verified, and scoring 1.0 on the validator (2x A100-SXM4-80GB)

## 2026-04-14

- Published v0 policy doc (docs/policy.md)
- Added status block to README

## 2026-04-13

- Forked from upstream Basilica
- Modified validator to run against self-hosted incentive API
- Rebuilt API endpoints so miners and validators can discover each other on this fork
- Opened PR #1: grpc_endpoint_override config, allow_offline mode on FixedAssignment, non-fatal serve_axon on local nets
- Published live dashboard at polaris.computer/substrate
