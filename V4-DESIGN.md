# Cathedral v4 — the always-on, paid SAT Competition

**Date:** 2026-06-09 · **Status:** current design (supersedes the model-gym draft) ·
**Substrate:** this scaffold (~2.8k lines), not the 46k-line monolith ·
**Companion:** `V4-DESIGN.html` (dummy-proof walkthrough + the primary-source evidence)

## Thesis

A standing world-record bounty machine for solver progress. Miners earn only by
(a) publishing an open-source solver that provably beats the reigning champion,
(b) proving a weakness in the champion with a breaker instance, (c) doing
community solve-work on certificate-checked challenges, or (d) supplying
attested eval compute. Every payout path is closed by a certificate (witness or
DRAT) — no judge, no opinion, no self-reported numbers. No progress → burn.

**Emission buys exactly one thing: mathematically verified algorithmic progress
beyond the current world frontier. When there is none, it buys nothing.**

Evidence base (primary papers, read 2026-06-09, full citations in the HTML):
SATLUTION proved the solver-improvement loop now costs <$20k and is bottlenecked
on a trusted certificate-gated evaluation engine — which is what a subnet
natively is. AWS/EDA demand-side papers show the world consumes exactly one
artifact from this field: better open solvers. PUPPER killed planted-generator
hardness as a foundation. NeuroSAT/G4SATBench killed the model-gym transfer bet.
MallobSat killed *general-purpose* trustless fleet solving (clause sharing needs
datacenter links) — but NOT the frontier niche (see Lane F).

## The ecosystem — four lanes reinforcing each other

The scaffold already has three lanes; they map onto the first three below.
The flywheel: Lane S produces champion solvers → Lane A hands them to the
community as designated solvers for challenge batches → Lane I adversaries keep
the benchmark honest and aim both → Lane F (when its two gaps close) turns Lane
A's solve-work into cubes of real open math problems, so the community's cycles
produce headlines instead of answers to our own puzzles.

### Lane S — solver arena (flagship; finish `solver_docker` into this)
- Miners publish open-source solvers: source + container pinned by digest.
  Publishing is the price of getting paid. Source-hash dedup; a copy of the
  champion can never beat the champion.
- Eval: fresh hidden batches of diverse instances, standardized hardware,
  competition conditions. Every result carries its certificate: SAT → witness
  (re-checked against the CNF), UNSAT → DRAT (drat-trim, already proven on
  Stitch). No certificate, no credit. TIMEOUT is the eval host's own
  observation, not a claim.
- Scoring: PAR-2 (production v6 already ships this) + a **marginal-VBS bonus**
  — extra weight for instances only your solver closes. Diversity becomes
  economically rational (the MallobSat/SATzilla lesson as a reward function).
- Champion/challenger: dethrone only past a fixed margin on the full batch.
  Record falls → jackpot + burn steps down one notch (this IS the public
  tier-jackpot commitment, retargeted at verified world-record progress).
  Champion at launch: kissat-sc2025 (or the SC2025 winner binary), so emission
  flows only for beating the actual human frontier.

### Lane A — community solve challenges (keep; today's `sat_challenge`, evolved)
- The community keeps receiving SAT challenges — onboarding stays open to
  anyone with a box. Submissions stay certificate-checked, speed stays
  server-measured (scaffold rules already enforce both).
- **Designated solvers:** challenge batches can name a published Lane S solver
  (typically the champion) for the community to run — turning Lane A into
  distributed evaluation/replication of Lane S results and a zero-expertise
  on-ramp ("docker pull the champion, point it at the board").
- Supply discipline (PUPPER lesson): no reliance on planted-generator hardness.
  Challenge batches draw from rotated diverse families + Lane I instances; when
  Lane F activates, cubes of real open problems become the premium challenge
  stream.

### Lane I — breaker instances (seeded by today's `encoding` lane machinery)
- Submit an instance; it pays only on **disagreement-proven hardness**: the
  champion times out while some other submitted solver closes it with a valid
  certificate. Hardness is proven, never claimed.
- Self-healing benchmark: a family that falls to a cheap trick stops separating
  solvers and its reward dries up automatically — the structural fix for the
  PUPPER failure mode.
- The encoding lane's faithfulness gates (equivalence probes, traps,
  counterexample verification) generalize into the breaker-verification
  toolkit; `consensus.py` (counterexample-beats-majority) is load-bearing here
  and in Lane A.

