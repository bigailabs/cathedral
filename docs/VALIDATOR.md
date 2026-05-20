# Validator notes

This document is the structural reference for validators. It explains what the eval pipeline does, what each anti-cheating mechanism protects against, what a validator operator needs to run one, and how to verify any specific eval on the live publisher.

The operational runbook (systemd unit, log filtering, weight-status table, recovery steps) lives in [validator/RUNBOOK.md](validator/RUNBOOK.md). This file is the conceptual companion: the "why each check exists" doc that lets a new operator reason about the system end-to-end.

## Mechanism

The eval pipeline runs inside the publisher process (`cathedral.publisher.app`) once a miner has submitted a bundle. v1's live emissions path is `attestation_mode='ssh-probe'` (BYO Box), set in `src/cathedral/publisher/submit.py`.

For each submission with status `queued`, the orchestrator (`cathedral.eval.orchestrator.EvalOrchestrator`) does the following:

1. **Promote to `evaluating`.** Single-tick state transition so a separate `run_once()` call observes the new state. Confirms the row is not double-claimed.
2. **Generate the eval task.** `cathedral.eval.task_generator.generate_task` produces a deterministic `EvalTask` per `(card_id, epoch, round_index)`. The prompt is the card's task template hydrated with the current `source_pool`. `task_hash = BLAKE3(prompt.encode('utf-8'))` pins the task bytes for downstream audit.
3. **Resolve the runner.** Dispatch by `attestation_mode`:
   - `ssh-probe` -> `SshProbeRunner` (v1 default) or `SshHermesRunner` (when `CATHEDRAL_PROBER_VERSION=v2`); BYO Box, Cathedral SSHs into the miner-declared host and invokes `hermes chat -q "<task>"` against an isolated `cathedral-eval-<round>` profile. This is the live path.
   - `bundle` -> `BundleCardRunner` (BYO Box, reads `artifacts/last-card.json` from the bundle, no attestation)
   - test modes (`stub*`) -> in-process stubs
4. **Dispatch the eval (live path: ssh-probe).** `SshProbeRunner.run` (or `SshHermesRunner.run` under `CATHEDRAL_PROBER_VERSION=v2`) does the following:
   - Opens an SSH connection to `ssh_user@ssh_host:ssh_port` declared on the submission, using Cathedral's universal probe key.
   - Snapshots the miner's primary `~/.hermes/` into an isolated `~/.hermes/profiles/cathedral-eval-<round>/` so the miner's working profile is never modified.
   - Invokes `hermes chat -q "<task>"` against the eval profile. Hermes runs the full agentic loop (tool calls, skills, memory) against the miner's configured LLM provider.
   - Captures the trace bundle (state.db slice, sessions JSON, request dumps, skills, memories, logs), SCPs it back, and tears down the eval profile.
   - Returns the produced Card JSON.
5. **Preflight.** `cathedral.cards.preflight.preflight(card)` (`src/cathedral/cards/preflight.py`):
   - Citations non-empty.
   - `no_legal_advice` is the literal boolean `true`.
   - `summary`, `what_changed`, `why_it_matters` non-empty after strip.
   - Every citation `200 <= status < 400`.
   - No legal-advice framing in `summary + action_notes + why_it_matters` (substring match against `LEGAL_ADVICE_PHRASES`: `"you should"`, `"we recommend that you"`, `"our advice is"`, `"as your lawyer"`, `"this constitutes legal advice"`).
   - On failure: the eval row is persisted with `weighted_score=0` and an `errors` entry; no rank update.
