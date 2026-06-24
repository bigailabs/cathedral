# Cathedral v0 Launch Readiness Checklist

Status: operator handoff for the three-lane v0 work. This is not a launch
announcement. Treat the current state as a reviewable scaffold unless every
gate below is satisfied in the target environment.

## Executive Readiness

| Area | Mergeable now | Not yet claimable |
|---|---|---|
| Lane 1 Audit Arena / SAT audit witness replay | Offline verifier scaffold for SAT assignment checking, witness decode, deterministic replay, and private distillation trace generation. | Broad production audit market, public exploit disclosure flow, live third-party exploitation, or emissions from audit claims. |
| Lane 2 TEE GPU compute intake/provisioning | Default-off publisher intake, gated live entry, signed offers, operator consent/review, deterministic Chutes profile preflight, cryptographic verifier hook, dry-run/execute handoff seam. | Real-machine verifier success, provider acceptance, live health, revenue receipts, or broad miner hardware asks. |
| Lane 3 distillation data path | Private trace shape from verified/rejected audit replay outcomes. | Public dataset, production training pipeline, redaction/disclosure automation, or claims that traces are ready for external release. |
| Validator/Polaris safety | Local/offline smoke paths and fail-closed/default-off knobs. | Any live validator endpoint change, Polaris spend, external provider execution, or chain weight change from these lanes without explicit operator action. |

Launch scorecard: `LAUNCH_SCORECARD.md` and `scaffold/launch_readiness.py`.
The current local scaffold score is 91 / 100. The `controlled-v0` profile is
launchable with the remaining secure-compute proof gates deferred. The `full`
profile still requires real TEE GPU verifier/provider/revenue proof.
If no real TEE GPU proof machine is available, use the explicit
`no-hardware-v0` profile. It is the same controlled launch posture: Secure
Compute may collect signed offers and evidence requests from invited or
allowlisted miners, but provider execution and revenue claims remain
operator-gated.

Operator readiness report:

```bash
python launch_readiness_report.py --show-gates
python launch_readiness_report.py --profile controlled-v0 --require-ready
python launch_readiness_report.py --profile no-hardware-v0 --require-ready
python launch_readiness_report.py --db <publisher.db> --require-ready
```

The DB-backed report proves Cathedral has the required receipts. It does not
independently prove provider truth. Before any broad hardware ask, compare the
`verifier_command_digest` in receipts against the intended real TDX/GPU verifier,
set `CATHEDRAL_TEE_GPU_REAL_VERIFIER_DIGESTS`, and verify provider, health, and
usage receipts out-of-band.

Important scoring distinction: the signed validator weight vector is the live
economics interface. Eval rows, `lane_challenge_solves`, `per_miner_solves`, and
lane-specific tables are inputs or audit trails, not themselves the validator
weight vector. Lane 2 currently writes `tee_gpu_*` state only and must not be
represented as changing validator emissions.

## Lane 1: Audit Arena / SAT Audit Witness Replay

What is safe scaffold:

- Verifies a miner DIMACS assignment against the exact CNF hash.
- Decodes the satisfying assignment into replay inputs.
- Rejects production decode maps that do not bind replay-critical fields.
- Allows static known-answer witnesses only for explicit smoke/corpus replay
  modes.
- Runs a deterministic replay adapter and emits accepted/rejected supervision.
- Builds a private `cathedral.audit_trace.v1` distillation trace with a stable
  trace hash.

Launch gates before production claims:

- [ ] Every production audit package pins repo URL, commit, invariant, CNF hash,
  decode map hash, replay adapter identity, and expected replay semantics.
- [ ] Decode maps use bit projections plus non-empty `required_fields`; static
  witnesses are limited to smoke/corpus fixtures.
- [ ] Replay is deterministic and side-effect-free for shadow runs.
- [ ] Operator review exists before public disclosure, live earning experiments,
  or use against third-party systems.
- [ ] Severity/reachability gates distinguish dust findings from meaningful
  economic impact.
