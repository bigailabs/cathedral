# Cathedral subnet scaffold — minimalist, lane-based

A <2k-line scaffold for a lane-based Cathedral subnet. Each **lane** is a
self-contained challenge mechanism sharing one common core. It runs end-to-end
today (mint → submit → grade → score → weights) with some lane edges stubbed —
**the structure is the deliverable.**

```
python3 -m scaffold.demo          # offline, zero deps
POLARIS_LIVE=1 POLARIS_URL=… POLARIS_KEY=… python3 -m scaffold.demo   # real /v1/attest
```

Total: **~1,300 lines of Python**, stdlib-only (the live attest path imports
`httpx` lazily; the offline demo needs nothing).

## Core invariant

**Only an independently-verified artifact earns positive weight.** A SAT witness
self-verifies against the CNF, so a verified solve (Lane A/B) or a verified
counterexample (Lane C) earns weight — and that is the *only* thing that does.
Claims that cannot be verified offline earn **zero**: a stubbed UNSAT cert (until
a real DRAT/LRAT checker is wired) and an "is-safe" verdict (proving safety at
width is the cliff) pay nothing — not as a penalty, simply no reward. The
equivalence/vacuity gate, windowed traps, and cross-miner consensus are the
**flag / penalty layer** (they mark bad-faith or refuted claims); they are never
a reward path. This invariant is what the 8 codex findings forced, and it is the
load-bearing anti-farm property of the whole scaffold.

## How this extends the prior work

This builds on `cathedral/src/cathedral/lanes/` — the clean **Task Family
Contract** (`contract.py`: pure `generate`/`verify`/`score`, no-I/O, no-clock,
hidden-stays-hidden) and its two lanes (`synthetic_boolean_v1` = SAT,
`solver_improvement_v1` = the unmerged "clean reference" solver lane). The
scaffold keeps that contract verbatim (renamed to `mint_challenge` /
`validate_submission` / `score` to match the brief), and:

- **Lane A (`sat_challenge_v1`)** = `synthetic_boolean_v1` reduced to the
  contract: publish CNF, fastest valid witness wins.
- **Lane B (`solver_docker_v1`)** = `solver_improvement_v1` + the two things it
  lacked: the submitted solver runs as an **attested Docker image** (image
  digest = MRTD, the Entrius pin) and an **UNSAT** outcome (proof cert), keeping
  the 80/20 runner/solver split.
- **Lane C (`encoding_v1`)** = NEW. The encoding subnet, **bounded to
  bug-finding** per the v1.2 cliff: mint find-a-planted-bug challenges, never
  prove-safety-at-full-width. Faithfulness via the gates below.

## Architecture

```
scaffold/
  contract.py     Lane interface {mint_challenge, validate_submission, score} + wire types + Outcome
  registry.py     register / lookup / active — the only seam lanes plug into
  grading.py      shared 3-outcome grade (SAT/UNSAT/TIMEOUT) + attest-timeouts-only policy + speed curve
  verify.py       verification primitives: SAT witness, UNSAT cert, attestation — reused by all lanes
  dimacs.py       planted-3SAT generator, witness verifier, tiny DPLL solver
  polaris.py      Polaris client: /v1/attest, /api/keys, /api/billing ledger (offline stub by default)
  pinning.py      difficulty = (property, width, SOLVER) — recorded, not assumed (v0/v1 finding)
  validator.py    the loop: mint → dispatch by family_id → collect → score → normalize weights
  lanes/
    sat_challenge.py    Lane A
    solver_docker.py    Lane B
    encoding.py         Lane C (+ consensus() helper + the 4 faithfulness gates)
  demo.py         end-to-end run with honest + adversarial miners per lane
```

Lanes are independent: adding/removing one touches only `registry.install_default_lanes()`.

## The map — real / stub / Polaris calls

| Component | Status | Notes |
|---|---|---|
| Lane contract, registry, validator loop, weight normalize | **REAL** | pure functions; uniform dispatch |
| 3-outcome grading + attest-timeouts-only policy + speed curve | **REAL** | `grading.py` |
| SAT witness check + DPLL solve | **REAL** | `dimacs.py`; the correctness gate |
| Encoding faithfulness gates (equivalence/vacuity, windowed trap) | **REAL** | `encoding.py`; bounded, in-band |
| Cross-miner consensus gate | **REAL** | `consensus.py` (core); validator scatters each Lane-C challenge to all miners and resolves across them |
| Solver pinning (property, width, solver) | **REAL** | `pinning.py`; stamped per challenge |
| UNSAT proof cert | **STUB** | shape-check only; real = vendored `drat-trim`/`lrat-check`. Flags `stub=True`, never claims a proof was verified |
| Attestation quote | **STUB offline / REAL live** | offline = deterministic stub; `POLARIS_LIVE=1` → real `POST /v1/attest` (Intel-TDX, Stitch) |
| Miner transport | **STUB** | in-process pool; real = publisher submit endpoint feeding the same contract |
| Challenge source | **REAL (synthetic)** | planted-3SAT in-tree; real launch leases from the private generator (`sat-generator-contract.md`) |

### What each lane calls in the Polaris API

| Lane | Polaris surface | When |
|---|---|---|
| A — SAT | `/api/keys` (miner auth), `/api/billing` ledger (credit accepted solves) | every solve |
| B — solver-Docker | **`POST /v1/attest`** to bind the pinned image MRTD — **timeouts only** (SAT/UNSAT self-verify, so no attest spend); `/api/keys`; `/api/billing` runner+solver split | on a timeout claim |
| C — encoding | **`POST /v1/attest`** to vouch a TIMEOUT (an honest "no in-band bug found in budget"); `/api/keys`; `/api/billing` | on a timeout claim |