6. **Score.** `cathedral.cards.score.score_card(card, registry_entry)` produces a six-dimension `ScoreParts` in `[0.0, 1.0]` each:
   - `source_quality` (weight 0.30): share of citations from `OFFICIAL_SOURCE_CLASSES` (`government`, `regulator`, `court`, `parliament`, `law_text`, `official_journal`), plus up to `+0.20` for covering the registry entry's `required_source_classes`.
   - `maintenance` (weight 0.20): bands by `age_hours / cadence`. `<=1` gives 1.0, `<=2` gives 0.6, `<=4` gives 0.2, else 0.0.
   - `freshness` (weight 0.15): continuous decay. `ratio <= 1.0` gives 1.0, `ratio >= 4.0` gives 0.0, linear in between.
   - `specificity` (weight 0.15): length bands of `what_changed + why_it_matters`. `<100` gives 0.2, `<400` gives 0.6, `<1500` gives 1.0, else 0.7.
   - `usefulness` (weight 0.10): `+0.5` if `action_notes`, `+0.3` if `risks`, `+0.2` if `confidence > 0.5`, capped at 1.0.
   - `clarity` (weight 0.10): `summary` length in `[40, 800]` gives at least 0.4, with 1.0 awarded if 1-6 sentences.
   - Weights sum to 1.0; tunable by subclassing `cathedral.types.ScoreParts.weighted`.
7. **First-mover delta.** `cathedral.eval.scoring.first_mover_multiplier` (in `src/cathedral/eval/scoring.py`):
   - First mover for `(card_id, metadata_fingerprint)` -> multiplier 1.0.
   - Late mover beating incumbent by `+0.05` -> multiplier 1.0.
   - Late mover outside the 30-day window -> multiplier 1.0.
   - Otherwise -> multiplier 0.50.
8. **Runtime multiplier.** v1 applies a `1.00x` runtime multiplier to every scored submission, then caps the final score at `1.0`.
9. **Hash the output card.** `output_card_hash = BLAKE3(canonical_json(output_card_json))`. The pipeline hashes the literal dict the publisher both serves and stores, not a Pydantic re-render. This is what downstream verifiers pin.
10. **Sign the public projection.** `EvalSigner.sign` over `canonical_json` of:

    ```
    {
      id, agent_id, agent_display_name, card_id,
      output_card, output_card_hash,
      weighted_score, polaris_verified, ran_at
    }
    ```

    Canonical-JSON rules from `cathedral.v1_types.canonical_json`: sorted keys, no whitespace, UTF-8, with `signature`, `cathedral_signature`, and `merkle_epoch` excluded from the signed bytes. Signing key is `CATHEDRAL_EVAL_SIGNING_KEY` (32-byte raw Ed25519 private key, hex).

    See **Known limitations** for the served-vs-signed projection mismatch around extra projection fields.

11. **Persist + rank.** Insert into `eval_runs`. Recompute the submission's 30-day rolling average and `current_rank` within the card. Update `agent_submissions`.

The signed projection is the wire shape served by `GET /v1/leaderboard/recent` and `GET /v1/agents/{id}` (in `recent_evals[].cathedral_signature`). A validator's only trust input is the Cathedral public key and the projection bytes; everything else is derivable.

### The signed projection (EvalOutput)

Fields and rules:

| Field | Source | Notes |
|-------|--------|-------|
| `id` | uuid4 | eval_run row id |
| `agent_id` | `agent_submissions.id` | the miner's submission, not their hotkey |
| `agent_display_name` | submission | persisted for downstream UI without a join |
| `card_id` | submission | one of the registry entries |
| `output_card` | runtime response | the literal Card dict the publisher serves |
| `output_card_hash` | `BLAKE3(canonical_json(output_card))` | pinned so the projection is self-verifying |
| `weighted_score` | scorer + multipliers | post-clip, in `[0.0, 1.0]` |
| `polaris_verified` | runner | reflects whether a Polaris attestation was verified |
| `ran_at` | `_ms_iso(datetime.now(UTC))` | ISO-8601 UTC, millisecond precision, `Z` suffix |
| `cathedral_signature` | `EvalSigner.sign(...)` | base64 Ed25519, excluded from canonical bytes |
| `merkle_epoch` | weekly close job | excluded from canonical bytes; appended post-anchor |