- [ ] Accepted and rejected outcomes are retained with reasons and trace hashes.

Cannot claim yet:

- Do not claim Cathedral has a broad live audit market.
- Do not claim SAT satisfiability alone proves an exploitable bug.
- Do not claim a decoded witness is valid unless replay reproduces against the
  pinned target logic.

## Lane 2: TEE GPU Compute Intake / Provisioning

What is safe scaffold:

- Default-off routes for signed miner capacity offers and admin review.
- Explicit operator-use authorization required.
- Deterministic preflight rejects obvious non-starters.
- Current Chutes TEE profile preflight is restricted to published 8x profiles:
  `h200`, `pro_6000`, `b200`, and `b300`.
- Provider handoff can produce a command or run it only when execution is
  explicitly enabled by an operator.
- The lane is off-chain revenue ops, not validator emissions.

Launch gates before broad miner hardware asks:

- [ ] `CATHEDRAL_TEE_GPU_ENABLED=1` is set only on the intended publisher.
- [ ] A long random `CATHEDRAL_TEE_GPU_ADMIN_TOKEN` is configured.
- [ ] `CATHEDRAL_TEE_GPU_REQUIRE_INTAKE_CODE=1` is configured for live intake.
- [ ] `CATHEDRAL_TEE_GPU_INTAKE_CODE` or
  `CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST` gates public miner entry.
- [ ] `CATHEDRAL_TEE_GPU_REQUIRE_EVIDENCE=1` is used before scale.
- [ ] `CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE=1` is used before asking miners
  to buy hardware.
- [ ] `CATHEDRAL_TEE_GPU_VERIFY_CMD` is installed and fails closed when evidence
  does not prove TDX, NVIDIA GPU CC, request/report binding, and debug-disabled
  state.
- [ ] `CATHEDRAL_TEE_GPU_REAL_VERIFIER_DIGESTS` contains the approved
  `verifier_command_digest`; fixture verifier receipts do not count.
- [ ] The approved verifier command points to an immutable/pinned binary or
  script. The digest identifies the command string, not the verifier file
  contents.
- [ ] A bad-evidence negative control is recorded by the verifier before
  `compute_real_verifier_tested` is marked complete.
- [ ] Evidence request/review records are present for every accepted machine.
- [ ] Manual `operator_reviewed` evidence remains lab-only and does not pass when
  crypto evidence is required.
- [ ] GPU confidential-compute evidence is verified from a provider-accepted
  source; screenshots, `nvidia-smi`, cloud metadata, and benchmarks are not proof.
- [ ] Provider listing acceptance is imported back into Cathedral.
- [ ] Health checks and revenue/usage receipts exist before calling capacity
  useful or payable.
- [ ] A real accepted machine shows:
  - `launch_evidence.provider_listing_verified=true`
  - `launch_evidence.health_verified=true`
  - `launch_evidence.usage_or_revenue_verified=true`
  - `launch_evidence.production_compute_ready=true`
- [ ] Provider-side unlist/retire procedure is documented and tested.

Cannot claim yet:

- Do not claim Secure Compute is live revenue capacity in a no-hardware launch.
- Do not ask miners broadly to buy hardware until at least one real machine has
  reached `cryptographically_verified` and passed evidence -> provider listing
  -> health -> revenue receipt.
- Do not recommend A6000, AMD GPUs, CPU-only TEE, non-TEE GPUs, or single-GPU
  shapes unless the provider publishes that exact TEE measurement profile.
- Do not claim image-as-MRTD; Route A is not built.
- Do not claim self-reported GPU/TDX fields prove confidential compute.
- Do not claim Cathedral capacity rows alone are payment authority.

## Scoring And Tier Weights

Default launch mode:

- [ ] `CATHEDRAL_WEIGHTS_MODE=proportional` is active.
- [ ] `CATHEDRAL_WEIGHTS_TIER_WEIGHTS` is explicit, for example
  `1=1,2=3,3=8`.
- [ ] `CATHEDRAL_REFILL_ENABLED=true` is active on the publisher that should
  mint SAT work.
