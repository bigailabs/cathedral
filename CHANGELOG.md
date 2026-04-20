# Changelog

## 2026-04-20

### Acknowledgement

- **New [CREDITS.md](CREDITS.md)** names the Basilica engineering team — Evan Pappas (epappas), itzlambda, open-junius (Opentensor Foundation), Covenant AI — and includes a "What Cathedral is" section making explicit what's distinct to this project: the thesis, brand, frame, audience, editorial surface, operating decisions.
- `README.md` fork references now point to CREDITS.md; Origins section names the three engineers directly.
- `LICENSE` preserves upstream `© 2025 tplr.ai` (required by MIT) and adds `Portions © 2026 bigailabs (cathedral fork modifications)` — the standard MIT fork form.
- Marketing-site mirror shipped in parallel as [cathedral.computer/origins](https://cathedral.computer/origins), linked from every page footer.

### Site

- **New `/masons` public register** and `/mason?h=<hotkey>` per-mason page. Each verified mason gets a permanent, shareable URL with an embed snippet for bio use. Founding-mason designation for the first 10 verified (ordering by uid-asc as a proxy until the backend exposes verification timestamps). Not linked from header nav — intentionally a slow surface.
- **`/ledger` redesigned.** A subnet-wide pipeline band sits above the existing dashboard: apprentice signups → verified masons → with income → ambassador (stub) → mentor (stub) → patron (stub) → clerk (stub). Live rows read from `/api/v1/cathedral/signups/stats`, `/api/substrate/validator/registry`, `/api/v1/cathedral/rentals`.
- **`/ledger` correctness fix.** Registered-masons table was joining to machines by array index, which showed wrong hardware for the wrong mason whenever either list reordered. Hardware column removed from the per-row table (`/machines` and registry share no safe join key); now rendered as an aggregate summary beneath the table instead.
- **`/ledger` XSS hardening.** All `innerHTML` interpolation of backend fields replaced with `createElement + textContent`. Same safe pattern the home-page MinersRegister uses.
- **`/afrotensor` refactored to step-through.** One stage visible at a time with `?s=N` URL param, horizontal `01 … 08` pip indicator, prev / mark-complete-next buttons. Existing progress state (`afrotensor_progress_v1` localStorage + `POST /api/v1/cathedral/progress`) untouched. Previous version was one long scroll.
- **Home hero reshape** (multi-PR). Data-first left column — `01 Live state` meta leads, thesis quote follows with breathing room. Log pane message formatter strips seven layers of substrate error-wrapping into one clean line (e.g. `weight (1010) · no-permit · attempt N · netuid=39`); raw message preserved on hover. Decoder dedupe prevents the same "means / doing" block repeating per retry cycle. "Posture tags" (expected · no action / informational / needs attention) added to annotation titles.
- **Resilient fetches.** 20s `AbortController` timeouts on home-page substrate fetches; visible stale-state messaging replaces perpetual "loading…" when the upstream is down. Mirrors the backend resilience work on polariscomputer (`asyncio.Lock` + `asyncio.wait_for` on `/state` and `/validator/registry`).
- **Copy pass — roles.** Final label set is **apprentice, mason, clerk, patron, master** — five labels derived from evidence, not assigned. Dropped "freemason" (Masonic-Order baggage) and "carpenter" (invented slot-filler with no concrete definition). Mason-who-ships-code stays just a mason; contributions show up on the ledger as work, no new label needed.
- **Copy pass — "miners" → "masons"** in user-facing text. Thesis lede restructured: "A cathedral is a commons." stands alone, body stanza follows with breathing room.
- **Dark mode default.** First-visit users always get dark; OS `prefers-color-scheme` no longer consulted. Saved toggle still wins on return.
- **Hero layout hygiene.** Breakpoint to stacked moved from 1100px to 900px so side-by-side holds through normal laptop widths. `min-width: 0` on grid children, mobile nav wraps cleanly.

## 2026-04-19

### First verified miners, home-ownable pivot landed

- **UID 115** (our miner, RTX 5090 on a Lium container) and **UID 155** (McDee, RTX 3090 + 2× RTX 3060 at home) both reached `status=verified` — the first real end-to-end verifications on the subnet.
- The pivot promised in issue #24 is now operational: consumer GPUs, workstations, Apple Silicon, DGX Spark, and CPUs all admit. Data-center SKUs (A100/H100/MI300/etc.) remain rejected.
- 23 GPU tiers + 3 CPU tiers live in the incentive API (`/v1/incentive/config`).

### CPU mining verified end-to-end on testnet

- **Cathedral testnet SN292** now running alongside mainnet. Our validator at UID 32, test CPU miner at UID 33 (AMD EPYC 7B12, 4 vCPU, 16 GB) reached `status=verified` with score 1.00.
- New validation path: when a miner declares a `CPU_*` category, the validator probes `lscpu` + `free -m` + `nproc` over SSH instead of `nvidia-smi`. Synthetic GPU UUID keeps downstream persistence unchanged.
- Pricing tiers: `CPU_BASIC` ($0.02 / vCPU-hr), `CPU_STANDARD` ($0.04), `CPU_PERFORMANCE` ($0.08). Opt-in; remove from the pricing table to disable.

### Validator hardened for home miners

- Dropped the NAT inbound-port requirement when binary attestation is disabled. Home routers don't forward random ports; requiring it was a blanket disqualifier.
- Dropped the Docker requirement on the same condition. Many home boxes don't run Docker.
- Dropped strict GPU-UUID matching on Lightweight re-checks; the upstream normalizer drifted between the Full-scan stored form and the Lightweight rescrape, flagging good nodes as mismatched.
- SSH scheduler deadlock hardened with a `tokio::time::timeout` and a 10-minute workflow-wide cap (cathedral#29 — precedent in 4 upstream Basilica fixes).
- Lightweight success on a non-rented node now graduates status to `verified` (previously stuck at `online`).
- `RegisterBid` and rental APIs no longer reject `GpuCategory::Other(_)` — consumer/Apple/DGX categories all pass.

### Site

- Three-tier funnel on the home page: **declared on SN39** (cheap on-chain signal), **registered with us** (called RegisterBid), **verified** (SSH-confirmed). Was one misleading count before.
- Miners Register pins verified + registered rows at the top; declared-only chain rows are now hidden from the list (header counts still show the full funnel).
- `/ledger` replaced the stale "Our miner orphaned" block with a live **Registered miners** table off `/api/substrate/validator/registry`.
- New sage `--ok` palette token. "verified" pills render in sage, "registered" in accent blue.

### Infrastructure

- Single VPS (`135.181.8.214`) now runs both validators: `cathedral-validator.service` (SN39 finney) and `cathedral-validator-testnet.service` (SN292 test). Distinct configs, data dirs, ports.
- `cathedral-miner-testnet` process running on the VPS too as the CPU test miner. SSH target is a dedicated `cathedral-miner` user on the same box.
- Daily launchd job `com.cathedral.basilica-watch` tracks upstream `one-covenant/basilica` commits/PRs into `research/basilica-history/DELTA.md`.
- 15-min launchd job `com.cathedral.miner-watch` snapshots validator.db state transitions to `docs/overnight-miner-watch.log`.

### CLI scope corrected

- `cathedral-cli` is miner and validator operator tooling only — `status`, `miners`, `miner <uid>`, `validators`. Cathedral is the subnet; user-facing rent/run/deploy surfaces belong under `polaris-cli` on Polaris.

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