The canonical bytes for verification are `json.dumps(dict_minus_excluded, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")`. Exact reference in `cathedral.v1_types.canonical_json`. The validator implementation that does this is `cathedral.validator.pull_loop.verify_eval_output_signature`.

The Cathedral signing pubkey is published at `GET /.well-known/cathedral-jwks.json` on the publisher. Validators should fetch it once during setup, then pin it locally as `CATHEDRAL_PUBLIC_KEY_HEX`. The validator binary reads only the pinned env var at startup; it does not auto-rotate from the same publisher it then trusts (key handoff must happen out of band).

### Remote signed weight source darkship

Local weight computation remains the default. Remote signed weights are opt-in
through `[weight_source].mode = "remote"` or `CATHEDRAL_WEIGHT_SOURCE=remote`.
Remote mode refuses startup unless the validator has a pinned policy public key
through `CATHEDRAL_POLICY_PUBLIC_KEY_HEX` or
`weight_source.cathedral_policy_public_key_hex`.

The corrected #155 vector contract is:

```python
class SignedWeightVector(BaseModel):
    schema_version: int = 1
    policy_version: int
    vector_id: str
    issued_at: str
    expires_at: str
    network: str
    netuid: int
    metagraph_block: int
    burn_hotkey: str
    burn_uid_snapshot: int | None
    weights_by_hotkey: dict[str, float]
    policy_hash: str
    key_id: str
    signature: str
```

`burn_hotkey` must appear in `weights_by_hotkey`; the optional
`burn_uid_snapshot` is logging context only. The validator maps every signed
hotkey, including the burn hotkey, to the live metagraph locally. Hotkeys that
are no longer registered are dropped and the remaining weights are normalized.
If no signed weight maps to a live UID, the validator refuses to call
`set_weights`.

Remote vector polling and chain weight setting use separate cadences.
`weight_source.poll_interval_secs` fetches and verifies vectors into durable
state. `weights.interval_secs` remains the only cadence that calls
`set_weights`, and the chain `version_key` remains `SPEC_VERSION`. The
validator persists accepted and applied policy versions in SQLite so rollback
protection survives restart. During publisher outage it can reuse the cached
accepted vector until `refuse_after_stale_minutes`; after that window it
refuses to set weights.

Publisher-side policy changes stay publisher-side. Burn share, lane shares,
and miner hotkey overrides are read while producing the signed vector, so
validators in remote mode do not need another binary update when those inputs
change.

## Anti-cheating

Each row below names a concrete attack and the mechanism that catches it. Citations point at the file enforcing the check.