### Lane F — frontier fleet (research track; Design C revived, eyes open)
Trustless cube-and-conquer on decade-class open problems (Kochen-Specker
minimum, Schur 6, van der Waerden, Ramsey). The published record says: conquer
is embarrassingly parallel and miner-shaped (cubing is ~11–13% of CPU time);
what's missing is exactly two things, and both look closable from our side:
1. **Data transfer** — clause sharing/cube dispatch over WAN. Fred's
   decentralized-training session analyzed transfer techniques (gradient
   compression, async topologies) that may map onto clause/cube traffic.
   Concrete question: what bandwidth × latency envelope does useful cube
   dispatch + selective clause sharing actually need, and do DT-style
   compression/scheduling tricks fit it?
2. **Proof custody** — per-cube UNSAT proofs reach terabytes if stored.
   Candidate concise answer: **check-then-discard with attested checkers** —
   an attested runner (TDX, our `/v1/attest`) verifies each cube's LRAT
   on-the-fly at generation (the 2024 literature shows on-the-fly LRAT checking
   runs at solving speed) and signs the verdict; only the signed verdict +
   cube-partition cover proof persist. Trust moves from "store and reship
   terabytes" to "attest verification at the edge."
Status: NOT in the launch scope. Gaps 1–2 are the Sonnet research brief. When
they close, Lane F's cubes dispatch through Lane A as premium challenges.

## Eval trust — the community is the referee (we run NO eval infrastructure)

Hard constraint: no first-party eval fleet, no hosted inference, no rented
cluster. The community benchmarks the solvers; attestation is an **opt-in
multiplier**, not our hardware. Three trust tiers:

