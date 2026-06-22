# Cathedral v0 Launch Scorecard

Status: current local scaffold score is **91 / 100**.

Launch profile:

- `full`: **not ready** until real secure-compute evidence reaches 100 / 100.
- `controlled-v0`: **ready** at 91 / 100 because the real secure-compute proof
  gates are explicitly deferred. Secure Compute may run as gated live intake
  under an invite code or hotkey allowlist, but remains operator-gated before
  provider execution or revenue claims.
- `no-hardware-v0`: alias for `controlled-v0`. Use this when Cathedral is
  launching without an available real TEE GPU proof machine.

This is a launch-readiness score, not validator emissions. The scorecard lives
in `scaffold/launch_readiness.py` and is tested by `launch_readiness_verify.py`.

## Weighted Tiers

| Tier | Points | Meaning |
|---|---:|---|
| T0 safety and validator continuity | 20 | Nothing breaks live validators, weights, rollback, or spend controls. |
| T1 incentives and scoring | 20 | Scoring is explicit, proportional, opt-in where risky, and does not leak off-chain lanes into emissions. |
| T2 SAT audit arena | 20 | Audit claims are verified by deterministic replay, not miner text. |
| T3 secure compute | 20 | TEE GPU supply is signed, consented, cryptographically verified, listed, live, and revenue-backed. |
| T4 distillation and operations | 20 | Verified traces become private training data with docs, redaction, and operator runbooks. |

## Current Result

The local scaffold earns 91 points because it now has:

- default-off lane controls
- signed weight-vector continuity
- explicit proportional tier scoring
- live SAT generator/refill wiring with board-visible policy
- row-score opt-in path
- deterministic SAT/audit replay
- signed compute offers and consent
- gated live compute intake with invite-code/allowlist controls
- production crypto evidence gate for TEE GPU intake
- private distillation trace shape
- launch docs, runbook, and stop conditions

The missing 9 points are the gates local tests cannot honestly prove:

- `compute_real_verifier_tested` - real TDX + NVIDIA GPU verifier accepts a real
  machine and rejects bad evidence
- `compute_provider_listing_verified` - at least one verified machine is accepted
  by the provider listing path and recorded with `provider-status`
- `compute_health_and_revenue_verified` - listed compute proves health plus
  usage or revenue through `health-receipt` and `usage-receipt`

The distillation redaction gate is implemented in `scaffold/distillation.py`.
Private export hashes identities and omits raw agent/witness content by default.
Public export refuses private/undisclosed targets unless the operator marks the
issue fixed, public, opt-in, or Cathedral-owned.

## Scoring For Miner Emissions

The launch scoring model is proportional by default:

```bash
CATHEDRAL_WEIGHTS_MODE=proportional
CATHEDRAL_WEIGHTS_TIER_WEIGHTS="1=1,2=3,3=8"
CATHEDRAL_REFILL_ENABLED=true
CATHEDRAL_REFILL_TARGET_T1=25
CATHEDRAL_REFILL_TARGET_T2=25
CATHEDRAL_REFILL_METHOD_T1=biased
CATHEDRAL_REFILL_METHOD_T2=ajm
```

If the solve ledger is empty during cutover, the signed vector reports:

```bash
policy_metadata.effective_mode=flat_recent_fallback
```

Meaning:

- tier 1: participation floor
- tier 2: harder SAT work, 3x
- tier 3: higher-value audit/replay work, 8x

The exact numbers can change, but they must be explicit, tested, and signed into
the policy hash/metadata. Compute supply remains off-chain and must not become a
validator emission lane until a separate economic design is approved.

## 100% Launch Definition

Cathedral is 100% launch-ready only when:

1. `python3 launch_readiness_verify.py` passes.
2. The operator readiness report reaches 100 / 100 from real evidence:
   `python3 launch_readiness_report.py --db <publisher.db> --require-ready`.
3. `CATHEDRAL_TEE_GPU_VERIFY_CMD` is a real verifier, not a fixture.
4. `CATHEDRAL_TEE_GPU_REAL_VERIFIER_DIGESTS` includes the approved
   `verifier_command_digest`; fixture verifier receipts do not count.
5. One real TEE GPU machine completes:
   - bad-evidence verifier rejection receipt
   - signed offer
   - fresh evidence request
   - cryptographic TDX verification
   - cryptographic NVIDIA GPU verification
   - provider listing acceptance
   - health check
   - usage or revenue receipt
   - `launch_evidence.production_compute_ready=true`
6. Distillation export redaction tests pass.
7. The full smoke suite passes.

Human-readable local report:

```bash
python3 launch_readiness_report.py --show-gates
python3 launch_readiness_report.py --profile controlled-v0 --require-ready
python3 launch_readiness_report.py --profile no-hardware-v0 --require-ready
```

Machine-readable production report:

```bash
python3 launch_readiness_report.py --db <publisher.db> --json
```

Important: the DB-backed report proves that Cathedral has recorded the required
receipts. It does not independently prove external provider truth. Before broad
hardware asks, compare `verifier_command_digest` against the intended real
TDX/GPU verifier, set `CATHEDRAL_TEE_GPU_REAL_VERIFIER_DIGESTS`, and verify
provider, health, and usage receipts out-of-band.

## Do Not Claim Yet

- Do not claim production hardware-buy readiness until T3 is 20 / 20.
- Do not claim a public distillation corpus until T4 redaction is closed.
- Do not change tier weights on mainnet without a clear miner-facing explanation.
- Do not include compute rows in validator emissions.