| Attack | Mitigation | Where |
|--------|------------|-------|
| Hand-written cards posing as agent output | v1 live (ssh-probe): Cathedral SSHs into the miner-declared host and invokes Hermes itself, so the card was produced by the miner's running agent during the eval window, not pasted in after the fact. `unverified` submissions never reach the leaderboard. | `src/cathedral/eval/ssh_hermes_runner.py`, `src/cathedral/eval/ssh_probe_runner.py`; `src/cathedral/publisher/submit.py` mode dispatch |
| Copying another miner's card | Submission-time exact-bundle-hash and fuzzy display-name checks at intake; first-mover delta penalty (0.50x) for late copies that fail to beat the incumbent by 0.05 within 30 days. | `src/cathedral/publisher/similarity.py`; `src/cathedral/eval/scoring.py::first_mover_multiplier` |
| Citation hallucination | The runtime fetches every citation URL and computes `BLAKE3(bytes)` server-side. Preflight rejects any citation outside HTTP 200-399. | `docker/cathedral-runtime/server.py` (fetch + hash), `src/cathedral/cards/preflight.py::BrokenSourceError` |
| Legal-advice framing | Preflight scans the concatenated summary, action notes, and why-it-matters for prohibited phrases. | `src/cathedral/cards/preflight.py::LEGAL_ADVICE_PHRASES` |
| Bundle tampering at rest | Per-bundle AES-256-GCM data key wrapped under a master KEK; ciphertext stored in `cathedral-bundles`; nonce bound to the wrapped key id. Mismatched nonces fail decrypt. | `src/cathedral/storage/crypto.py::encrypt_bundle` / `decrypt_bundle` |
| Sybil display-name collision | NFKC-normalized + lowercased + whitespace-collapsed display names; Levenshtein ratio >= 0.85 against names from the last 7 days rejects. Fingerprint = `BLAKE3(display_name_norm | bundle_size_bucket_1KiB)` collisions across hotkeys rejected. | `src/cathedral/publisher/similarity.py::run_similarity_check` |
| Same hotkey, same bundle re-upload | `idx_agent_unique` UNIQUE index on `(miner_hotkey, card_id, bundle_hash)` -> 409. | `src/cathedral/publisher/repository.py` (schema), 409 mapping in `submit.py` |
| Cross-hotkey bundle theft (same zip bytes, different hotkey) | Exact bundle-hash duplicate check before any storage write. 409. | `similarity.run_similarity_check` |
| Backdated `submitted_at` (gaming first-mover) | Server clock is the sole source of truth for the persisted `submitted_at` and `first_mover_at`. The client-supplied value is used only to verify the hotkey signature; a ±5 minute skew window rejects obvious back/forward-dating. | `src/cathedral/publisher/submit.py` CRIT-1 |
| Output card with miner-supplied identity fields | The scoring pipeline overrides `id`, `worker_owner_hotkey`, and `polaris_agent_id` with server-trusted values before the card is validated or hashed. | `src/cathedral/eval/scoring_pipeline.py::score_and_sign` CRIT-9 |

The honest gap: a sophisticated miner who buys real GPUs, runs an approved Hermes build, and feeds it deliberately misleading source material will pass every attestation check. The attestation only proves "this approved runtime produced this output for this task." Source-quality scoring is what catches misleading content, and that's a scoring concern, not an attestation one.

## Requirements for running a validator

### Hardware

A small CPU-only box. No GPU.

- **RAM:** 4 GB minimum, 8 GB recommended
- **Disk:** 50 GB SSD
- **CPU:** 2 vCPU minimum, 4 vCPU comfortable
- **Network:** stable outbound HTTPS. The pull loop polls `/v1/leaderboard/recent` every 30s by default (`publisher.pull_interval_secs`) and pulls a few KB per round.

### Prerequisites

- A Bittensor sr25519 hotkey registered on **SN39** (mainnet, the operator path for v1).
- A Linux host (any distro, x86_64 or aarch64).
- **Python 3.11 or 3.12.** Newer Python versions are not yet tested.
- SQLite (default) or PostgreSQL for the local store. SQLite is fine for v1.

> Testnet (SN292) is retained for protocol development and continues to ship with `config/testnet.toml`. Operators should run against SN39 mainnet; testnet is not the supported operator path.

### Networking

- **Inbound: nothing public required.** The validator binds an HTTP admin/health server on `0.0.0.0:9333` by default (configurable via `http.listen_host` / `http.listen_port`). Bind it to `127.0.0.1` if you only want local access, or leave on `0.0.0.0` and firewall it. You do not need this port reachable from the public internet, since Cathedral never connects to you. The bearer-protected admin endpoints are for your own ops tooling.
- **Outbound: HTTPS 443 only.** The validator initiates outbound connections to `api.cathedral.computer` (publisher) and your configured subtensor endpoint. No other outbound dependencies.
- **No NAT / port-forwarding required.** Unlike miner-style subnets, Cathedral validators don't receive inbound traffic from miners or other validators. Pull-only.

### What you do NOT need

