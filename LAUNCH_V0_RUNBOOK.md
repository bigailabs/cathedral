# Cathedral v0 Launch Runbook

Status: launch-candidate runbook for the current three-lane scaffold. This is
the operator path to a controlled launch, not permission to ask miners broadly
to buy hardware.

## Launch Posture

Launch in this order:

1. Merge and deploy the default-off scaffold.
2. Enable observability and admin-only review surfaces.
3. Launch Secure Compute as gated live intake if no real TEE GPU proof machine
   is available.
4. Run one controlled secure-compute machine through evidence, provider
   listing, health, and revenue receipt when available.
5. Ask miners to buy production hardware only after the cryptographic evidence
   gate works on a real machine.
6. Enable broader miner intake only after the first full loop works.

Do not enable validator-economic changes and provider execution in the same
step.

## Preflight Gate

Run locally before deploy:

```bash
python3 attest_verify.py
python3 tee_gpu_verify.py
python3 weights_verify.py
python3 launch_readiness_verify.py
python3 distillation_verify.py
python3 audit_arena_verify.py
python3 arena_runner_verify.py
python3 rc_verify.py
python3 launch_readiness_report.py --show-gates
python3 launch_readiness_report.py --profile no-hardware-v0 --require-ready
python3 -m py_compile scaffold/publisher/app.py scaffold/publisher/attest.py scaffold/publisher/tee_gpu.py scaffold/publisher/weights.py
```

Run where dependencies exist:

```bash
python3 publisher_verify.py
DATABASE_URL=postgresql://... python3 postgres_verify.py
```

If the local Python lacks publisher dependencies, use an isolated target install
instead of mutating the system interpreter:

```bash
rm -rf /tmp/cathedral-publisher-deps
python3 -m pip install --target /tmp/cathedral-publisher-deps -e ".[publisher]"
PYTHONPATH=/tmp/cathedral-publisher-deps python3 publisher_verify.py
```

`postgres_verify.py` requires a real Postgres `DATABASE_URL`; do not substitute
SQLite for this gate. On Ubuntu/WSL without Docker or sudo, use the ephemeral
Postgres helper:

```bash
scripts/verify_ephemeral_postgres.sh
```

## Safe Default Environment

Start with:

```bash
CATHEDRAL_ENV=production
CATHEDRAL_PRODUCTION=1

CATHEDRAL_ATTEST_ENABLED=
CATHEDRAL_ATTEST_ALLOW_STUB=
CATHEDRAL_ATTEST_STATUS_PUBLIC=
CATHEDRAL_ATTEST_STATUS_TOKEN=<long-random-token>

CATHEDRAL_TEE_GPU_ENABLED=
CATHEDRAL_TEE_GPU_PUBLIC_CATALOG_ENABLED=
CATHEDRAL_TEE_GPU_REQUIRE_INTAKE_CODE=
CATHEDRAL_TEE_GPU_INTAKE_CODE=
CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST=
CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE=
CATHEDRAL_TEE_GPU_VERIFY_CMD=
CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED=

CATHEDRAL_REFILL_ENABLED=true
CATHEDRAL_REFILL_INTERVAL_SECONDS=20
CATHEDRAL_REFILL_MAX_MINTS=4
CATHEDRAL_PREGEN_QUEUE_SIZE=8
# Pregen is enabled by default when the queue is nonzero. Set false only if
# the host cannot spare background low-priority CNF generation.
CATHEDRAL_PREGEN_ENABLED=
CATHEDRAL_REFILL_TARGET_T1=25
CATHEDRAL_REFILL_TARGET_T2=25
CATHEDRAL_REFILL_METHOD_T1=biased
CATHEDRAL_REFILL_METHOD_T2=ajm
CATHEDRAL_PUBLISHER_SEED_SECRET=<long-random-seed-secret>
CATHEDRAL_OPEN_WINDOW_RETIRE_AFTER_SECONDS=3600
CATHEDRAL_OPEN_WINDOW_RETIRE_AFTER_DISTINCT_SOLVERS=256

# Read-service reliability. Keep these on for controlled v0 so board/top/recent
# can serve timer-built snapshots and degrade cleanly under DB pressure.
CATHEDRAL_MATERIALIZED_SNAPSHOT_ENABLED=1
CATHEDRAL_MATERIALIZED_SNAPSHOT_REFRESH_SECS=60
CATHEDRAL_MATERIALIZED_SNAPSHOT_MAX_STALE_SECS=900
CATHEDRAL_RECENT_SNAPSHOT_LIMIT=50
CATHEDRAL_RECENT_NO_CURSOR_MAX_LIMIT=50

# Per-miner unique assignments are beta. Keep off for controlled v0 unless
# running an explicit shadow/economic rollout.
CATHEDRAL_PERMINER_ENABLED=
CATHEDRAL_PERMINER_SHADOW=

CATHEDRAL_WEIGHTS_MODE=proportional
CATHEDRAL_WEIGHTS_TIER_WEIGHTS="1=1,2=3,3=8"
```

