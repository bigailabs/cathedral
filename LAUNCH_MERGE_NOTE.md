# Cathedral Controlled v0 Merge Note

Status: ready for controlled/no-hardware v0. Not ready for a broad production
hardware ask until real secure-compute evidence reaches the full gate.

## What This Merge Proves

- SAT scoring defaults to proportional composition over distinct verified solves.
- Per-miner beta scoring is coldkey-aware when coldkey collapse is enabled, so
  stacked hotkeys under one coldkey share score instead of multiplying it.
- Submit endpoints now require solution-bound signatures; blank active-CNF
  signatures cannot be reused to rank work.
- Pre-auth rate limiting no longer trusts unverified hotkey headers.
- The active board can expose scoring policy and tier distribution metadata.
- After deploy and live smoke, miners can query a public explanation for their
  current scoring inputs.
- Secure Compute intake remains default-off, consented, invite/allowlist-gated,
  and operator-gated.
- Google TPU intake is exploratory only: not Chutes-listable and not emissions-eligible.
- Audit Arena accepts SAT-backed witnesses only after deterministic replay.
- Distillation exports are private by default and public export is disclosure-gated.
- Publisher, Postgres, validator CLI, attestation, arena, and launch-readiness
  paths pass local verifier gates.

## Commands Run

Core WSL/local suite:

```bash
uv run --with-requirements deploy/requirements.txt python attest_verify.py
uv run --with-requirements deploy/requirements.txt python distillation_verify.py
uv run --with-requirements deploy/requirements.txt python audit_arena_verify.py
uv run --with-requirements deploy/requirements.txt python arena_runner_verify.py
uv run --with-requirements deploy/requirements.txt python rc_verify.py
uv run --with-requirements deploy/requirements.txt python weights_verify.py
uv run --with-requirements deploy/requirements.txt python tee_gpu_verify.py
uv run --with-requirements deploy/requirements.txt python launch_readiness_verify.py
```

Result: pass.

Publisher E2E with publisher extras:

```bash
uv run --with-requirements deploy/requirements.txt python publisher_verify.py
```

Result:

```text
PUBLISHER VERIFY: PASS all 98 checks
```

Validator release safety tests now run in CI:

```bash
python -m pytest -q \
  scaffold/publisher/tests/test_validator_release_gate.py \
  scaffold/publisher/tests/test_validator_health_endpoint.py
```

These cover the read-only validator release gate and the admin
`/v1/admin/validator-health` surface used to protect the weight feed.

Postgres E2E used an ephemeral PostgreSQL 16 server unpacked under `/tmp` from
Ubuntu packages and a local `DATABASE_URL`:

```bash
scripts/verify_ephemeral_postgres.sh
```

Result:

```text
POSTGRES VERIFY: PASS all 33 checks
```

Postgres now applies 23 migrations and verifies 14 core tables, including
`coldkey_map`.

Controlled launch readiness:

```bash
python3 launch_readiness_report.py --profile no-hardware-v0 --require-ready
```

Result:

```text
Cathedral launch readiness: READY
Profile: controlled-v0
Score: 91.0/100.0 (91.0%)
Blockers: none
Deferred gates:
  - compute_real_verifier_tested
  - compute_provider_listing_verified
  - compute_health_and_revenue_verified
```

Hygiene:

```bash
git diff --check
python -m compileall scaffold publisher_verify.py weights_verify.py tee_gpu_verify.py launch_readiness_verify.py scripts/cathedral_live_table.py
```

Result: pass.

Publisher-only live smoke:

```bash
BASE_URL=https://api.cathedral.computer uv run --with-requirements deploy/requirements.txt python live_smoke.py
```

This is only the publisher referee check. It is useful when debugging submit
behavior, but it is not the final controlled-v0 launch gate because it does not
check the validator weight feed, direct split origins, edge route map, or soak.

Preferred controlled-v0 post-deploy gate:

```powershell
powershell -ExecutionPolicy Bypass -File deploy/post-deploy-smoke.ps1
```

This gate runs the read-only validator release gate first, then checks direct
read/submit origin health and role isolation before the edge route map, short
soak, and publisher live smoke. The publisher live smoke intentionally submits
one deliberately wrong signed SAT assignment and expects the deployed referee to
reject and persist it; use `-SkipLiveSmoke` only for read-only preflight, not
for the final controlled v0 gate. Use `-SkipValidatorReleaseGate` only for
non-mainnet staging. Use `-SkipSplitOriginSmoke` only for pre-split staging, not
for the final controlled v0 gate.

Result against current production: pre-deploy gaps are expected. Older prod may
not expose board `generator` / `scoring` / `distribution` or
`/v1/leaderboard/explain`, and submit may still return `429 submit_busy_retry`
under load. The validator release gate now retries bounded signature
convergence across `api.cathedral.computer`, the legacy-prefixed alias, and
`read.cathedral.computer`; persistent divergence after those retries remains a
payment-feed launch blocker. Rerun this after Railway deploy before claiming the
deployed miner experience or payment feed is live.

## Claim Live Checklist

Do not post that the new miner path or payment feed is live until all of this is
true against production:

- PR merged and Railway/edge deployment completed.
- `deploy/post-deploy-smoke.ps1` passes without `-SkipLiveSmoke`.
- `-SkipValidatorReleaseGate`, `-SkipSplitOriginSmoke`, `-SkipEdgeSmoke`,
  `-SkipRouteMap`, and `-SkipSoak` were not used for the final gate.
- `/v1/validator/weights/next` converges across canonical, legacy-prefixed, and
  direct read-service URLs with no stale fallback in steady state.
- `/v1/leaderboard/explain` is available and uses the deployed scoring path.
- Publisher live smoke rejected and persisted the deliberately wrong signed SAT
  assignment.

## Not Claimable Yet

Full production hardware launch is still blocked by real-world evidence:

- `compute_real_verifier_tested`
- `compute_provider_listing_verified`
- `compute_health_and_revenue_verified`

Before asking miners broadly to buy or rent hardware, one real machine must
complete:

1. signed offer
2. fresh evidence request
3. real TDX plus NVIDIA GPU confidential-compute verification
4. provider listing acceptance
5. health receipt
6. usage or revenue receipt
7. DB-backed readiness report at 100 / 100