- A GPU. The validator does no model inference.
- Write access to the `cathedral-bundles` bucket. Validators verify signed projections from the publisher; they do not re-decrypt bundles.
- Polaris API tokens. Those are publisher-side credentials.
- The KEK (`CATHEDRAL_KEK_HEX`). Validators never see plaintext bundles.

## Quickstart

### 1. Install

```bash
git clone https://github.com/cathedralai/cathedral
cd cathedral
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

This installs four console scripts: `cathedral`, `cathedral-validator`, `cathedral-miner`, `cathedral-publisher`.

### 2. Fetch the public keys

The publisher serves a JWKS document at `GET /.well-known/cathedral-jwks.json`. Fetch it once during setup, then pin the values locally so all signature verification runs against your pinned copy and never auto-rotates from the same publisher you then trust.

```bash
curl -s https://api.cathedral.computer/.well-known/cathedral-jwks.json | jq
```

The document carries two keys:

- `kid: cathedral-eval-signing`: Cathedral signs every `EvalRun` projection with this key. Pin as `CATHEDRAL_PUBLIC_KEY_HEX` in the validator's env.
- `kid: polaris-runtime-attestation`: Polaris signs runtime attestations with this key. Pin as `polaris.public_key_hex` in your TOML config (used by the legacy `/v1/claim` worker that still boots when `cathedral-validator serve` starts up).

Today's values (May 2026; always re-fetch from the URL above before pinning so you catch any rotation):

```bash
# CATHEDRAL_PUBLIC_KEY_HEX  (kid: cathedral-eval-signing)
# Gates the pull loop. If absent, startup logs `pull_loop_disabled` and
# the validator falls back to legacy-only operation.
export CATHEDRAL_PUBLIC_KEY_HEX=10890a66aa752479cb3b634f366d7bd27c374324d83f88d2d6b69ab066f25e26