`CATHEDRAL_WEIGHTS_MODE=row_score_recent` is the explicit opt-in mode that makes
recent `eval_runs.row_json.weighted_score` affect the signed weight vector. Do
not flip it as part of the compute launch.

## Phase 1: Deploy SAT Refill, Keep New Lanes Dark

Deploy with SAT refill enabled and all non-SAT new lanes disabled.

Checks:

- `/health` works.
- `/v1/validator/weights/next` returns the expected signed vector.
- `/v1/synthetic-boolean/active-challenges` reports
  `generator.enabled=true`, tier 1 `method=biased`, tier 2 `method=ajm`, and
  targets `25/25`.
- `/v1/attest/nonce` returns 404.
- `/v1/tee-gpu/offers` returns 404.
- Chutes execution is disabled.

Run the bundled post-deploy smoke gate:

Pre-merge/pre-deploy operator preflight:

```powershell
powershell -ExecutionPolicy Bypass -File deploy/launch-preflight.ps1 -RailwayExe "$env:USERPROFILE\bin\railway.exe" -Python deploy\python-wsl.cmd
```

This is read-only. It checks PR state, local branch state, Railway CLI auth/link,
and the final smoke plan before any merge/deploy attempt.

Post-deploy final gate:

```powershell
powershell -ExecutionPolicy Bypass -File deploy/post-deploy-smoke.ps1 -RequireFinalGate -Python deploy\python-wsl.cmd
```

This verifies the validator weight-feed release gate, direct read and submit
origins, edge routing, short edge soak, and publisher live smoke. The final
publisher live smoke is intentionally not read-only: it submits one deliberately
wrong signed SAT assignment and expects the deployed referee to reject and
persist the replay guard.

Use `-PlanOnly` before deploy to print the exact commands without hitting the
live service. Use `-SkipLiveSmoke` only for read-only preflight; do not use it
for the final controlled-v0 gate. If this Windows shell only has the Microsoft
Store `python` stub, pass `-Python <path-to-real-python>` or run the equivalent
command from WSL. If you are smoke-testing before `read.cathedral.computer` and
`submit.cathedral.computer` exist, add `-SkipSplitOriginSmoke`; do not use that
skip for the final controlled-v0 gate. Use `-ValidatorReleaseNoChain` only for
feed-only staging checks. Use `-SkipValidatorReleaseGate` only when intentionally
testing non-mainnet staging.

Rollback:

- Revert deployment or unset all lane env flags.
- No database rollback should be needed; migrations are additive.

## Phase 2: Secure Compute Gated Live Intake

Enable gated intake only:

```bash
CATHEDRAL_TEE_GPU_ENABLED=1
CATHEDRAL_TEE_GPU_ADMIN_TOKEN=<long-random-token>
CATHEDRAL_TEE_GPU_REQUIRE_INTAKE_CODE=1
CATHEDRAL_TEE_GPU_INTAKE_CODE=<shared-invite-code>
# Optional hand-picked bypass for known hotkeys:
CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST=<comma-separated-hotkeys>
CATHEDRAL_TEE_GPU_REQUIRE_EVIDENCE=1
CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE=1
CATHEDRAL_TEE_GPU_VERIFY_CMD="<tdx-and-nvidia-gpu-verifier-command>"
CATHEDRAL_TEE_GPU_REAL_VERIFIER_DIGESTS="<sha256-of-approved-verifier-command>"
CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED=
```

Allowed:

- signed miner offers from invite-code holders or allowlisted hotkeys
- evidence requests
- operator attestation review as lab-only context
- admin-only cryptographic evidence verification
- Chutes manifest dry-run

Use the shared invite code only as a coarse launch gate. Use
`CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST` when access must be bound to specific miner
hotkeys.

Not allowed:

- accepting `operator_reviewed` as production proof
- public catalog
- provider execution
- emissions from compute rows
- open public compute intake without an invite code or allowlist