The scaffold reuses these — it does **not** reimplement attestation, auth, or
billing. `polaris.PolarisClient` is the seam.

## Honest scope (the constraints, held)

- **Bug-finding only (Lane C).** Width capped at `MAX_INBAND_WIDTH = 12`; every
  instance is tractable. No safety-proving challenge is ever minted — those land
  on the EVM-SMT cliff (W≈52→56) and are permanent timeouts with no signal.
- **Attest timeouts only.** SAT witnesses and UNSAT certs self-verify, so they
  never cost an attestation. Only the unfalsifiable "I ran out the clock" claim
  is attested. (`grading.attestation_required`)
- **Windowed traps are never near the cliff.** A trap is a known in-band planted
  bug; a near-cliff trap would false-nuke honest miners who legitimately time
  out. Enforced by the same in-band width cap.
- **The durable tooth is the equivalence gate + consensus, and they catch
  different attacks** (neither alone is sufficient):
  - *consensus* catches the cross-miner outlier — a peer's verified
    counterexample refutes every "safe" peer, even a sound-but-incomplete one.
  - *the equivalence/vacuity gate* catches collective blindness — a vacuous
    encode everyone agrees on passes consensus but fails the probe battery,
    because earning a "safe" verdict requires an encode that actually flags
    known-buggy inputs (real work). Structural held-out rotation is weak against
    a reasoner; this is the part that needs solving.
  - The demo shows all three on different instances: equivalence gate drops the
    vacuous clique on a SAFE instance (consensus passes them); a windowed trap
    drops a "safe" on a planted bug; consensus drops a sound-but-incomplete
    `missed_safe` that equivalence + trap both passed.

- **Scoring principle (hardened after the codex review): only an independently
  VERIFIED artifact earns positive weight.** A SAT witness self-verifies → it
  pays. A "safe"/UNSAT claim that the validator cannot verify offline (proving
  safety at width is the cliff; the UNSAT cert checker is a stub) earns **0** —
  not a penalty, just no reward. So "safe" cannot be farmed: a constant
  always-safe miner scores nothing. The equivalence gate, windowed traps, and
  consensus are the **penalty / flag layer** (they mark bad-faith or refuted
  claims), not a reward path.

- **Honest limitation (the real bound, not hidden):** because "safe" is
  unrewarded rather than oracle-confirmed, a real non-trap bug that NO miner
  finds simply goes undetected and unrewarded — a miss, not a false payout.
  Windowed traps sample for it, the equivalence gate raises encode quality, and
  consensus surfaces it the moment any miner finds it. Closing the residual
  collective-blindness gap fully is the cliff problem, intrinsic to bug-finding,
  not solved here.

## Codex review (grading/scoring path + gates)

A `codex exec` pass over the scoring path found 8 concrete exploits; all fixed
and regression-checked (each now scores 0 / is rejected):

1. **Fake UNSAT earned full credit** — a stub cert (`"0\n"`) scored 1.0.
   Fix: UNSAT credits only on `chk.ok and not chk.stub`; stub → INVALID.
2. **Self-asserted "safe" earned credit** — `{"verdict":"safe","solved":true}`
   scored 1.0 on a buggy instance. Fix: "safe" earns 0 (only verified artifacts
   pay); consensus/traps/equivalence remain the penalty layer.
3. **Duplicate submissions multiplied weight** — replaying a solve 3× → 3×
   weight. Fix: one scored submission per `(task_id, miner_hotkey)`.
4. **Miner `wall_ms` could yield >1 / inf / late-positive scores.** Fix:
   `speed_bonus` rejects non-finite/negative and clamps to the time limit.
5. **Solver-Docker paid self-reported speed** on unattested SAT runs. Fix: that
   lane is not speed-aware (timing isn't verifiable under attest-timeouts-only).
6. **Malformed assignments self-verified** — out-of-range vars padded
   completeness; missing vars defaulted False. Fix: `verify_witness` requires
   exactly vars `1..n`, no extras, no contradictions.
7. **Wrong-task submissions scored** — `submission.task_id` was ignored. Fix:
   the round drops any submission whose `task_id` ≠ the challenge's.
8. **`grade()` failed open on NaN `raw_metric`** (→ 1.0). Fix: non-finite
   metric → 0.

No `TIMEOUT`/`INVALID` path earns positive weight. Re-run after fixes is green
and all gates still fire.

## Provenance of the design inputs

- Gauntlet is **`v0`** (`~/experiments/evm-gauntlet-v0-gap1` on Stitch) — an EVM
  mutation-gate experiment (base + `flip_cmp`/`off_by_one`/`op_swap`/`overflow`/
  `wrong_const` mutants + `safe`/`alwaysbad`/`triv` controls). There is no
  written v1/v1.1/v1.2; the held-out-weak / false-alarm-durable / windowed-trap
  bounds are Fred's synthesis on top of v0 + the EVM-SMT cliff write-up. Lane C's
  mutant names mirror v0 deliberately.
- The SAT/UNSAT cliff + encode-competition model: `~/notes/cathedral-evm-smt-experiment-writeup.md` §7b.
- Polaris attest surface: `~/attestor/DEMO.md` (the real `/v1/attest`, not the
  stubbed `/v1/runtime/run`).

Not deployed. Local run only. Nothing committed.