# polaris.public_key_hex     (kid: polaris-runtime-attestation)
# Goes in the TOML config, not env. Required because `cathedral-validator serve`
# still constructs the legacy `/v1/claim` worker which verifies Polaris evidence.
# Value: 50b8a077ab857c91a9b4f2b94295e81f0f01e4ec1fa5b3e9fd4073ea00def24c
```

Validators do not need to export `POLARIS_ATTESTATION_PUBLIC_KEY` themselves. That env var is publisher-side: the publisher uses it to verify Polaris attestations before signing the `EvalRun` projection. Validators verify the publisher's signature (the Cathedral one), not Polaris's.

### 3. Set the bearer token

Set `CATHEDRAL_BEARER` to any non-empty string. This is the local validator's bearer token for its own `/v1/claim` endpoint (the legacy miner-claim intake path). It is not publisher-read auth and you do not send it to anyone; the publisher does not read it. The validator binary requires it at startup so the `make_bearer_dep` dependency builds cleanly.

```bash
export CATHEDRAL_BEARER=$(openssl rand -hex 32)
```

`CATHEDRAL_PUBLISHER_TOKEN` is optional and future-facing. If you set it, the pull loop sends it as `Authorization: Bearer <token>` to the publisher. The publisher does not currently enforce auth on `/v1/leaderboard/recent`, so leaving it unset is the right default today; the env var is wired so a later release can flip on server-side enforcement without a validator code change.

### 4. Configure

Copy `config/mainnet.toml` (SN39, the operator path) and edit:

- `network.validator_hotkey`: your local Bittensor wallet hotkey NAME (the value you pass to `btcli` as `--wallet.hotkey`, e.g. `default`). Not the ss58 address. The bittensor SDK opens the wallet by name and reads the ss58 off the on-disk key file; `cathedral chain-check` then verifies that ss58 is registered on the subnet's metagraph.
- `network.wallet_name`: local Bittensor wallet (coldkey) name (defaults to `cathedral-validator`; change if your wallet is named differently).
- `polaris.public_key_hex`: the Polaris runtime-attestation pubkey from the JWKS document above (required; the legacy worker is constructed even when only the pull loop is doing real work).

Env vars the validator reads at startup:

- `CATHEDRAL_BEARER`: required. Local validator bearer for `/v1/claim`.
- `CATHEDRAL_PUBLIC_KEY_HEX`: required to enable the pull loop. If unset, the pull loop is disabled and the validator logs `pull_loop_disabled`.
- `CATHEDRAL_PUBLISHER_TOKEN`: optional. Forwarded to the publisher only when `[publisher].api_token_env` is configured in the TOML.

### 5. Bring up

```bash
cathedral-validator migrate --config config/mainnet.toml
cathedral chain-check       --config config/mainnet.toml  # confirm hotkey + subtensor
cathedral-validator serve   --config config/mainnet.toml
```

(For protocol development against SN292, swap `config/mainnet.toml` for `config/testnet.toml`.)

Operational follow-on (systemd unit, log filtering, weight-status table, recovery): [validator/RUNBOOK.md](validator/RUNBOOK.md).

## FAQ

### Do I need any port open to the public internet?

No. Cathedral never connects back to your validator. The validator binds an HTTP admin/health server on `0.0.0.0:9333` by default (configurable via `http.listen_host` / `http.listen_port`) but that's for your own local ops tooling. Bind it to `127.0.0.1` or firewall it from the public internet. Outbound HTTPS 443 to `api.cathedral.computer` (publisher) and your subtensor endpoint is the only network requirement.

### Do I need a GPU?

No. The validator does no model inference. A small CPU-only box is the recommended deployment.

### Where do I get `CATHEDRAL_BEARER`? Who do I send it to?

You generate it yourself, locally:

```bash
export CATHEDRAL_BEARER=$(openssl rand -hex 32)
```

You don't send it to anyone. `CATHEDRAL_BEARER` is the local validator's bearer for its own `/v1/claim` endpoint (the legacy intake path the worker still drains). It is not publisher-read auth. The pull loop reads from the publisher without bearer auth today; if a future release adds publisher-side enforcement, that token is `CATHEDRAL_PUBLISHER_TOKEN` (optional, wired via `[publisher].api_token_env`).

### Where do I get the Cathedral and Polaris public keys?

From the JWKS endpoint, served live by the publisher:

```bash
curl -s https://api.cathedral.computer/.well-known/cathedral-jwks.json | jq
```

Use `kid: cathedral-eval-signing` for the `CATHEDRAL_PUBLIC_KEY_HEX` env var. Use `kid: polaris-runtime-attestation` for `polaris.public_key_hex` in your TOML config. Both are 64-character lowercase hex strings, exactly 64 chars, no leading-zero padding, no whitespace, no quotes. If your copy is a different length, recopy from the URL above; Ed25519 pubkeys cannot be padded.

Fetch the JWKS once during setup and pin it locally. Do not configure the validator to re-fetch at runtime from the same publisher whose signatures it then trusts. That defeats the purpose of pinning.

### What if my pubkey is the wrong length?

You miscopied. Ed25519 pubkeys are 32 random bytes encoded as 64 lowercase hex characters. They cannot be padded with zeros to reach 64 chars — left-padding produces a different point on the curve and every signature verification will silently fail. Re-fetch from the JWKS endpoint above.

### What does the validator actually do?

`cathedral-validator serve` wires four asyncio loops inside the FastAPI lifespan:

- **worker**: drains the legacy `/v1/claim` queue, fetches Polaris evidence, verifies it, scores it. This is the pre-bundle intake path; the worker is still constructed by `serve` today, which is why `polaris.public_key_hex` remains required in the TOML.
- **pull loop**: polls `GET /v1/leaderboard/recent` on the publisher every 30s by default, verifies each `EvalRun` projection with `CATHEDRAL_PUBLIC_KEY_HEX`, and upserts `pulled_eval_runs` rows. Only spawned when `CATHEDRAL_PUBLIC_KEY_HEX` is set; otherwise startup logs `pull_loop_disabled`.
- **weight loop**: joins the latest score per hotkey to the metagraph uids, normalizes, and calls `subtensor.set_weights` on the configured interval.
- **stall watchdog**: flips `health.stalled` true if heartbeats stop landing.

No miner connections, no model inference, no decrypting bundles.

### Why doesn't the validator need to verify Polaris attestations directly?

The publisher verifies the Polaris attestation before it signs the `EvalRun` projection. The Cathedral signature you verify locally is the publisher's attestation that *it* verified Polaris. You verify one signature per eval, the publisher's, against the key you pinned in step 2 of the Quickstart.

### How much disk will I grow into?

Scored evals are compact rows (a few KB each). At current throughput (~20 evals/day across all cards), expect a few hundred MB over the first six months. The 50 GB SSD recommendation is mostly headroom for systemd journal, OS, and Bittensor wallet — the validator's own state is small.

### Can I run multiple validators from the same host?

Yes — different `[network]` sections in separate config files, different `listen_port`, different `database_path`. Bittensor validator-binding is per-hotkey, not per-host, so each instance needs its own hotkey registered on the relevant subnet.

## Future enhancements

Listed in the order most likely to ship:

- **On-chain weekly Merkle anchoring.** `cathedral.publisher.merkle.epoch_for` and `cathedral.chain.anchor` exist; the missing piece is a scheduler that calls `system.remarkWithEvent` once per epoch with the Merkle root over the epoch's `EvalRun`s.
- **TEE attestation verifiers wired live.** `cathedral.attestation.nitro` is implemented; TDX and SEV-SNP return 501 from the submit endpoint. The contract is in [ATTESTATION_CONTRACT.md](ATTESTATION_CONTRACT.md).
- **Layer-2 audit replay.** Validators randomly sample a bundle, re-run the miner's `soul.md` on a different task, and compare structural similarity (citation overlap, source-class profile, summary length) against the submitted card. Catches a miner who passes attestation by running an approved runtime but feeds it cherry-picked sources.
- **Per-domain rubric profiles.** Different weights for legal vs. finance vs. science cards. The publisher already carries `scoring_rubric` per `card_definition`; the in-process registry needs to read from there instead of `ScoreParts.weighted`.
- **Approved-runtime registry on-chain.** Currently a git JSON file (see [ATTESTATION_CONTRACT.md](ATTESTATION_CONTRACT.md) §6.2); will move to a multi-sig-controlled chain extrinsic when the Hermes release cadence stabilizes.

## Known limitations

These are the rough edges a validator operator should understand before depending on the system.

- **Single publisher.** Today there is one Cathedral publisher (`api.cathedral.computer`). Validators verify its signature, but the publisher is a centralization point for intake, eval orchestration, and signing. Distributed publishers are post-v1.
- **Citation re-fetch is best-effort.** The runtime fetches every cited URL and the publisher accepts statuses in `[200, 400)`. Regulator sites can rate-limit, redirect, or return `202 + empty body`. Anything outside that range fails preflight; some legitimate sources will fail intermittently. Miners learn to pin official mirrors.
- **Hotkey-keyed history.** Score history is keyed by `miner_hotkey` ss58. Losing the hotkey means losing the history; there is no recovery.
- **No live TEE miners.** Nitro verifier is wired; TDX and SEV-SNP return 501. self-TEE is spec-only today.
- **Legacy `/v1/claim` worker still boots alongside the pull loop.** `cathedral-validator serve` constructs the worker even though most miners now flow through the bundle pipeline. The worker stays in place because some operators still post to `/v1/claim`, and because the worker is the writer of the legacy `scores` rows that the weight loop blends with `pulled_eval_runs`. Both paths feed the same weight vector via `latest_score_per_hotkey`.
- **Served projection includes fields outside the signed payload.** The publisher signs over nine fields (`id, agent_id, agent_display_name, card_id, output_card, output_card_hash, weighted_score, polaris_verified, ran_at`; see `src/cathedral/eval/scoring_pipeline.py::score_and_sign`). The `_eval_run_to_output` projection served at `/v1/leaderboard/recent` and `/v1/agents/{id}` additionally carries `cathedral_signature`, `merkle_epoch`, and may carry future non-signed fields. A validator must allow-list the signed fields before canonicalizing. The eval-verification walkthrough below does this explicitly.

## How to verify a specific eval

Pick any agent from the live leaderboard (e.g. `https://cathedral.computer/cards/eu-ai-act/`). Grab its `agent_id` and run:

