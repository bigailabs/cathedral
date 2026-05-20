# Releases

Canonical release notes for the Cathedral subnet. Mirrored to GitHub at
[github.com/cathedralai/cathedral/releases](https://github.com/cathedralai/cathedral/releases).

Versioning follows the runtime image: `v<major>.<minor>.<patch>` tracks
`docker/cathedral-runtime/CATHEDRAL_RUNTIME_VERSION`. A new tag is cut
whenever the producer-side surface changes in a way miners or validators
need to know about.

---

## v1.1.23 - SAT shadow hardening on public main

**Date:** 2026-05-20

**Headline:** Signed public release for the SAT shadow path hardening on main while SAT weight remains `0.0` by default.

### Added

- **Public SAT CNF transport.** Miners receive a public CNF URL plus hash metadata rather than an inline CNF body in the prompt. Fetch tokens gate the public endpoint so challenge bodies are not fetchable before announcement.
- **First-submitted SAT receipt ordering (#166).** The publisher records SAT stdout receipt before trace collection, verification updates receipt state, and durable winner selection picks the earliest valid receipt only after earlier receipts resolve.
- **Remote signed weights.** Validators can opt into a signed remote weight vector path while preserving the local fallback path.

### Changed

- **Global zero-score kill switch (#158).** `CATHEDRAL_ZERO_ALL_SCORES=true` now zeros both legacy EvalRun rows and SAT schema-5 rows.
- **Public feed remains hash-only for SAT.** Schema-5 rows expose hashes and score metadata, not raw CNF bodies, fetch tokens, raw answers, or verifier details.

### Verification

- Public shadow E2E ran a toy SAT challenge through miner stub -> publisher -> signed schema-5 row -> validator pull.
- SAT aggregation stayed inert with SAT weight `0.0`; a nonzero test weight proved the gate without changing the default.

### Operator note

- This is not a scored SAT launch. Challenge corpus material is operator-held and not committed to this repo.
- Tag-watching validators can update to this release through the signed-tag updater.

---

## v1.1.22 - SAT launch rails and hardened updater

**Date:** 2026-05-19

**Headline:** Public release tag for SAT launch rails, the synthetic boolean task-family scaffold, and the hardened validator updater.

### Added

- **Task Family Contract scaffold + `synthetic_boolean_v1` stub (#150).** Adds the lane contract boundary for public problem data versus publisher-side hidden metadata.
- **Synthetic boolean launch rails (#152).** Wires the SAT task-family path behind launch controls while keeping SAT weight disabled by default.
- **Validator updater hardening (#161).** Pins the expected remote, checks ancestry before updating, supports operator env overrides, runs migrations after checkout, and exits after a successful update so PM2 respawns from the new on-disk script.

### Operator note

- `v1.1.23` supersedes this release as the latest public signed tag.
- The SAT path remains shadow-only unless an operator intentionally configures positive SAT weight and operator-held challenge material.

---

## v1.1.21 - v4 cathedral_engine on main (dark-shipped)

**Date:** 2026-05-18

**Headline:** v4 publisher-side challenge runtime lands on main. Dark-shipped: no v4 weight, no v4 feed, no validator pull loop, no SSH transport wired yet. Cuts a clean rollback point for the engine merge so the remaining v4 launch work (transport, validator integration, real signing in driver, production corpus) can ship as independent releases.

### Added

- **v4 cathedral_engine (#133).** Publisher-side private challenge runtime: `CathedralEngine` with `load_task`, `load_and_scramble_task`, `build_bundle_and_handle`, `verify_miner_submission`, `package_elite_telemetry`. Jailed oracle subprocess with `REPRO_BUDGET_SECONDS=3.0` and `BOOKKEEPING_BUDGET_SECONDS=0.20`. Bookkeeping/repro budgets asserted by `tests/v4/test_benchmark.py`.
- **v4 wire types.** `MinerBundle` (wire-safe broken-state, no answer) + `PublisherHandle` (operator-only clean_state, rename_map). Type system enforces the split.
- **v4 isomorphic scrambler.** `IsomorphicScrambler` / `MinerArena` in `arena/sandbox.py` rotate identifiers + file paths so identical synthetic bug surfaces in unique scrambled forms per task.
- **v4 jailed oracle.** `oracle/jail.py` + `oracle/patch_runner.py`: namespaced (unshare) subprocess with pgrp-kill on timeout, RLIMIT_CPU + RLIMIT_AS bounded, network-isolated, tmpfs scratch.
- **v4 sign/verify helpers.** `cathedral.v4.sign.build_signed_v4_row` delegates to the existing v3 `EvalSigner` rather than reinventing signing. Validator no-execution guarantee in `tests/v4/test_validator_no_execution.py`.
- **v4 vault bases.** `python_fastapi_base` + `ts_prisma_base` for synthetic challenges.
- **v4 test fixtures.** Three in-tree synthetic rows under `tests/v4/fixtures/synthetic_rows/` (toy sign-flips, exercise the loader only). Production v4 corpus rows live under `CATHEDRAL_V4_CORPUS_PATH`, operator-only.
- **v4 test suite.** 58 tests across engine, oracle (incl. jailed), schemas, sign/verify, arena, benchmark, validator-no-execution, ts_prisma_vault.

### Not yet wired

- No SSH transport. `SshHermesRunner` only has `run_bug_isolation_challenge` (v3); no `run_v4_*` method exists.
- No validator v4 pull loop. `config/mainnet.toml` has `v3_bug_isolation_weight` only; no `v4_*_weight` yet.
- No production v4 corpus. Only 3 in-tree test fixtures.
- The local v4 E2E driver in `~/Documents/TOOLS/scripts/cathedral-v4-e2e/` exercises engine + oracle + canonical envelope but not the final signed wire row (`build_signed_v4_row`).

### Mainnet behavior

- `config/mainnet.toml` unchanged: `forced_burn_percentage = 95.0`, `v3_bug_isolation_weight = 0.0`.
- No `v4_*_weight` setting introduced (would have zero effect without validator pull).
- v3 feed remains gated by `CATHEDRAL_V3_FEED_ENABLED`.

### Do not move v4 emissions

Until transport (SSH or other), validator pull loop, real signed-row driver path, and production corpus are all wired and pass testnet E2E for at least one week. v3 stays the launch lever; v4 is dark-shipped engine code only.

---

## v1.1.20 - v3 full Hermes package capture (feed off)

**Date:** 2026-05-17

**Headline:** v3 `bug_isolation_v1` now captures task-scoped full
Hermes packages and private score records, so the lane can collect
labeled trajectory data once operators intentionally enable the feed.

### Added

- **Full Hermes package capture for v3 bug isolation (#134).**
  `SshHermesRunner.run_bug_isolation_challenge()` now mirrors the
  v1 card path by assembling a task-scoped Hermes package for each
  v3 challenge: state database snapshot, sessions, request dumps,
  memories, skills, logs, stdout, optional repair stdout, manifest,
  and bundle hash.
- **Private v3 score sidecar (#134).** The publisher can write a
  private `score_record.json` beside each package with the hidden
  oracle, parsed claim, score parts, weighted score, signature, bundle
  hash, and manifest hash. Public feed rows do not expose oracle
  fields, package URLs, manifest URLs, or score sidecar URLs.
- **v3 dataset catalog/export skeleton (#134).** New private catalog
  and export helpers create training-oriented JSONL shapes from score
  records while keeping hidden oracle data out of public and SFT/RM
  outputs.
- **Operator launch docs (#134).** New v3 launch-readiness, testnet
  E2E, mainnet launch-rule, and dataset docs describe the required
  testnet row, package upload, score sidecar, public-feed leak check,
  and rollback path.

### Operator note

- This release does **not** launch v3 on mainnet.
- `CATHEDRAL_V3_FEED_ENABLED` remains off unless an operator explicitly
  sets it.
- `config/mainnet.toml` still routes 95% to burn with
  `forced_burn_percentage = 95.0`.
- `config/mainnet.toml` still keeps v3 at zero weight with
  `v3_bug_isolation_weight = 0.0`.
- Do not move meaningful emissions to v3 until the SN292 testnet E2E
  has produced a signed v3 row, a full package, a private score
  sidecar, validator pull verification, and 24 hours of clean mainnet
  collection with no public oracle leakage.

---

## v1.1.19 - protective mainnet burn while v3 corpus hardens

**Date:** 2026-05-17

**Headline:** Mainnet validator policy routes 95% of SN39 weight to the
subnet owner burn uid until the v3 `bug_isolation_v1` corpus produces
launch-grade per-miner signal.

### Changed

- **Mainnet forced burn restored to 95%.** `config/mainnet.toml` now
  sets `forced_burn_percentage = 95.0`, leaving 5% for measured miner
  signal while the current v1 EU AI Act lane is too saturated to
  discriminate miner quality at scale.
- **Managed SN39 config sync follows the release policy.**
  `MAINNET_FORCED_BURN_PERCENTAGE` is now `95.0`, so managed validator
  startups rewrite stale SN39 mainnet configs back to the protective
  burn setting after signed-tag updates.

### Operator note

- No v3 feed or v3 weight change is included in this release.
  `CATHEDRAL_V3_FEED_ENABLED` remains off and
  `v3_bug_isolation_weight` remains `0.0`.
- Revert this burn percentage toward `0.0` only after a real
  `bug_isolation_v1` testnet E2E produces signed rows and validator
  pull verification, and after the private corpus passes red-team
  challenge checks.

---

## v1.1.18 - v3 bug isolation wiring + private corpus loader (feed off)

**Date:** 2026-05-16

**Headline:** Validator + publisher now ship the full v3
`bug_isolation_v1` lane, with the corpus loaded from operator-private
storage at runtime. The lane stays inert until the publisher
operator sets `CATHEDRAL_V3_CORPUS_PATH`, restarts, and flips
`CATHEDRAL_V3_FEED_ENABLED=true`. No miner-side or validator-side
action required for this tag beyond upgrading.

### Added

- **v3 `bug_isolation_v1` orchestrator lane (#128).** The publisher's
  SSH Hermes path now dispatches a bug-isolation challenge after a
  full EU AI Act eval completes, scores the miner's structured claim
  statically against a hidden oracle, signs the result under schema
  v3, and persists it. Validators with the new keyset accept and
  pull these rows. Gated behind `CATHEDRAL_V3_FEED_ENABLED` (default
  off); production corpus also empty by default, so no real v3 jobs
  run after upgrade until the operator opts in.
- **`epoch_salt` bound into the v3 signed payload (#128).** The salt
  that derives `challenge_id_public` is now part of the signed
  subset, so a man-in-the-middle cannot relabel the epoch a row came
  from while keeping the public id intact for cross-epoch
  answer-sharing.
- **Private corpus loader (#130).** Real challenge rows are a hidden
  oracle and live entirely outside this public repo.
  `cathedral.v3.corpus.private_loader.load_private_corpus()` reads
  operator-curated JSON from the path in `CATHEDRAL_V3_CORPUS_PATH`,
  validates each entry through `ChallengeRow.model_validate(...)`,
  rejects `UNVERIFIED_` ids and `swebench`/`SWE-bench` source
  markers, and caches in-process. `PILOT_CORPUS` in `seed_pilot.py`
  is permanently `()`.
- **Boot-time corpus preload (#130).** Publisher lifespan now calls
  the loader at startup whenever `CATHEDRAL_V3_CORPUS_PATH` is set,
  so the operator's preflight check (`corpus_loaded path=... rows=N`
  in the boot logs) succeeds independently of the feed flag.
- **`CATHEDRAL_V3_BUG_ISOLATION_WEIGHT` blending env (#128).** Lets
  validator operators dial v3's contribution to the per-hotkey
  weight blend without a code change. Defaults to `0.0`, holding v3
  at zero weight until each operator explicitly opts in.

### Changed

- **v3 keyset locked in three mirrors.** The signed-payload keyset
  for `eval_output_schema_version=3` is asserted byte-equal across
  `cathedral.v3.sign._V3_SIGNED_KEYS`,
  `cathedral.eval.v2_payload._SIGNED_KEYS_BY_VERSION[3]`, and
  `cathedral.validator.pull_loop._SIGNED_KEYS_BY_VERSION[3]` by
  tests. Future drift will fail CI loudly rather than break verify
  silently in the field.
- **Latency anomaly handling in the validator pull loop.** v3 rows
  are bucketed v1 vs v3 in `latest_pulled_score_per_hotkey` instead
  of mean-of-means by task_type. Historical `task_type='unknown'`
  rows no longer get inflated weight against newly-typed entries.

### For validator operators

Upgrade as usual; the `cathedral-updater` PM2 app will pick up this
signed tag automatically. No env or config changes required to stay
at parity. **Do not** set `CATHEDRAL_V3_BUG_ISOLATION_WEIGHT` above
`0.0` until a separate operator DM gives you the go-ahead; v3 has
not yet been exercised end-to-end on testnet.

### For miners

No action required. The v3 lane stays off across the fleet at this
tag. A future tag will document the miner-visible Hermes prompt
shape (`FINAL_ANSWER` JSON block, claim schema, expected stdout
contract) when the corpus and feed flip are ready.

### Not in this release

- A populated production corpus. Real rows live in
  operator-controlled private storage (see
  `docs/v3/corpus/PRIVATE_CORPUS_STORAGE.md`).
- Mainnet v3 emissions. Flag and weight both default off; live-feed
  enablement is gated on the operator preflight ritual in
  `src/cathedral/v3/corpus/CORPUS_TODO.md`.

---

## v1.1.7 - Prober: hermes chat -q (full agentic loop)

**Date:** 2026-05-13

**Headline:** The SSH prober now invokes `hermes chat -q` (full agentic
loop with tool calls + skill execution + multiple model turns + memory
reads) instead of `hermes -z` (one-shot `model.generate(prompt)` with
no agent loop). Cathedral's value prop is verifying the agent actually
did work — fetching source URLs, calling tools, reasoning across
turns. The `-z` invocation stripped exactly that out.

### Fixed

- **Prober no longer bypasses the Hermes agent loop.** `hermes -z` was
  a one-shot model call: no tool calls, no skill execution, no URL
  fetching, no memory reads. Iota1's test tonight (submission
  `13bb4a12`) confirmed the failure mode: Hermes returned "I apologize
  for the difficulties in accessing real-time information about
  Article 5 of the AI Act..." because `-z` doesn't run the agent that
  would have fetched the source. v1.1.7 switches to `hermes chat -q`,
  which runs the full Hermes agent loop end-to-end.
- **Trace bundles now capture the full tool-call history.** Request
  dumps, session messages, and the SQLite slice now reflect a real
  agentic run rather than a single API call. Proof-of-loop counts
  (`tool_call_count`, `api_call_count`, `tool_calls_observed`) become
  the real verifiable-work signal.

### Changed

- **`SshHermesRunnerConfig.eval_timeout_secs` default raised from 120s
  to 300s.** The agentic loop has to think, call tools, wait for tool
  results, and think again, so the one-shot 120s budget is too tight.
  The orchestrator's `CATHEDRAL_SSH_EVAL_TIMEOUT` env default also
  moves from 600s → 300s for consistency (was already 600s in the
  env-driven path).
- **Dropped `SshHermesRunnerConfig.eval_max_turns` and the
  `CATHEDRAL_HERMES_MAX_TURNS` env wrapper.** `hermes chat -q` has no
  equivalent CLI flag, and we want the full agentic loop now rather
  than a single-turn cap. The old field was already inert on Hermes
  0.13.0 (the `--max-turns` flag does not exist in 0.13.0; v1.1.5
  stopped passing it).

### For miners

No miner-side change required. The same `~/.hermes/` skills you've
already shipped now get exercised by the agent during the eval round.
If your `soul.md` instructs the agent to cite sources, the agent will
now actually fetch those sources rather than hallucinating around the
gap. Trace bundles get richer; scoring weights are unchanged.

---

## v1.1.4 — Miner-onboard UX: resubmits + public failed-evals

**Date:** 2026-05-12

**Headline:** Two production UX fixes from tonight's first-miner
onboarding pass. Miners can resubmit under their own hotkey after a
failed eval without inventing new display_names, and the public API
now exposes failed eval attempts so the leaderboard's empty-state can
surface real network activity even before any agent scores above zero.

### Fixed

- **Same-hotkey resubmits are no longer blocked by the 7-day fuzzy
  display_name dedupe.** The check was intended to stop OTHER miners
  from squatting an existing display_name; a miner resubmitting under
  their own hotkey (e.g. after a failed eval) should not have to mint
  `McDEE-v2` / `McDEE-v3` / etc to bypass their own prior rows. The
  fuzzy candidate set now excludes the submitter's own hotkey. The
  cross-hotkey squat path is unchanged: a different miner submitting
  the same or fuzzy-close display_name within 7 days is still
  rejected (`status=rejected` with `rejection_reason` set).

### New

- **`GET /api/cathedral/v1/cards/{card_id}/attempts?limit=20`** — public
  endpoint returning recent `eval_runs` for a card INCLUDING failed
  ones (`_ssh_hermes_failed=true` and/or `weighted_score=0`). Same
  per-row shape as `/feed` plus a `miner_hotkey` field for
  attribution. Ordered `ran_at DESC`, default 20 rows, max 200. Lets
  the public leaderboard surface real network activity even before
  any submission scores above zero (cathedralai/cathedral PR #119
  empty-state design).

### For miners

If your eval failed and you want to resubmit under the same display_name,
just do it — same hotkey, same name, different bundle is now the normal
recovery path. No need to suffix v2 / v3 / etc.

---

## v1.0.7 - ssh-probe runtime scaffolding

**Date:** 2026-05-12

**Headline:** Cathedral landed the runtime scaffolding for bundle-based
miners and ssh-probe verification. Current v1 mining uses BYO Box:
Cathedral SSHs into the miner's host, invokes Hermes, captures the trace,
and scores the returned card through the standard Cathedral signing chain.
Polaris deployment code stayed gated and is not documented as a live miner
path.

### New

- **BYO Box - `ssh-probe`**: miner runs Hermes themselves on any host with
  SSH access. They authorize Cathedral by adding the platform SSH key
  (`/.well-known/cathedral-ssh-key.pub`) to `authorized_keys`. Cathedral
  SSHs in, invokes Hermes for the eval, captures the trace, and scores the
  returned card. The live v1 runtime multiplier is 1.00x.
- **Polaris deploy scaffolding**: `polaris-deploy` runner code landed, but
  the mode is gated off in v1 production until isolation and economics are
  ready.
- **Submit endpoint** accepts `attestation_mode` (`polaris-deploy` /
  `ssh-probe` / `tee` / `unverified` / `bundle`) plus optional
  `ssh_host` / `ssh_port` / `ssh_user` / `hermes_port` for BYO Box
  registration.
- **`PolarisDeployRunner`** and **`SshProbeRunner`** added under
  `cathedral.eval.*`. Both produce the canonical `PolarisRunResult` shape;
  scoring + signing path is unchanged.
- **Hermes bundle ingest**: the Polaris-deployed Hermes container fetches
  the encrypted bundle from R2 at startup, decrypts with the env-injected
  KEK + `encryption_key_id`, and extracts into its profile dir. Skills /
  AGENTS.md / soul.md become the running agent's identity.
- **Structured execution trace** in the `/chat` response: tool calls
  (redacted args schema), model calls (model + tokens + latency), agentic
  loop depth, start/end timestamps. Persisted to `eval_runs.trace_json` as
  an unsigned sidecar so old validators continue to verify signatures
  unchanged.
- **CHECK constraint widening** for `agent_submissions.attestation_mode`
  via a 12-step SQLite ALTER procedure (the previous constraint rejected
  `polaris-deploy` and `ssh-probe` on legacy volumes; SQLite cannot ALTER
  a CHECK in place).
- **Site additions**: `/verification` page enumerates exactly what each
  signature attests + what v1 does NOT yet verify; submit form on
  `/jobs/<id>/submit` now has a verification-mode picker that conditionally
  reveals SSH coordinates for BYO Box.
- **`docs/VALIDATOR.md`** + **`docs/validator/RUNBOOK.md`** updated for
  the v2 dispatch flow.
- **Universal Cathedral SSH key** published at
  `https://api.cathedral.computer/.well-known/cathedral-ssh-key.pub` for
  free-tier miner installation.

### Changed

- Default `attestation_mode` was widened for bundle-based miners instead of
  legacy `polaris`. Current v1 production defaults to `ssh-probe`; Polaris
  deploy modes remain gated.
- Publisher dispatcher (`_runner_for`) now routes all five attestation
  modes correctly; previously only `polaris` and `tee` were wired.
- Polaris's `TemplateDeploymentRequest.validate_parameters` widened to
  admit base64 (`+ /`) and URL query (`? & % # ~`) characters in
  `env_vars` values so presigned URLs and wrapped keys can pass through
  to the spawned Hermes container.

### Operational

- `ghcr.io/cathedralai/cathedral-runtime:v1.0.7` published + public.
- Railway env on `cathedral-publisher`:
  - `CATHEDRAL_PIN_CHUTES_KEY=true` (Cathedral forwards its Chutes key
    to spawned Hermes containers for gated Polaris deployment tests).
  - `CATHEDRAL_PROBE_SSH_PUBLIC_KEY` set to the platform-wide pubkey.

### Known limitations

- Live verification chain (`set_weights` on SN292 testnet, weekly Merkle
  anchor on chain) wired in code but not yet running against the live
  publisher signature stream.
- v1 runtime treats `soul.md` as the LLM system prompt rather than
  spinning up the full Hermes agentic loop with tool routing. v2.1 closes
  that gap (tracked in `cathedral-redesign/OBSERVABILITY_V2.md`).
- BYO Box ssh-probe live miner pipeline is in code + tested.

### For miners

Start here: `curl https://api.cathedral.computer/skill.md`.

Current live path: rent or own a box with Hermes running, register SSH
coordinates with your submission, install the platform SSH key in
`authorized_keys`.

### For validators

`docs/validator/RUNBOOK.md` is the canonical setup guide.

```bash
git clone https://github.com/cathedralai/cathedral
cd cathedral
bash scripts/provision_validator.sh
```

Validators verify the Cathedral signature on every `EvalRun` projection
locally against a pinned pubkey. Polaris attestations and miner-hotkey
signatures are verified upstream by the publisher.

### Signed commits

Every commit landing on `main` from 2026-05-12 onward is SSH-signed and
shows as "Verified" on GitHub, so validators auditing the source tree can
trust each revision back to a known maintainer key.
