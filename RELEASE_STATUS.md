# Cathedral v4 — release status

**One line:** v4 is **live** — the thin orchestrator serves
`api.cathedral.computer`, scoring is one signed number per miner, and the v4
validator that applies it is released for operators to adopt.

## Live in production

- **Orchestrator:** `api.cathedral.computer` runs the v4 thin publisher on
  Postgres + a broadcast read tier (the legacy single-SQLite wedge and disk
  cliff are gone). Same Ed25519 signing key as the prior backend (`10890a66…`),
  so validators and miners saw no identity change at cutover.
- **Scoring:** composed entirely orchestrator-side into one signed weight per
  miner, served at `/v1/validator/weights/next`. A trailing recency window
  replaces the legacy 7-day validator-side mean — idle miners decay out instead
  of coasting on a frozen tail. Burn rides inside the signed payload.
- **Validator:** `scaffold/validator_thin.py` — fetch → verify → apply. No local
  averaging, no row database. Released on the `v4` branch; see
  [VALIDATOR.md](VALIDATOR.md). Adoption is per-operator and incremental.

## Verified (all gates green)

| Gate | Result | Proves |
|---|---|---|
| `publisher_verify.py` | 74/74 | end-to-end miner loop + the signed-vector interface: vector signs/verifies, tamper rejected, recency gate decays idle miners, rollback fence holds, **and the deployed validator's own `verify_vector` + `invariant_check` accept our vector** (drop-in proven) |
| `wire_compat.py` | 8/8 | canonical-JSON + Ed25519 reproduces **live production rows byte-for-byte** (sampled live rows verify under the prod pubkey `10890a66…`), incl. tamper checks |
| `rc_verify.py` | 36/36 | scoring invariants across all lanes; liar rejected; self-reported `wall_ms` ignored; attested ≥ floor |
| Arena (on Stitch, real solvers) | 38/38 | kissat dethroned the champion through the full certified loop; every cheat scored 0 with reasons |

## On-chain reality (measured)

At launch, **74.5% of SN39 validator stake applies the signed vector** (verified
by comparing each permitted validator's on-chain weights to the signed vector;
the largest validator, ~33% of stake, is on it). The remaining ~25.5% statically
burn 100% and are blind to any score — a per-operator policy choice, not a code
gap. Scoring changes reach miners through the relaying majority with no validator
release.

## What's next (not blockers — deferred by design)

1. **Burn step-down** — burn rate is a signed-payload field; dialing it below the
   current 85% is an orchestrator-side change, no validator release.
2. **Proportional pay** — the orchestrator can compose weight as work-share
   (distinct solves vs the busiest solver) instead of flat-per-recent-solver;
   one env flip, no validator release.
3. **Difficulty ladder** — calibrated solve-time tiers with a solver-robust,
   monotonic hard-instance family (reduced-round preimage or cube-of-real-instance,
   not threshold random-3SAT — falsified by the P0 spike).
4. **Lane S/I as on-chain value** — the arena (beat-the-champion, attested speed)
   and hard-instance discovery, wired into the signed score.

## Open questions (see `V4-DESIGN.md` → Open questions)

- Multiplier calibration `m` (how large to draw attested supply without making
  unattested participation worthless).
- Quorum size `k` for attested title matches (depends on the variance run).
- A dedicated weight-policy signing key (the vector currently reuses the eval
  key — works and matches production, but a clean separation is a coordinated
  validator re-pin, deferred).