```bash
curl -s https://api.cathedral.computer/v1/agents/{agent_id} | jq '.recent_evals[0]' > eval.json
```

The object has the public EvalOutput projection plus `cathedral_signature`. Verify in this order. Each step is a yes/no; if any fails, the eval is invalid.

**1. Cathedral signature.**

Build the canonical bytes from the nine signed fields and verify with the Cathedral public key. The served projection carries extras (`cathedral_signature`, `merkle_epoch`, and future non-signed fields) that are not part of the signed payload; strip them before canonicalizing.

```python
import base64, json
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

eval_run = json.load(open("eval.json"))
SIGNED_KEYS = {
    "id", "agent_id", "agent_display_name", "card_id",
    "output_card", "output_card_hash",
    "weighted_score", "polaris_verified", "ran_at",
}
payload_dict = {k: eval_run[k] for k in SIGNED_KEYS if k in eval_run}
canonical = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")

pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(CATHEDRAL_PUBLIC_KEY_HEX))
sig = base64.b64decode(eval_run["cathedral_signature"])
pk.verify(sig, canonical)   # raises InvalidSignature if bad
```

Reference for the publisher-side signing payload: `cathedral.eval.scoring_pipeline.score_and_sign` (the `public_payload` dict). See **Known limitations** for the served-vs-signed mismatch the explicit allow-list above works around.

