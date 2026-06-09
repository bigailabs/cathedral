# Primer — Opus build session: Cathedral v4 on the scaffold

You are the build lead for Cathedral v4 (Bittensor SN39). The design phase is
DONE — do not relitigate it. Your job is to make the scaffold real.

## Read first (in this order)
1. `V4-DESIGN.md` — the locked design: four lanes (S/A/I/F), scoring, burn,
   non-regression inventory, migration plan.
2. `scaffold/contract.py`, `grading.py`, `consensus.py`, `validator.py`,
   `verify.py`, `pinning.py`, `chain.py` — the core you're extending.
3. `scaffold/lanes/solver_docker.py` — the seed of Lane S (your main work).
4. `scaffold/lanes/sat_challenge.py` (Lane A) and `lanes/encoding*.py` (Lane I
   toolkit).

## Locked decisions (do not reopen)
- v4 = record-bounty arena. Lane S (solver-commit) is the flagship; Lanes A
  (community challenges, with designated solvers) and I (breaker instances)
  stay; Lane F (trustless C&C) is research-only, NOT in your scope.
- **We run NO eval infrastructure.** The community benchmarks solvers. The
  eval batch runner you build is the harness that *attested community nodes*
  run (TDX via Polaris `/v1/attest`), not something we host. Attestation is an
  opt-in **multiplier**: attested submissions earn ×m over unattested; title
  matches are adjudicated by a k-of-n quorum of attested runners (median).
  Unattested community work still earns at base rate — coverage claims are
  unfakeable (faking "X closed i" requires solving i), relative speed comes
  from cohort racing by receipt order.
- Lane A designated solver is **assigned, not chosen**: derived from
  (block hash, hotkey, challenge); submissions carry a `claimed_solver_digest`
  field. Unattested claims are subject to attested spot-check audits
  (mismatch → slash).
- Solvers are submitted as open source + container pinned by digest. Every
  scored result carries a certificate (witness re-check or DRAT via drat-trim).
  TIMEOUT is the eval host's observation, never a miner claim.
- Scoring: PAR-2 + marginal-VBS bonus; champion/challenger with a fixed
  dethrone margin. Champion at launch = SC2025 winner binary.
- Lane contract rules are inviolable: pure/deterministic mint, total verify,
  bounded score, hidden stays hidden, server-measured timing.
- Weights ship through the production Path B signed-vector pipeline; the
  scaffold validator stays dry-run until Fred flips it.

## Build order (milestones — finish one before starting the next)
1. **Lane S core** (`scaffold/lanes/solver_arena.py` + extend `solver_docker`):
   - Solver registry: (source_url, container_digest, source_sha256), dedup on
     source hash, one-eval-per-commitment.
   - Eval batch runner: given a solver digest + N seeded instances, run under
     resource limits (salvage the monolith's `src/cathedral/v4/oracle/jail.py`
     containment pattern — network-isolated, rlimit-bounded), collect
     per-instance (outcome, wall_ms, certificate), verify every certificate.
   - Champion state machine: current champion, challenger evaluation, strict
     dominance margin, record-fall event (this triggers the burn-step/jackpot).
   - PAR-2 + marginal-VBS scoring on top of `grading.py` (do NOT fork the
     grading module; extend via lane score()).
2. **Lane A designated solvers**: assignment, not choice — the designated
   solver digest for a (hotkey, challenge) derives deterministically from
   (block hash, hotkey, challenge) inside the lane contract's purity rules;
   submissions carry `claimed_solver_digest`; certificate-checking unchanged.
   Keep the cohort-race bookkeeping (which solver cohort closed which
   instances, receipt order) in the round report so qualifier scoring can
   consume it.
3. **Offline end-to-end**: `run_round` exercising Lane S with two stub solvers
   + real kissat/cadical containers; rc_verify-style checks (liar solver,
   forged DRAT, copied champion, timeout fraud) — every adversarial case must
   score 0 with a reason.
4. **Shadow wiring**: compute v4 weights alongside production into a
   `shadow_weight_diffs`-shaped table (thresholds 0.10 blocker / 0.01
   investigate); scaffold validator on Stitch in dry-run.

## Non-regression (the monolith is a known-working state — don't lose these)
See the inventory section of `V4-DESIGN.md`. The ones you will touch:
- Verifier totality + bounded allocation; port the 9 adversarial + 3 golden
  fixtures from `~/code/cathedral/src/cathedral/lanes/synthetic_boolean_v1/fixtures/`.
- Sybil property: k identities → zero marginal merit, in every lane.
- Burn-on-empty: no positive scores → 100% to burn UID.

## Standing constraints (Fred's)
- Commit as Fred only — no agent attribution, no Co-Authored-By.
- Concise replies, lead with the bottom line.
- Everything lives on GitHub (`wallscaler/cathedral-scaffold`) + Stitch.
- Dry-run by default for anything chain-facing; broadcast is Fred's call.

## Definition of done for this session
Milestones 1–3 merged on master with rc_verify extended to cover Lane S
adversarial cases, all passing. Milestone 4 if time allows.
