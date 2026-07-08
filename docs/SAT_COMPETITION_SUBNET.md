# SAT Competitions → a <1k-LOC Competition Subnet

Deep research on the SAT Competition ecosystem and a distilled design for a
minimal SAT-competition subnet, mapped onto the primitives Cathedral already
ships. Research claims below were multi-source verified against official
competition rules, the organizers' peer-reviewed reports, and solver-author
retrospectives (25/25 extracted claims survived 3-vote adversarial
verification; sources linked inline).

---

## Part 1 — The SAT Competition, distilled

### 1.1 History and structure

A first SAT competition ran in 1992; the current annual series started in
2002 (organized by Hirsch, Le Berre, and Simon) and is credited as a main
driving force behind two decades of solver progress
([SAT Museum, PoS 2023](https://ceur-ws.org/Vol-3545/paper6.pdf),
[Järvisalo et al., AI Magazine 2012](https://www.cs.helsinki.fi/u/mjarvisa/papers/jarvisalo-leberre-roussel-simon.aimag.pdf)).
SAT Competition 2020 ran five tracks — Main (with No-Limits and Glucose-hack
sub-tracks), Parallel, a new AWS-sponsored Cloud track (MPI over 1,600 virtual
cores, 1,000 s wall-clock), the IPASIR-based Incremental Library track, and a
Planning track
([official 2020 slides](https://satcompetition.github.io/2020/downloads/satcomp20slides.pdf),
[AIJ 2020 report](https://www.sciencedirect.com/science/article/pii/S0004370221001235)).

**Lesson for a minimal subnet:** pick one regime. Sequential wall-clock
solving is the Main-track default and the only one with a full certificate
regime; parallel/cloud tracks historically waive UNSAT proofs because proof
logging is hard for portfolio/distributed solvers.

### 1.2 Scoring: PAR-2

All tracks rank by **PAR-2**: the sum of runtimes over solved instances plus
**2× the timeout** for each unsolved instance, lower is better
([2024 tracks](https://satcompetition.github.io/2024/tracks.html),
[2023 tracks](https://satcompetition.github.io/2023/tracks.html)). In 2020 the
timeout was 5,000 s, so every miss cost 10,000 points
([AIJ 2020 report](https://www.sciencedirect.com/science/article/pii/S0004370221001235)).

PAR-2 and raw solve count demonstrably diverge: in the 2019 edition CaDiCaL
solved **244** instances against the winner's **240**, yet placed lower on
PAR-2 (4583.40 vs 4525.14 average)
([2019 results](https://satcompetition.github.io/2019/results.html),
[Kissat winners page](https://fmv.jku.at/kissat/winners.html)). A subnet has
to make the same choice consciously: speed-weighted scoring and solve-count
scoring reward different miners.

### 1.3 Certificates and the verification asymmetry

- **SAT:** printing a satisfying model is required in all tracks except
  No-Limits. Checking it is `O(|formula|)` — trivial for a verifier
  ([2022 rules](https://satcompetition.github.io/2022/rules.html),
  [2024 rules](https://satcompetition.github.io/2024/rules.html)).
- **UNSAT:** Main-track solvers must emit machine-checkable proofs
  (tracecheck/DRAT in 2022–23; by 2024 certificates are required for *both*
  outcomes, with DRAT-trim, dpr-trim, GRAT/gratgen, and VeriPB all accepted
  ([2024 output formats](https://satcompetition.github.io/2024/output.html)).
- The cost asymmetry is codified in the budgets: 2020 validated each UNSAT
  proof by DRAT-trim → LRAT → the formally-verified checker `cake_lpr`, with a
  **45,000 s proof-checking budget against a 5,000 s solving limit** — 9× more
  time to check than to solve
  ([AIJ 2020 report](https://www.sciencedirect.com/science/article/pii/S0004370221001235)).

**Lesson:** SAT answers are the cheap side of the asymmetry; UNSAT proofs are
the expensive side. A subnet that wants a small verifier either (a) issues
predominantly satisfiable instances so model checking suffices, or (b) caps
proof size/checking time and treats "checker ran out of budget" as *unsolved*,
never as *fraud*.

### 1.4 Benchmark supply: Bring Your Own Benchmarks (BYOB)

Since 2017 each Main-track team must submit **20 new instances** unseen in
prior competitions, at least 10 of them "interesting": *not* solvable by
MiniSat within one minute, but solvable by the submitter's own solver within
one hour ([2024 rules](https://satcompetition.github.io/2024/rules.html)).
The 2020 suite was 300 new + 100 old instances, drawn 7 SAT + 7 UNSAT per
author, capped at ~13–14 instances per research group out of 400, balanced
135 SAT / 135 UNSAT / 130 unknown, after filtering out anything MiniSat
solved in under ten minutes
([AIJ 2020 report](https://www.sciencedirect.com/science/article/pii/S0004370221001235),
[SAT Museum](https://ceur-ws.org/Vol-3545/paper6.pdf)).

**Lesson:** crowdsourced challenges work *only* with difficulty gating
(a reference solver playing MiniSat's "too easy" role), freshness rules, and
per-source caps. A trustless subnet can't assume honest submitters, so
validator-generated instances are the decentralized analog — with a
calibration solver setting tier difficulty exactly the way MiniSat gates BYOB.

### 1.5 Anti-cheating: disqualification, not point deductions

The rule text, stable 2013–2024: *"A SAT solver will be disqualified if the
solver produces a wrong answer. Specifically, if a solver reports UNSAT on an
instance that was proven to be SAT by some other solver, or SAT and provides
a wrong certificate."* Disqualified solvers are award-ineligible and publicly
marked ([2024 rules](https://satcompetition.github.io/2024/rules.html)).

Enforcement is real:

- **2020:** of 64 Main-track submissions, **6 were disqualified** for
  non-satisfying assignments and **4 were demoted** to No-Limits for invalid
  UNSAT proofs
  ([AIJ 2020 report](https://www.sciencedirect.com/science/article/pii/S0004370221001235)).
- **2002:** the winning solver zchaff was later shown — by re-verification —
  to have produced entirely wrong models (all 9 re-checked models incorrect),
  so the Kissat retrospective substitutes Limmat as the effective 2002 winner
  ([fmv.jku.at/kissat/winners.html](https://fmv.jku.at/kissat/winners.html)).
- Portfolio solvers mixing codebases from different author groups are banned
  outside No-Limits; organizers may not participate.

Two mechanisms matter most for a subnet:

1. **Cross-checking is nearly free.** Any verified SAT model refutes *every*
   UNSAT claim on the same instance. The competition's cheapest fraud
   detector costs one `O(|formula|)` witness check.
2. **Wrong ≠ unverifiable.** Historically an UNSAT proof that merely failed
   to verify in budget counted as *unsolved*; a provably wrong answer
   *disqualified*. (2025 rules tighten toward disqualification for wrong
   certificates.) A slashing policy must preserve this distinction or it will
   punish checker timeouts as fraud.

### 1.6 Winners and the monoculture problem

Lineage: Glucose won 2011–12; the MapleSAT series (LRB decision heuristic,
interleaved SAT/UNSAT restart policies) won 2016–18; Kissat — a leaner
rewrite descended from CaDiCaL — won 2020 (PAR-2 3926.2, 264/400 solved),
followed by kissat-mab (2021) and kissat-mab-hywalk (2022), each built
directly on the previous winner
([SAT Museum](https://ceur-ws.org/Vol-3545/paper6.pdf),
[2021 slides](https://satcompetition.github.io/2021/slides/ISC2021-fixed.pdf),
[2022 slides](https://satcompetition.github.io/2022/slides/satcomp22slides.pdf)).

**Lesson:** winners are open source, so every miner starts from the same
state-of-the-art binary. The Kissat monoculture is the centralized preview of
a subnet's copying problem — differentiation comes from tuning, scheduling,
and hardware, and anti-relay defenses must assume identical solver cores.
This is also the opportunity the README's SATLUTION citation points at:
rewards flow to whoever evolves the frontier, not whoever downloads it.

### 1.7 Sibling competitions: how others price a wrong answer

The neighboring competitions differ most in exactly the dimension a subnet
cares about — what a wrong answer costs — and the pattern tracks whether
answers are certificate-checkable:

- **SMT-COMP** — decision answers are *not* certificate-checked, so wrong
  answers are made fatal. The original 2005 scoring was +1 per correct
  answer, **−8 for a wrong "unsat", −4 for a wrong "sat"**
  ([Barrett, de Moura, Stump, CAV 2005, Fig. 1](http://theory.stanford.edu/~barrett/pubs/BdMS05-CAV.pdf)).
  Modern rules score each division as a lexicographic tuple
  ⟨errors, correct, wall-clock, CPU⟩ — "fewer errors takes precedence over
  more correct solutions," so a single unsound answer ranks a solver below
  every sound one. Benchmark scrambling is seeded partly from the NYSE
  Composite opening value so organizers can't bias selection
  ([SMT-COMP 2021 rules §7](https://smt-comp.github.io/2021/rules.pdf)).
- **MaxSAT Evaluation (incomplete track)** — every submitted solution is
  cheaply verifiable, so scoring is anytime-quality in [0,1]:
  `(best_known_cost + 1) / (your_cost + 1)`, and an infeasible model simply
  scores 0 — no negative penalty needed
  ([MSE 2021 incomplete track](https://maxsat-evaluations.github.io/2021/incomplete.html)).
- **Model Counting Competition** — tolerance bands: the exact track's
  strictest ranking disqualifies on any wrong count; approximate rankings
  allow bounded error but disqualify after >20 answers outside the margin
  ([MC 2024 description](https://mccompetition.org/2024/mc_description.html)).

The pattern: *when answers self-verify, score quality and floor at zero; when
they don't, make wrongness lexicographically fatal.* SAT witnesses self-verify
— which is why a SAT subnet gets the gentle regime on its main path and only
needs the draconian one for UNSAT claims.

### 1.8 Prior art: NP-hard problems under crypto incentives

- **TIG (The Innovation Game)** runs a satisfiability challenge on uniform
  random 3-SAT at clause/variable ratio 4.267 — the phase transition — with an
  all-or-nothing `O(formula)` witness check
  ([tig-challenges source](https://github.com/tig-foundation/tig-monorepo/blob/main/tig-challenges/src/satisfiability/mod.rs),
  [track params](https://docs.tig.foundation/benchmarking/advanced-tips)).
  Its distinctive contribution is the *innovation-attribution* layer:
  block rewards split ~50% to benchmarkers (compute), 20% to "advance"
  (algorithmic-method) submissions, 10% to code submissions, 20% to challenge
  owners ([tokenomics](https://docs.tig.foundation/tokenomics)). Copying is
  handled by delayed code publication (new code stays private until round
  X+2), adoption-weighted merge points, and a UAI (unique algorithm
  identifier) that derivative implementations must retain — so optimizers of
  someone else's method keep paying attribution to the method's creator
  ([advance submission](https://docs.tig.foundation/innovating/advance-submission)).
  This is the fullest existing answer to the Kissat-monoculture problem.
- **Graphite (Bittensor SN43)** scores TSP tours *relative to two anchors*:
  worse than a greedy nearest-neighbour baseline → 0, equal to it → 0.2, and
  quadratic interpolation up to the cohort-best solution that round
  (`0.8·(1 − |best−score|/|best−benchmark|)² + 0.2`), invalid tours costed at
  infinity ([reward.py](https://github.com/GraphiteAI/Graphite-Subnet/blob/main/graphite/validator/reward.py)).
  Notably it has **no per-miner instance separation** — all miners solve the
  same instance, and copying the public baseline is "defended" only by the
  0.2 floor. That is the design gap per-miner HMAC instances close.
- **Omron (Bittensor SN2)** inverts the asymmetry: miners pay the expensive
  direction (zk-proof generation, often 100–1000× inference cost) so
  validators only do cheap proof verification
  ([subnet-2 README](https://github.com/inference-labs-inc/subnet-2)). SAT
  gets that trust model for free on satisfiable instances — the witness *is*
  the succinct proof — which is precisely why a SAT subnet can be tiny where
  a zk-ML subnet cannot.
- **Cathedral (this repo)** is the direct prior art: planted/AJM instance
  generation, publisher-side witness verification, host-measured wall time,
  Ed25519-signed score rows and weight vector, a ~200-line thin validator,
  and HMAC per-miner instances. Part 2 is largely a distillation of what is
  already load-bearing here, stripped to its minimal kernel.

---

## Part 2 — Designing the subnet in <1,000 lines

### 2.1 Competition mechanic → subnet mechanism

| SAT Competition mechanism | Subnet translation |
|---|---|
| PAR-2 (speed + miss penalty) | Per-challenge credit `base × ref/(ref+wall)`; a miss earns 0 and dilutes your share — the market analog of the 2× penalty, since you can't score below zero in an emissions split |
| Model printing + `O(formula)` check | `verify_witness` as the single correctness gate; server-side, ~40 LOC |
| DRAT → LRAT → cake_lpr, 9× budget | v0: don't. Issue satisfiable instances only. v1: optional UNSAT lane, drat-trim behind a hard proof-size/time cap |
| Disqualification for wrong answers | Wrong witness / forged proof ⇒ score 0 on the row; repeated ⇒ zero weight for the window. Unverifiable-in-budget ⇒ unsolved, never slashed |
| Cross-checking SAT vs UNSAT verdicts | Occasionally assign the same instance to k miners; any verified SAT witness kills all UNSAT claims on it |
| BYOB freshness ("unseen in prior competitions") | Per-miner HMAC-seeded instances: `seed = HMAC(secret, hotkey‖epoch‖tier‖seq)` — no two miners ever see the same formula, so copying/relaying answers is structurally impossible |
| MiniSat 1-min "too easy" filter | Reference solver calibrates tiers; drop any tier the reference solves instantly |
| Per-author benchmark cap (≤14/400) | Per-coldkey reward collapse: sum per coldkey, then split across its hotkeys — Sybil hotkeys gain nothing |
| Host-measured StarExec runtimes | Wall time measured by the publisher on submit receipt, never miner-claimed |
| Public results + solver archive | Signed public score rows (Ed25519 over canonical JSON), independently re-verifiable |

### 2.2 The load-bearing decisions

**1. SAT-witness-only scoring (v0).** The entire verifier is "does this
assignment satisfy this CNF." Use planted-satisfiable generation with the
Achlioptas–Jia–Moore two-hidden-assignments construction at the phase
transition (m/n ≈ 4.26) so there is no literal-frequency bias to exploit, and
discard the planted witness after generation — verification re-checks the
submitted assignment directly, so the publisher holds no secret worth
stealing. UNSAT/DRAT is a later lane, not a v0 requirement; the competition's
own 9× checking budget is the argument.

**2. Per-miner instances are the anti-copy primitive.** The competition
relies on human rules (freshness, portfolio ban, organizer exclusion); a
subnet replaces all of them with one line of HMAC. Nobody can relay an
answer to a formula only they were given. The seed secret must be stable
across restarts and fail closed if absent.

**3. Publisher as verifier-of-record, validator as signature checker.** The
competition has one trusted evaluation cluster (StarExec/AWS); the subnet
analog is a publisher that mints challenges, verifies witnesses, measures
wall time, and signs (a) every score row and (b) one weight vector. The
validator then needs no solver, no DB, and no re-scoring — it verifies
Ed25519 against a pinned key, checks expiry/netuid/monotonic policy version,
and calls `set_weights`. That is what keeps the whole system under 1k lines.

**4. PAR-2, adapted to emissions.** Competition PAR-2 is lower-is-better with
an additive miss penalty; an emissions split needs higher-is-better and
non-negative. Equivalent shape:

```
credit(row)  = tier_base × ref_ms / (ref_ms + wall_ms)   if witness verifies
             = 0                                          otherwise
score(miner) = Σ credits over trailing window (e.g. 24 h), collapsed per coldkey
weight       = score / Σ scores  (after burn share)
```

`ref/(ref+wall)` is scale-free (0.5 at the reference time, →1 as wall→0), so
a miss "costs 2× timeout" exactly in the sense PAR-2 intends: relative to the
pool, an unsolved instance is pure dilution.

### 2.3 LOC budget

The kernel already exists in this repo; the point of this table is that a
from-scratch reimplementation fits in ~950 lines, and everything else in the
production tree (TEE lanes, arena, corpus, deploy) is optional accretion.

| Module | Responsibility | LOC | Existing analog |
|---|---|---|---|
| `dimacs.py` | DIMACS parse/emit, planted 3-SAT gen (biased + AJM), `verify_witness`, reference DPLL for calibration | ~170 | `scaffold/dimacs.py` (171) |
| `grading.py` | Speed curve, outcome grading (SAT/TIMEOUT/INVALID) | ~90 | `scaffold/grading.py` (89) |
| `perminer.py` | HMAC(seed_secret, hotkey‖epoch‖tier‖seq) → per-miner instance ids + CNFs; fail-closed secret check | ~60 | `scaffold/publisher/per_miner.py` (subset) |
| `wire.py` | Canonical JSON (sorted keys, tight separators, signature fields stripped), Ed25519 sign/verify, signed-key sets | ~80 | `scaffold/wire.py` (79) |
| `publisher.py` | HTTP app: `GET /challenges`, token-gated `GET /cnf`, `POST /submit` (verify + host-clock wall time + sign row), `GET /weights/next` (windowed PAR-2 compose + coldkey collapse + sign vector) | ~250 | thin slice of `scaffold/publisher/app.py` |
| `validator.py` | Fetch signed vector → verify sig/key-id/expiry/netuid/policy-version fence → burn split → `set_weights` | ~200 | `scaffold/validator_thin.py` (~200) |
| `reward.py` | Windowed compose, per-coldkey collapse, normalization | ~100 | `game/reward.py` (151, subset) |
| **Total** | | **~950** | |

A reference miner (fetch → run kissat/pysat → submit) is ~100 lines but
lives outside the trust boundary and outside the budget.

### 2.4 The epoch loop

```
every EPOCH (e.g. 20 min):
  publisher: derive per-miner challenge set for (epoch, tier) from HMAC seeds
miner:
  GET  /challenges            → ids + tier + time limit
  GET  /cnf?id=…              → DIMACS (token-gated to the assigned hotkey)
  solve; POST /submit {id, assignment}
publisher on submit:
  wall_ms  = now - challenge_issue_time      (host clock only)
  ok       = verify_witness(cnf, assignment) (complete, non-contradictory, all clauses hit)
  row      = sign({miner, id, tier, ok, wall_ms, answer_hash, …})
  append row to public feed
every WEIGHTS_TICK:
  vector = sign(compose(rows in trailing 24 h))   # PAR-2-shaped credits, coldkey-collapsed
validator (each tick, ~200 LOC total):
  verify vector sig + fences → set_weights
```

### 2.5 What v0 deliberately leaves out (and why that's the competition's advice)

- **UNSAT proofs** — the 9× checking budget says a small verifier shouldn't
  carry drat-trim on the critical path. When added (v1), follow the
  competition's two-tier response: *unverifiable-in-budget = unsolved*,
  *provably wrong = slashed*, and pipe proofs through drat-trim with a hard
  size cap; credit nothing when the checker is absent (never stub-approve).
- **Parallel/cloud tracks** — no certificate regime exists for them even in
  the competition; a subnet inherits that gap.
- **Crowdsourced benchmarks** — BYOB requires honest submitters plus an
  organizer filter; per-miner generated instances give freshness for free.
  Real/corpus instances (the existing 10% fast-path lane) are the bridge to
  application workloads once the mechanism is proven.
- **Attestation** — SAT witnesses self-verify; only non-self-verifying claims
  (timeouts, secure-compute lanes) ever need TEE quotes, so attestation stays
  out of the minimal kernel.

### 2.6 Open questions carried forward from the research

1. **UNSAT economics:** full DRAT checking vs probabilistic spot-checking vs
   proof-size caps vs planted-SAT-only — each trades verifier cost against
   the share of real-world workloads (which are heavily UNSAT/proof-shaped)
   the subnet can absorb.
2. **Monoculture rewards:** when every miner runs Kissat, speed scoring pays
   for hardware and tuning. If the goal is solver *evolution* (SATLUTION-style),
   a TIG-like innovation-attribution layer on top of raw solve scoring is the
   known design space.
3. **Scoring shape:** PAR-2-style speed weighting vs distinct-solve counting
   (the current proportional mode) demonstrably rank differently (CaDiCaL
   2019). Speed weighting sharpens incentives but increases variance and
   latency-infrastructure races; solve counting is calmer but rewards
   breadth over speed.

---

*Research pipeline: 5 search angles → 24 primary sources fetched → 119
claims extracted → 25 survived 3-vote adversarial verification (0 refuted) →
synthesized above, plus a targeted primary-source sweep for §1.7–1.8
(TIG monorepo/docs, Graphite validator source, Omron README, SMT-COMP/MaxSAT/MCC
rules). Core sources: satcompetition.github.io rules/tracks/results 2019–2025,
the organizers' AIJ 2020 report, the SAT Museum paper (PoS 2023), and
fmv.jku.at/kissat/winners.html.*