**2. Output hash.**

Recompute `BLAKE3(canonical_json(output_card))` and confirm it matches `output_card_hash`:

```python
import blake3
canonical_card = json.dumps(eval_run["output_card"], sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
assert blake3.blake3(canonical_card).hexdigest() == eval_run["output_card_hash"]
```

**3. Citations re-fetch.**

For each entry in `output_card.citations`, GET the URL, compute `BLAKE3` of the response body, and compare to `citation.content_hash`. Confirm the HTTP status is in `[200, 400)`. Reference implementation lives in the runtime: `docker/cathedral-runtime/server.py`.

External regulator sites can rate-limit or change between miner fetch and your re-fetch. A mismatch here is a soft signal, not a definitive forgery proof; but a citation that returns 404 today and 200 at miner-fetch time is a yellow flag worth investigating.

**4. Score reproduction.**

Re-run the six-dimension scorer against the `output_card` and the registry entry for `card_id`:

```python
from cathedral.cards.score import score_card
from cathedral.cards.registry import CardRegistry
from cathedral.types import Card

card = Card.model_validate(eval_run["output_card"])
entry = CardRegistry.baseline().lookup(eval_run["card_id"])
parts = score_card(card, entry)
recomputed = parts.weighted()
# Apply first-mover delta as in src/cathedral/eval/scoring_pipeline.py
# to reproduce eval_run["weighted_score"], capped at 1.0. In v1 the runtime
# multiplier is 1.00x.
```

The first-mover delta requires the submission's `metadata_fingerprint` and the historical incumbent score, which need a DB join; the formulas are in `cathedral.eval.scoring`.

**If any step fails, the eval is invalid.** Step 1 covers cryptographic integrity. Step 2 confirms the served card matches the hashed projection. Step 3 confirms citation provenance. Step 4 confirms the publisher applied the rubric honestly.
