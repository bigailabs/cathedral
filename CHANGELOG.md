# Changelog

## 2026-04-18

### Validator migrated off rented infra

- **The rented validator host (Verda VM) was terminated by the provider.** The wallet was recovered from a local backup, and the validator was re-hosted on a self-owned home box running WSL Ubuntu 22.04.
- **New public endpoint:** `tcp://polarisserver.tail02a2a.ts.net:443` (Tailscale Funnel TCP forwarder → validator gRPC on `127.0.0.1:50052`).
- **Same validator identity:** hotkey `5DnvAg…ThWPb`, SN39 UID 123, 131.82α stake — nothing changed on-chain; the key moved, the IP moved, the box is different.
- **UID 115 (cathedral-miner) orphaned.** The miner was previously registered against a GPU node on the terminated Verda VM. The on-chain registration is stale but preserved; we'll re-register once we have a new GPU node to point it at.

### Operational lessons baked in

- Validator and miner processes now launch with `stdbuf -oL` on both sides of the `tee` pipe — fixes the silent-log buffering bug we hit previously where a quiet miner looked "dead" because glibc block-buffered the pipe.
- Deployment is reproducible from the repo: `~/cathedral-bin/start-all.sh` spins both processes inside named tmux sessions. systemd units are staged in `~/cathedral-config/systemd/` for when we want reboot-safety.

### Site

- Dashboard shows a migration banner explaining the host move.
- `/mine` quickstart updated to point at the new gRPC endpoint (`polarisserver.tail02a2a.ts.net:443`, TLS over TCP via Funnel).
- `/ledger` now reflects that UID 115 is orphaned pending re-registration.

### Known rough edges

- Log streaming from the new validator to `cathedral.computer` is not yet reconnected. The Railway backend talks SSH to the old IP; we need a Tailscale sidecar on Railway, or a reverse HTTP tailer on the home box, to restore live logs on the site. Validator is running and logging locally — the site just can't see it yet.
- Funnel wraps outbound traffic in TLS. Miners connecting in must speak TLS on port 443. Bittensor gRPC typically does; this is being tested with external miners.
- Home-box GPU is consumer-grade (RTX 5060); our validator only prices A100/H100, so we do not yet have a billable local GPU node.

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
- Act 2 is about making mining economically coherent; we're still working out the model in public.

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