1. **Unattested community work (base rate).** Anyone solves Lane A challenges
   with their assigned designated solver. Correctness is free to verify
   (certificates); **coverage is unfakeable by construction** — faking "solver
   X closed instance i" when X can't requires solving i anyway, so the lie
   costs more than the truth. Cohort racing gives relative solver signal:
   designated solver per (hotkey, challenge) is derived from the block hash
   (miner doesn't pick), each (solver, instance) lands on k random coldkeys,
   receipt-order across cohorts averages out hardware (production v6's own
   trick). This tier produces the continuous qualifier benchmark.
2. **Attested work (higher multiplier).** A miner running inside a TDX runner
   — verified through the working **Polaris `/v1/attest` flow** — binds
   (solver digest, instance hash, wall time) cryptographically. Attested
   submissions earn a multiplier over unattested ones, because they carry two
   signals certificates can't: solver attribution and real timing. This is
   how the gold-standard tier gets supplied without us running anything:
   the multiplier IS the infrastructure budget.
3. **Title matches and audits (attested quorum).** A challenger whose
   qualifier numbers cross the nomination threshold gets a title match run by
   a k-of-n quorum of independent attested runners (median result;
   disagreement widens the quorum). The same attested population earns fees
   for spot-check fraud proofs: re-run a random sample of unattested claims;
   a claim that doesn't reproduce slashes the claimant. Optimistic
   benchmarking with fraud proofs — the rollup pattern applied to solver eval.

This maps exactly onto `grading.py`'s existing cost-minimizing policy
(attest TIMEOUTs only, SAT/UNSAT self-verify free): the only claims that
*need* attestation are timeout claims — which are precisely Lane I's
"champion fails on this instance" hardness proofs and the timeout half of
title matches. The policy was already right; v4 just gives it its real job.
`lanes/solver_docker.py` (image pinning + tamper-evident attested elapsed) is
already the attested-runner primitive.

Validators stay thin: verify certificates + attestation signatures + compute
weights from public data (or consume the Path B signed vector during
transition) — preserved as-is from production.

## Money (the burn, made literal)

| Event | Emission effect |
|---|---|
| No record falls, nothing breaks | Burn stays high (~85% today). Paid for nothing, honestly. |
| Solver record falls (certified) | Jackpot to new champion; burn steps down a notch. |
| Breaker instance proven (Lane I) | Steady decaying payment while the weakness stays open. |
| Community solves (Lane A) | PAR-2 budget share per challenge (v6 mechanics, sybil-proof split). |
| Attested work (opt-in, Polaris `/v1/attest`) | **Multiplier** on the same work — attested submissions earn ×m over unattested; attested quorums earn title-match + fraud-proof audit fees. |
| Fraudulent unattested claim caught by audit | Slashed. |

## Scaffold mapping (what to finish, what carries)

| Scaffold piece | v4 role |
|---|---|
| `lanes/solver_docker.py` | Seed of Lane S: extend from attested-solve to full solver-commit (registry, digest pinning, eval batch runner, champion state machine). **This is the build priority.** |
| `lanes/sat_challenge.py` | Lane A as-is + designated-solver field on challenges. |
| `lanes/encoding.py` / `encoding_real.py` | Lane I verification toolkit (equivalence gates, counterexample checks). |
| `grading.py` | Unchanged: SAT/UNSAT self-verify; TIMEOUT-only attestation policy now applies to attested eval runners. |
| `consensus.py` | Live again: counterexample-beats-majority for Lanes A/I; multi-validator era. |
| `verify.py` + drat-trim | The referee's core. Port production's verifier totality + 9 adversarial fixtures. |
| `chain.py` | Metagraph + set_weights (dry-run default) + block-hash seeding. |
| `pinning.py` | Generalizes to solver-image digest pinning. |

## Non-regression inventory (production `cathedralai/cathedral` @ 8595c1b is a known-working state)

**Preserve as-is:**
- **Burn enforcement**: `MAINNET_FORCED_BURN_PERCENTAGE = 85.0` hardcoded +
  auto-rewrite for finney/39 (`config.py:371-426`); `apply_burn()` routes to
  UID 204; **empty scores → 100% burn** (`chain/weights.py:21-60`). v4's
  record-jackpot steps modify this constant, nothing else.
- **Path B signed-vector pipeline**: Ed25519 signing, key-id pinning, invariant
  checks, replay/rollback guard, idempotent apply (`remote_weight_loop.py`,
  `remote_state.py`, `policy/signing.py`).
- **Shadow-diff machinery**: `shadow_weight_diffs` thresholds (0.10 blocker /
  0.01 investigate), env-gated flip — v4 migrates through this exact mechanism.
- **Preflight rails**: launch preflight + live-chain preflight (registered,
  permit, stake, rate-limit vs interval, immunity > commit-reveal).
- **Sybil property**: k identities → zero marginal merit (PAR-2 first-seen
  dedup, `lanes/par2.py:50-64` + its named test). Lane S adds source-hash dedup
  + one-eval-per-commitment; the property must hold in every lane.
- **DB discipline**: additive idempotent migrations; tuple cursor strict-`>`
  pulls (issue #109); durable backfill marker gating first weight-set.
- **Verifier totality + hostile-input fixtures**: total parsers, bounded
  allocation (`MAX_ASSIGNMENT_VARIABLES`), 9 adversarial + 3 golden fixtures
  port to every lane that checks certificates.

**Preserve through transition, then deprecate:**
- The v5/v6 publisher surface live miners use today (sr25519 canonical-JSON
  submit, tokenized CNF fetch, open-window board, throttles, replay dedup,
  8k/day file-backed CNF). **No hard cutover** — Lane A is its direct
  successor, so this is an evolution, not a kill: same community, upgraded
  challenge stream. Blend via the existing `active_special_weight` knob.

**Salvage:**
- Monolith's orphaned `src/cathedral/v4/` oracle jail (network-isolated,
  rlimit-bounded subprocess) → Lane S solver sandbox.
- v3 receipt-signing/export if Lane F needs signed cube-verdict trails.

## Migration (shadow-first, unchanged shape)

1. **Prototype Lane S** in `scaffold/lanes/`: solver registry + eval batch
   runner + champion state machine, stub solvers first, then kissat/cadical
   real. Lane A designated-solver field. Offline end-to-end via `run_round`.
2. **Shadow on production rails**: compute v4 weights alongside live ones into
   `shadow_weight_diffs`; scaffold validator on Stitch in dry-run as clean-room
   cross-check. ~2 weeks green.
3. **Blend, don't flip**: Lane S enters at small lane weight via the existing
   blend path; ramp as confidence grows. Env-gated, reversible.
4. **Retire the monolith** once v4 lanes carry 100% of non-burn weight for one
   settlement period.

## Open questions (owned by the research track)

1. Lane F gap 1: WAN clause/cube transfer envelope vs decentralized-training
   transfer techniques (Fred's DT session analysis is the starting corpus).
2. Lane F gap 2: check-then-discard proof custody — attested on-the-fly LRAT;
   what exactly must persist for a publishable mathematical result to be
   credible (cube cover proof + signed verdicts vs full DRAT)?
3. Lane S eval variance, reframed for community benchmarking: how many
   qualifier observations (cohort races, coverage points) for a stable
   nomination signal, and what quorum size k makes an attested title match
   decisive?
4. Lane I pricing: decay curve for breaker payments as solvers adapt.
5. Multiplier calibration: how large must the attested multiplier m be to
   draw enough TDX-capable supply for quorums and audits, without making
   unattested participation worthless? (Onboarding depends on the base tier
   staying viable.)