- [ ] Refill has enough burst supply for the current miner count:
  `CATHEDRAL_REFILL_INTERVAL_SECONDS=20`, `CATHEDRAL_REFILL_MAX_MINTS=4`,
  `CATHEDRAL_PREGEN_QUEUE_SIZE=8`, and
  `CATHEDRAL_OPEN_WINDOW_RETIRE_AFTER_DISTINCT_SOLVERS=256`.
- [ ] `CATHEDRAL_PREGEN_ENABLED` is left unset/on unless the publisher host
  cannot spare background low-priority CNF generation.
- [ ] `CATHEDRAL_REFILL_TARGET_T1=25` and `CATHEDRAL_REFILL_TARGET_T2=25` are
  configured.
- [ ] `CATHEDRAL_PUBLISHER_SEED_SECRET` is set to a stable production secret
  before local or per-miner CNF generation is enabled.
- [ ] Tier 1 generator method is `biased`; tier 2 generator method is `ajm`.
- [ ] `/v1/synthetic-boolean/active-challenges` exposes `scoring` and
  `distribution` matching the intended board mix.
- [ ] `/v1/synthetic-boolean/active-challenges` exposes `generator` metadata
  matching the active refill config.
- [ ] `row_score_recent` is enabled only with an explicit
  `CATHEDRAL_WEIGHTS_ROW_SCORE_TASK_TYPES` allowlist for approved task types.
- [ ] If proportional mode falls back because the solve ledger is empty,
  `policy_metadata.effective_mode=flat_recent_fallback` is visible before launch.
- [ ] Tier weights match the product story:
  - tier 1: participation floor
  - tier 2: harder SAT work
  - tier 3+: higher-value audit/replay work
- [ ] Signed vector `policy_metadata.tier_weights` matches the intended launch
  policy.
- [ ] Compute supply remains off-chain and does not enter emissions.
- [ ] Per-miner unique assignments remain explicitly launch-gated:
  `CATHEDRAL_PERMINER_ENABLED` is off for controlled v0, or shadow-only with
  `CATHEDRAL_PERMINER_SHADOW=1`.
- [ ] Do not claim per-miner assignments are live economic scoring unless the
  operator has intentionally enabled them and `/v1/validator/weights/next`
  reports `policy_metadata.score_source=per_miner`.

## Lane 3: Distillation Data Path

What is safe scaffold:

- Audit replay outcomes can become private training traces.
- Trace records include task context, submission hash, decoded witness, replay
  evidence, accepted/rejected supervision, and trace hash.
- Rejected claims are useful negative examples when reasons are preserved.

Launch gates before production dataset claims:

- [ ] Trace export is private by default.
- [ ] Sensitive target details are redacted unless the target owner opted in or
  the issue is fixed/public.
- [ ] Public export uses a long random redaction salt or server-side HMAC secret
  so miner hotkey hashes are not dictionary-reversible.
- [ ] `distillation_verify.py` passes, proving public export is disclosure-gated
  and raw witness/agent details are stripped.
- [ ] Trace provenance links back to pinned challenge artifacts and replay code.
- [ ] Dataset promotion requires operator review, not automatic acceptance from
  miner text.
- [ ] Training/evaluation consumers treat accepted replay as supervision, not as
  permission for disclosure or live exploitation.

Cannot claim yet:

- Do not claim public distillation corpus readiness.
- Do not claim traces are safe to publish without redaction and disclosure
  review.
- Do not train on unverified miner narratives as positive examples.

## Polaris And Live Validator Safety

- [ ] No Polaris resources are rented during local validation.
- [ ] No live validator endpoint, key, chain endpoint, burn policy, or weight
  vector setting is changed by this v0 handoff.
- [ ] Stub attestation is allowed only in tests or shadow mode.
- [ ] Production markers reject stub attestation:
  `CATHEDRAL_ENV=production` or `CATHEDRAL_PRODUCTION=1`.