First-machine gate:

1. Miner submits a consented offer.
2. Cathedral issues `POST /v1/tee-gpu/evidence-request`.
3. Evidence is stored.
4. Operator submits one known-bad evidence sample and records verifier
   rejection as a negative control.
5. `POST /v1/admin/tee-gpu/capacity/{capacity_id}/verify-evidence` verifies:
   TDX quote, NVIDIA GPU confidential-compute evidence, request/report binding,
   and debug-disabled state.
6. Evidence status is `cryptographically_verified`.
7. Preflight is eligible.
8. Chutes handoff dry-run command is reviewed.
9. Operator enables execution for one handoff only.
10. Provider status is imported with `provider-status`.
11. Health is captured with `health-receipt`.
12. Usage or revenue is captured with `usage-receipt`.
13. `launch_evidence.production_compute_ready=true`.
14. `python3 launch_readiness_report.py --db <publisher.db> --require-ready`
    reaches 100 / 100.

The readiness command proves required receipts exist in Cathedral. It does not
independently prove provider truth. Compare `verifier_command_digest` against
the intended real verifier, set `CATHEDRAL_TEE_GPU_REAL_VERIFIER_DIGESTS`, and
verify provider, health, and usage receipts out-of-band before asking miners to
buy hardware.

Rollback:

```bash
CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED=
CATHEDRAL_TEE_GPU_PUBLIC_CATALOG_ENABLED=
```

Then pause or retire affected capacity rows from the admin surface.

## Phase 3: Solver Attestation

Enable only after a real verifier command is installed:

```bash
CATHEDRAL_ATTEST_ENABLED=1
CATHEDRAL_ATTEST_DCAP_VERIFY_CMD="<verifier-command>"
CATHEDRAL_ATTEST_STATUS_TOKEN=<long-random-token>
CATHEDRAL_ATTEST_ALLOW_STUB=
```

Rules:

- Stub verification is ignored in production-marked environments.
- `/v1/attest` requires signed hotkey headers.
- `/v1/attest/status/{eval_run_id}` is private unless explicitly made public.
- A successful attestation upgrades and re-signs an eval row.
- It affects emissions only if the configured weight mode consumes row scores.

Rollback:

```bash
CATHEDRAL_ATTEST_ENABLED=
CATHEDRAL_ATTEST_DCAP_VERIFY_CMD=
```

Existing base solve rows remain valid. Existing attestation audit rows remain
as audit history.

## Phase 4: Audit Arena And Distillation

Keep live earning policy in shadow replay until an operator approves each audit
package.

Acceptance:

- target repo and commit pinned
- CNF hash pinned
- decode map has bit projections and `required_fields`
- deterministic replay adapter identity recorded
- accepted/rejected trace hash stored
- public disclosure reviewed separately

Do not use third-party live exploitation as the default verifier. Use local
replay, Cathedral-owned canaries, opt-in targets, or normal rule-compliant
competition.

Distillation export:

- use private export by default
- run `python3 distillation_verify.py`
- do not produce public traces unless disclosure status is fixed, public,
  opt-in, or Cathedral-owned

## Stop Conditions

Stop rollout immediately if:

- signed weight vector changes unexpectedly
- `/v1/validator/weights/next` fails or stalls
- compute rows write to validator scoring tables
- Chutes execution runs without explicit operator env
- attestation verifier falls back to stub in production
- provider-listed capacity has no health or revenue path
- miners can spoof `operator_reviewed` evidence
- `operator_reviewed` passes while `CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE=1`
- `verify-evidence` succeeds without a real verifier command

## Definition Of Launch Ready

The code is launch-ready for a controlled v0 when:

- all local gates pass
- `launch_readiness_report.py --profile no-hardware-v0 --require-ready` passes
- the launch scorecard is reviewed and every missing real-world gate has an
  owner before broad launch
- production env is dark by default
- rollback is tested by disabling env flags
- miner-facing docs say Secure Compute is gated live intake only until the full
  hardware loop works

The code is launch-ready for a full production hardware ask only when:

- one real secure-compute machine completes evidence -> listing -> health ->
  revenue receipt
- `launch_readiness_report.py --db <publisher.db> --require-ready` passes
- real TDX DCAP plus NVIDIA GPU evidence verifier command is installed and its
  `verifier_command_digest` is allowlisted
- miner-facing docs say exactly what hardware is accepted and explicitly reject
  CPU-only TEE, AMD GPUs, A6000, and non-TEE GPU machines
