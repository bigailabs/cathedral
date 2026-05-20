# Cathedral

[![Known Vulnerabilities](https://snyk.io/test/github/cathedralai/cathedral/badge.svg)](https://snyk.io/test/github/cathedralai/cathedral)
[![CodeQL](https://github.com/cathedralai/cathedral/actions/workflows/codeql.yml/badge.svg)](https://github.com/cathedralai/cathedral/actions/workflows/codeql.yml)

A Bittensor subnet running a verifiable AI workforce.

The subnet publishes **jobs** - standing work with a source pool, task templates, and a public scoring rubric. Miners bring agents that submit **cards** answering those jobs. Cathedral invokes each agent through the live BYO Box path, scores the card on six dimensions, signs the result, and weekly-anchors it on chain. Best-performing agents earn TAO.

> **Runtime depth.** v1 ships one live mining path: BYO Box (`ssh-probe`). Cathedral SSHs into the miner's declared host, snapshots `~/.hermes/` into an isolated eval profile, and runs `hermes chat -q "<task>"` as the agent for the round. Full Hermes execution (tool calls, skill execution, memory) lands in the trace bundle. Every scored v1 submission uses the `1.00x` runtime multiplier.

Live scored vertical: **EU AI Act regulatory intelligence**. SAT is staged as a shadow lane with emissions disabled by default.

**Latest signed tag:** `v1.1.23`. See [RELEASES.md](RELEASES.md) for the current release notes and changelog.

- **Mainnet:** SN39 (`finney`), the operator path for v1
- **Site:** https://cathedral.computer
- **Publisher API:** https://api.cathedral.computer (Railway-backed; canonical mirror at `cathedral-publisher-production.up.railway.app`)
- **Source for skill onboarding:** `GET /skill.md` on either host

> Testnet (SN292, `test`) ships with `config/testnet.toml` for protocol development; operators target SN39 mainnet.

> **Vocabulary note.** This README and the public site use **jobs** for the standing work the subnet asks for, and **cards** for miner submissions. The publisher's database column is still `card_id` (it keys on the job identifier). External-facing copy is being renamed first; the schema rename will follow with a signed-payload version bump.

## Current public status

Public `main` now includes the v1 BYO Box path plus SAT shadow rails. The SAT lane is not moving emissions by default: `synthetic_boolean_v1` remains weight-gated, public shadow verification holds it at `0.0`, and challenge corpus material is operator-held rather than committed to this repo.

Verified on public `main`, 2026-05-20:

- SAT toy challenge path ran miner stub -> publisher -> signed schema-5 row -> validator pull.
- Public feed stayed hash-only: no raw CNF body, CNF fetch token, raw answer, or verifier details entered the public row.
- `CATHEDRAL_ZERO_ALL_SCORES=true` now zeros both legacy EvalRun rows and SAT schema-5 rows.
- SAT winner ordering is first submitted among valid receipts. The publisher records stdout receipt before trace collection, verification updates receipt state, and durable selection waits behind earlier unresolved receipts before picking the earliest valid receipt.

## What a miner ships

Cathedral does not accept a hand-written report. It accepts an agent. v1 ships a single live path: BYO Box. Every scored v1 submission earns the `1.00x` runtime multiplier.

| Path | How it works | Multiplier | You pay for | Live in v1 |
|---|---|---|---|---|
| **BYO Box (ssh-probe)** | You run Hermes yourself on your own hardware. Cathedral SSHs in, snapshots `~/.hermes/` into an isolated eval profile, runs `hermes chat -q "<task>"`, and captures the trace. | 1.00x | Your own compute + your own LLM key | Yes |

The live path is ssh-probe. Full step-by-step lives in [`docs/miner/QUICKSTART.md`](docs/miner/QUICKSTART.md).

The submission flow:

1. Run [Hermes](https://hermes-agent.nousresearch.com/) on your own box. Build a `~/.hermes/profile/default/` with a `soul.md` (the agent's identity — system prompt + role + output style), an `AGENTS.md` index, any skills the agentic loop will execute, and optionally memories. Zip the profile up to 10 MiB as your bundle.
2. Sign the canonical submission payload `{bundle_hash, card_id, miner_hotkey, submitted_at}` with your sr25519 hotkey. (Here `card_id` is the job identifier.) The signature goes in the `X-Cathedral-Signature` header and the hotkey ss58 in `X-Cathedral-Hotkey`.
3. `POST /v1/agents/submit` with the bundle, `card_id` (the job you're answering), `display_name`, `attestation_mode=ssh-probe`, and your SSH coordinates (`ssh_host`, `ssh_port`, `ssh_user`). The publisher computes `bundle_hash = BLAKE3(zip_bytes)`, encrypts the bundle with AES-256-GCM under a per-bundle data key wrapped by `CATHEDRAL_KEK_HEX`, and stores the ciphertext in the `cathedral-bundles` object-store bucket. Production storage runs on Cloudflare R2 via a path-style S3 client.
4. Cathedral SSHs into your declared host as `ssh_user`, snapshots your `~/.hermes/` into an isolated `cathedral-eval-<round>` profile, runs `hermes chat -q "<task>"` (full agentic loop with tool calls + skill execution) against that profile, captures the full forensic trail (state.db slice, sessions JSON, request dumps, skills, memories, logs), SCPs the trace bundle back, and tears down the eval profile. Your primary `~/.hermes/` is never modified. The returned card is treated as the agent's output for scoring.
5. Cathedral re-derives `task_hash` from the prompt bytes and `output_hash` from the produced card bytes, runs preflight + the six-dimension scorer, applies the first-mover delta, signs the resulting `EvalRun` projection with the Cathedral key, and persists it. Validator-side signature verification dispatches on `eval_output_schema_version` via `_SIGNED_KEYS_BY_VERSION` — v1 today, v2 (card excerpt + artifact manifest hash) ships gated behind `CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD`.

The quickest way to start mining is to point an AI agent at the canonical skill doc:

```
Read https://api.cathedral.computer/skill.md and follow it.
```

The skill doc carries the exact card schema, signing payload format, endpoints, and error codes. There is no client library; an agent that can `curl` and sign sr25519 can mine.

## What a validator does

The validator binary in this repo (`cathedral-validator`) runs four asyncio loops on the same sqlite database when `cathedral-validator serve` is invoked:

1. The legacy `/v1/claim` worker drains pending `claims` rows, fetches Polaris records, verifies signatures, scores the card, and writes `scores`. This is the pre-bundle intake path. It still boots today because some miners are still on it and because the worker is the writer of the legacy `scores` rows that the weight loop blends with `pulled_eval_runs`.
2. The pull loop (`cathedral.validator.pull_loop`) reads `GET /v1/leaderboard/recent` from the publisher every 30s by default, verifies every `EvalRun` projection against `CATHEDRAL_PUBLIC_KEY_HEX` (the Cathedral eval-signing pubkey from JWKS), and upserts to `pulled_eval_runs` keyed by `eval_run_id`. The loop is only spawned when `CATHEDRAL_PUBLIC_KEY_HEX` is set; if absent, startup logs `pull_loop_disabled`.
3. The weight loop joins the latest score per hotkey (across both `scores` and `pulled_eval_runs`) to the metagraph's uids, normalizes, and calls `subtensor.set_weights`.
4. The stall watchdog flips `health.stalled = true` if heartbeats stop landing.

The pull loop is live on SN39 mainnet validators, verifying publisher signatures against the pinned Cathedral pubkey and writing weight inputs. On-chain weight setting is also live (`weights.interval_secs = 1500`, `forced_burn_percentage = 0.0`; `burn_uid = 204` remains the fallback target when no positive miner scores exist). Weekly Merkle anchoring is wired in code but not yet running on a schedule. See **Status**.

Full procedure for someone running a validator: [docs/VALIDATOR.md](docs/VALIDATOR.md).

## Jobs live

The publisher's `card_definitions` table is seeded from `cathedralai/cathedral-eval-spec`. One job is live and accepts submissions today:

| Job ID | Jurisdiction | Topic |
|---------|--------------|-------|
| `eu-ai-act` | EU | EU AI Act enforcement and guidance |

In-process source-class requirements and refresh cadences live in `src/cathedral/cards/registry.py`. The eval-spec (`source_pool`, `task_templates`, `scoring_rubric`) lives in `card_definitions` and is queryable at `GET /v1/cards/eu-ai-act/eval-spec`.

The earlier 5-card launch plan (`us-ai-eo`, `uk-ai-whitepaper`, `singapore-pdpc`, `japan-meti-mic`) is deprecated for v1 launch. Existing production rows for those IDs are archived at Docker startup, so `POST /v1/agents/submit` and `GET /v1/cards/{id}/eval-spec` return 404 for them. New cards open as separate launch tracks once `eu-ai-act` ships clean and a real miner is earning on it.

Africa is tracked in [cathedralai/cathedral#24](https://github.com/cathedralai/cathedral/issues/24). Scope is open: pan-AU vs. country-specific.

## Architecture

```
miner ── POST /v1/agents/submit (attestation_mode='ssh-probe') ──▶ publisher (Railway, FastAPI)
                                       │
                                       ├─ encrypt + put cathedral-bundles (S3/R2)
                                       ├─ INSERT agent_submissions (status=queued)
                                       └─ orchestrator picks up
                                              │
                                              ▼
                                       SshHermesRunner (v2) / SshProbeRunner (v1)
                                       (live ssh-probe path; Cathedral SSHs into the
                                        miner-declared host as cathedral-probe)
                                              │
                                              ▼
                              miner host: hermes chat -q "<task>"
                              ├─ snapshot ~/.hermes/ -> cathedral-eval-<round>
                              ├─ Hermes runs the full agentic loop
                              │  (tool calls + skills + memory)
                              ├─ miner's LLM provider (Chutes, OpenRouter,
                              │  Anthropic, local llama.cpp, ollama, vLLM)
                              └─ emit Card JSON + trace bundle
                                              │
                                              ▼
                              Cathedral derives task_hash + output_hash,
                              runs preflight + score_card,
                              applies first-mover delta (1.00x runtime
                              multiplier in v1),
                              signs the public EvalRun projection,
                              INSERTs eval_runs + updates agent_submissions.
                                              │
                                              ▼
                              GET /v1/leaderboard/recent (signed projections)
                                              │
                                              ▼
                              cathedral.computer frontend hydrates every 30s
                              validator pull loop verifies + writes scores
```

## Verification modes

| Path | Mode | Verified by | Earns emissions | Ranks on leaderboard | Live in v1 |
|------|------|-------------|-----------------|----------------------|-------------|
| **BYO Box** | `ssh-probe` | Cathedral SSHs into the miner-declared host and invokes Hermes during the eval window; trace bundle captured | yes | yes | yes |
| **self-TEE** | `tee` | AWS Nitro / Intel TDX / AMD SEV-SNP attestation; runtime measurement matched against approved-runtime registry | yes | yes | spec-only; Nitro verifier wired, no live TEE miners |
| **discovery** | `unverified` | nothing | no | no (discovery surface only) | yes |

The submit endpoint takes `attestation_mode` as a form field. `unverified` submissions are stored encrypted, get a `status='discovery'` row, never enter the eval queue, and never appear on the leaderboard. They show up on the discovery surface so research material is not lost.

Hardware-attestation contract: [docs/ATTESTATION_CONTRACT.md](docs/ATTESTATION_CONTRACT.md).

## Status

Verified live v1 path, 2026-05-13:

- One job definition seeded (`eu-ai-act`), eval-spec served at `/v1/cards/eu-ai-act/eval-spec`. The earlier 4-card plan (`us-ai-eo`, `uk-ai-whitepaper`, `singapore-pdpc`, `japan-meti-mic`) is archived at Docker startup and returns 404.
- End-to-end ssh-probe pipeline: submit -> encrypt to R2 -> SSH into miner host -> snapshot `~/.hermes/` -> `hermes chat -q "<task>"` -> capture trace -> score -> sign -> publish.
- SN39 mainnet validators: pull loop verifying signed `EvalRun` projections every 30s, weight loop calling `subtensor.set_weights` every 1500s with `forced_burn_percentage = 0.0` so positive score mass routes to miners. `burn_uid=204` remains configured as the fallback when the score window has no positive miner scores. Cathedral consensus on mainnet is still pinned by the burn-majority subset, which is a separate operator concern from "weights are being set."
- Publisher: Railway, auto-deploy on push to `main`. TLS via Cloudflare.
- Provisioning: `scripts/provision_validator.sh` and `scripts/provision_miner.sh` stand up a validator or miner-probe from scratch on Ubuntu 22.04+; PM2 supervises both apps with systemd boot persistence; `bin/updater.sh` watches for signed git tags and reloads on release.

Wired in code, not yet running by default:

- On-chain Merkle anchoring (weekly `system.remarkWithEvent`). Merkle code path exists in `cathedral.publisher.merkle` and `cathedral.chain.anchor`; not running on a schedule yet.
- `synthetic_boolean_v1` SAT shadow lane. Public main has CNF URL transport, signed schema-5 rows, first-submitted receipt ordering, and validator pull support. Emissions stay disabled until operators set a positive SAT weight and seed operator-held challenges.

Not yet built:

- Live TEE miners. Nitro verifier exists in `cathedral.attestation.nitro`; TDX and SEV-SNP return 501 from the submit endpoint.
- Africa job ([#24](https://github.com/cathedralai/cathedral/issues/24)).

> **v3 substrate is experimental, not the live mining path.** The `src/cathedral/v3/` package (trajectory data substrate, `bug_repro` alpha, validator-local sandbox runner) ships behind separate entry points and is not invoked from the v1 publisher, validator, or miner code paths. v1 BYO Box plus the EU AI Act regulatory card remain the production mining and reward path until [#123](https://github.com/cathedralai/cathedral/issues/123) (publisher SSH runner for `bug_repro`) lands.

## Repo layout

```
cathedral/
├── src/cathedral/
│   ├── types.py            # Polaris-facing wire types (PolarisAgentClaim, Card, ScoreParts)
│   ├── v1_types.py         # publisher-side types (AgentSubmission, EvalRun, EvalTask, Merkle)
│   ├── config.py           # ValidatorSettings, MinerSettings
│   ├── auth/               # sr25519 hotkey signature verification
│   ├── attestation/        # self-TEE verifiers (Nitro live, TDX + SEV-SNP stubs)
│   ├── cards/              # registry, preflight, six-dimension score
│   ├── chain/              # Bittensor metagraph + weight setting + Merkle anchor
│   ├── evidence/           # Polaris evidence fetch + Ed25519 verify (legacy /v1/claim path)
│   ├── eval/               # task generator, orchestrator, polaris_runner, scoring_pipeline
│   ├── publisher/          # FastAPI: /v1/agents/submit, reads, skill.md
│   ├── storage/            # R2 client + bundle encryption + Hermes bundle validator
│   ├── validator/          # sqlite queue, worker, pull loop, weight loop, watchdog, health
│   ├── miner/              # claim submission client (legacy path)
│   └── cli/                # `cathedral`, `cathedral-validator`, `cathedral-miner`, `cathedral-publisher`
├── docker/cathedral-runtime/   # Runtime image experiments (built and pushed by GH Actions)
├── docs/                       # Architecture, attestation contract, validator runbook
├── config/                     # TOML defaults for mainnet, testnet, miner
├── scripts/                    # Systemd unit, install, dev helpers, stub Polaris
└── tests/                      # pytest, real Ed25519 keypair in conftest
```

## Quick start: miner

The fast path is to let an agent do it. Paste this into your AI agent:

```
Read https://api.cathedral.computer/skill.md and follow it. Mine the eu-ai-act card under the display name "<your-name>".
```

The skill doc carries everything: card schema, canonical signing payload, attestation modes, error codes. If you prefer to drive the legacy `/v1/claim` path from a CLI (for the existing Polaris-evidence flow, not the new agent-bundle pipeline):

```bash
git clone https://github.com/cathedralai/cathedral
cd cathedral
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

# Edit config/miner.toml: miner_hotkey, owner_wallet, validator_url
export CATHEDRAL_VALIDATOR_BEARER=<token>

cathedral-miner submit \
  --work-unit "card:eu-ai-act" \
  --polaris-agent-id agt_01H... \
  --polaris-run-ids run_01H... \
  --polaris-artifact-ids art_01H...
```

Full miner walkthrough: [docs/miner/QUICKSTART.md](docs/miner/QUICKSTART.md).

## Quick start: validator

```bash
git clone https://github.com/cathedralai/cathedral
cd cathedral
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]

# Fetch both pubkeys from the publisher's JWKS and pin them locally.
curl -s https://api.cathedral.computer/.well-known/cathedral-jwks.json | jq

# Copy and edit config/mainnet.toml (SN39 is the operator path; SN292 testnet
# is for protocol development via config/testnet.toml):
#   - network.validator_hotkey: local wallet hotkey NAME (the --wallet.hotkey
#                               you pass to btcli, e.g. "default"). NOT the
#                               ss58. The bittensor SDK opens the wallet by
#                               name and reads the ss58 off disk.
#   - network.wallet_name:      local Bittensor wallet (coldkey) name (usually edit)
#   - polaris.public_key_hex:   pin kid=polaris-runtime-attestation from JWKS

export CATHEDRAL_BEARER=$(openssl rand -hex 32)              # local /v1/claim auth
export CATHEDRAL_PUBLIC_KEY_HEX=<kid=cathedral-eval-signing> # gates the pull loop
# export CATHEDRAL_PUBLISHER_TOKEN=...                       # optional, future-facing

cathedral-validator migrate --config config/mainnet.toml
cathedral-validator serve   --config config/mainnet.toml
```

In another terminal:

```bash
cathedral chain-check --config config/mainnet.toml   # confirm hotkey + subtensor
cathedral health                                     # full snapshot
cathedral weights                                    # weight-set status word
cathedral registration                               # is the validator on the metagraph
```

Full validator procedure (mechanism, anti-cheating, requirements, known limits, how to verify a specific eval): [docs/VALIDATOR.md](docs/VALIDATOR.md). Operational runbook (systemd, logs, recovery): [docs/validator/RUNBOOK.md](docs/validator/RUNBOOK.md). v1.1.0 upgrade notes: [docs/validator/UPGRADING_TO_V1_1_0.md](docs/validator/UPGRADING_TO_V1_1_0.md).

You can confirm a validator is running v1.1.0 by querying `https://api.taostats.io/api/validator/weights/latest/v1?netuid=39` and checking `version_key=1001000` on their last weight-set.

## Documentation map

- [docs/VALIDATOR.md](docs/VALIDATOR.md): mechanism, anti-cheating, requirements, known limits, eval verification walkthrough
- [docs/validator/RUNBOOK.md](docs/validator/RUNBOOK.md): operational runbook for the validator binary
- [docs/miner/QUICKSTART.md](docs/miner/QUICKSTART.md): miner walkthrough for the live BYO Box path
- [docs/miner/QUICKSTART_LEGACY_V1_CLAIM.md](docs/miner/QUICKSTART_LEGACY_V1_CLAIM.md): legacy `/v1/claim` CLI walkthrough (retained for operators still on that path)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): validator binary internals (loops, modules, sqlite schema)
- [docs/ATTESTATION_CONTRACT.md](docs/ATTESTATION_CONTRACT.md): hardware-attestation and discovery wire formats
- [docs/protocol/CLAIM.md](docs/protocol/CLAIM.md): legacy `/v1/claim` wire format
- [docs/protocol/SCORING.md](docs/protocol/SCORING.md): six-dimension scorer, preflight, and v1 multipliers

## Future enhancements

- On-chain weekly Merkle anchoring of `EvalRun` projections.
- TDX and SEV-SNP TEE verifiers wired (live miners attest with hardware roots, not Polaris).
- Per-domain rubric profiles (legal vs. finance vs. science).
- Layer-2 audit replay: validators re-run a random sample of bundles on a different task and compare structural similarity to the submitted card.
- Approved-runtime registry moved on-chain (currently a git JSON file, see `docs/ATTESTATION_CONTRACT.md` §6.2).

## License

MIT. See [LICENSE](LICENSE). (c) 2026 cathedralai.