- [ ] Real attestation routes fail closed when no verifier command is configured.
- [ ] `CATHEDRAL_ATTEST_STATUS_TOKEN` is configured unless
  `CATHEDRAL_ATTEST_STATUS_PUBLIC=1` is intentionally set.
- [ ] Any Chutes execution remains disabled unless
  `CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED=1` is intentionally set by an
  operator.
- [ ] Dry-run validator checks are separate from live `set_weights`.

## Tests And Smoke Tests Expected Before Merge

Run locally only; do not touch Polaris, Chutes, live validators, or external
services for this merge gate.

```bash
python audit_arena_verify.py
python attest_verify.py
python arena_runner_verify.py
python tee_gpu_verify.py
python weights_verify.py
python launch_readiness_verify.py
python publisher_verify.py
python postgres_verify.py
python rc_verify.py
```

External/deployed smoke only, with explicit operator approval:

```bash
BASE_URL=https://<deployed-publisher> python live_smoke.py
```

Minimum acceptance:

- [ ] Audit Arena smoke passes, including static-witness restrictions and
  production decode-map rejection cases.
- [ ] Attestation endpoints remain default-off/fail-closed without real verifier
  configuration.
- [ ] Arena runner remains inert unless an operator runner/solver is configured.
- [ ] TEE GPU tests prove no writes to validator scoring tables and no blocked
  capacity emits listable commands.
- [ ] Launch readiness scorecard totals 100 points for the full profile, while
  local scaffold does not falsely mark unproven real-world gates complete.
- [ ] `--profile controlled-v0 --require-ready` passes only with the real
  secure-compute proof gates shown as deferred.
- [ ] `publisher_verify.py` passes in an environment with publisher extras,
  including the sr25519 wallet backend, signed vector checks, and miner
  end-to-end submit path.
- [ ] Any Postgres-specific path used by production migrations is covered.
- [ ] Test output is attached to the merge note with exact command names and
  environment flags.

## Risk Register

| Risk | Impact | Mitigation | Owner gate |
|---|---|---|---|
| SAT assignment is valid but not an exploit | False audit claims and bad miner incentives. | Require deterministic replay against pinned target logic before acceptance. | Lane 1 operator review. |
| Static or sparse decode maps decouple witness from assignment | Miners can receive credit for canned inputs. | Allow static witnesses only in smoke/corpus modes; require bit projections and `required_fields` for production. | Audit package review. |
| Dust findings pollute launch story | Brand and operator time cost. | Add severity/reachability gate before disclosure or earnings. | Audit triage. |
| TEE/GPU self-reporting is treated as proof | Paying for fake or unusable hardware. | Require cryptographically verified fresh evidence, provider acceptance, health checks, and revenue receipts. | Lane 2 approval. |
| Provider listing succeeds once but drifts stale | Capacity appears available when it is not. | Add provider inventory import, heartbeat, stale demotion, and manual unlist process. | Ops runbook. |
| Lane 2 accidentally affects emissions | Validator economics change without review. | Keep writes out of `eval_runs`, `lane_challenge_solves`, and `per_miner_solves`; verify signed vector separately. | Publisher tests. |
| Polaris or live validator touched during smoke | Spend, chain, or production incident. | Local-only tests; no external service calls; explicit operator approval for live actions. | Release manager. |
| Sensitive traces leak | Legal, disclosure, and competitive risk. | Private-by-default trace store, redaction, opt-in/fixed/public disclosure checks. | Distillation owner. |

## Next Actions

1. Keep the current PR scoped as scaffold plus operator handoff, not production
   launch.
2. Finish Lane 1 audit package intake and replay adapter identity tracking.
3. Wire Lane 2 `CATHEDRAL_TEE_GPU_VERIFY_CMD` to the real nonce-bound TDX plus
   NVIDIA GPU verifier.
4. Add provider inventory import, health checks, stale demotion, and revenue
   receipt capture before broad hardware asks.
5. Define a private distillation export path with redaction and disclosure
   gates.
6. Prepare a merge note that lists exact local smoke commands, env flags, and
   any skipped tests with reasons.
