# synthetic_boolean_v1 -- release plan

Scope of this document: what is in this PR, what is stubbed, what comes
from existing v3/v4 infrastructure, what the lane author (Serge) still
owes us, what gates must be true before this lane gets weight > 0, what
the smoke suite proves, and what mainnet must not expose.

This is **release-prep scaffold**. Not mainnet enablement.

## What is ready (this PR)

* `src/cathedral/lanes/synthetic_boolean_v1/` -- contract-conformant
  lane package implementing `TaskFamily`.
  * `problem.py` -- deterministic planted-3-SAT generator with tier
    table 0..5, `_MAX_CLAUSES=4096` hard cap.
  * `verifier.py` -- pure-Python DIMACS SAT verifier, binary 1.0/0.0
    output, taxonomy of rejection reasons.
  * `dimacs.py` -- DIMACS CNF parser + solver-output parser +
    serializer. Strict mode (rejects anything ambiguous).
  * `__init__.py` -- `SyntheticBooleanV1` class wiring generator +
    verifier into `generate`/`verify`/`score`.
  * `DESIGN.md` -- v1 formulation, schemas, tier table, anti-gaming.
  * `README.md` -- lane-author brief (carried from PR #150).
  * `fixtures/golden/` -- 3 fixtures (solver_output shape,
    assignment-dict shape, tier 1 instance).
  * `fixtures/adversarial/` -- 7 fixtures (wrong, partial, missing,
    wrong type, malformed, unsat-status, extra-var).
* `src/cathedral/lanes/lifecycle.py` -- platform-side lifecycle state
  machine (generated -> active -> scored -> retired -> revealed) plus
  `ChallengeRecord.to_public_payload()` redaction helper and
  `assert_public_payload_safe()` leak guard.
* `tests/lanes/test_synthetic_boolean_v1_smoke.py` -- 10 E2E smoke
  tests covering generation, public/hidden split, valid + invalid
  scoring, lifecycle walk, sidecar shape, no-subprocess defense.
* `src/cathedral/lanes/registry.py` -- registers `SyntheticBooleanV1`.
* `docs/lanes/synthetic_boolean_v1-data-collection.md` -- integration
  plan for Hermes / bundle publisher / sidecar / dataset catalog.

CI gate: `pytest tests/lanes/ -v` -> 19 passed (9 contract + 10 smoke).

## What is stubbed / deferred

* **Publisher dispatch.** The `score_and_sign` branch that routes a
  `synthetic_boolean_v1` task into `lane.generate / verify / score`
  is not wired. The integration seam is documented in
  `synthetic_boolean_v1-data-collection.md`. Next PR.
* **Prompt builder.** A small `build_dimacs_prompt(public_input)`
  helper that turns a `PublicProblem.public_input` into the miner
  prompt is not shipped. Trivial; next PR.
* **Submission parser.** The helper that pulls `Submission.answer`
  from a `TraceBundle.hermes_stdout` (fenced-JSON shape) is not
  shipped. Mirrors `cathedral.eval.scorer_v2_publisher.parse_claim_v2`.
* **Mainnet weight allocation.** Lane weight stays at 0 until release
  gates below are satisfied.
* **UNSAT support.** Out of scope; requires DRAT/LRAT proof I/O
  handling. Lane generator only emits satisfiable instances.
* **Max-SAT partial credit.** Out of scope for v1; a future schema
  bump (`synthetic_boolean_v2`) could add it.
* **Trace provenance scoring.** Hermes trace is collected as a
  sidecar but does not feed v1 score. A future schema may incorporate
  trace if the verifier can enforce hard-to-fake trace requirements.
* **Reference-time normalization / hardware-fairness scoring.** v1
  scores binary on correctness only. See "Open design gates" below.
* **Copy-farm dedup.** Handled at the platform `claim_key` layer (like
  v2's `first_unique_verified_source_event`), not in the lane. The
  dedup key for SAT submissions still needs to be specified at the
  platform level. See "Open design gates" below.

## What comes from v3

* `cathedral.eval.ssh_hermes_runner.SshHermesRunner` -- captures
  prompt -> Hermes -> `TraceBundle`. Reused as-is; SAT prompt is just
  a string from the lane.
* `cathedral.eval.bundle_publisher.EvalArtifactPublisher` -- uploads
  the trace bundle to private storage and returns manifest + hash.
* `cathedral.v3.score_sidecar` -- template for the private sidecar
  shape. SAT lane writes a parallel sidecar under its own schema id.
* `cathedral.storage.HippiusClient` -- private-storage transport for
  sidecars.
* `cathedral.v3.datasets.catalog` / `export` -- training-dataset
  ingestor pattern. SAT sidecar fits the existing per-task-family
  switch.

## What comes from v4

* `cathedral.v4.sign.build_signed_v4_row` -- canonical-JSON +
  Ed25519 signing pattern. The SAT wire row will use the same signer
  (`EvalSigner`) with a `synthetic_boolean_v1`-specific signed keyset.
* `cathedral.v4.verify.verify_v4_row` -- validator-side verify
  pattern. SAT-lane rows will use a parallel `verify_synthetic_boolean_v1_row`
  with a matching pinned keyset.
* The "publisher executes / validator only verifies signature" split
  is the architectural invariant the SAT lane respects.

## What Serge (lane author) still needs to deliver

Per `README.md` merge checklist, the lane author's job for v1 is done
by this PR -- `generate`, `verify`, `score`, fixtures, contract green.
What the author owes us **for tier elevation past testnet**:

1. **Difficulty calibration baseline.** Run stock kissat against tier
   0..5 on a reference machine; record p50 / p99 wall clocks. This
   sets validator verification-cost ceilings per tier and informs
   weight allocation across tiers.
2. **Adversarial fixture expansion.** Current set is 7; we want at
   least 20 once we have weight, covering at minimum:
   * the empty CNF
   * a 1-variable formula
   * a 1-clause formula
   * pathological clause shapes (all-same-variable, contradictions)
   * solver outputs with extra `s` / `v` lines, comments interleaved
   * unicode garbage in the solver output
   * extremely long literals
3. **Trace-aware scoring proposal (if any).** If the lane author
   wants trace-bundle provenance to count toward score, propose the
   schema bump and provide deterministic-verification rules.
4. **Max-SAT plug-in (if shipped).** If the author wants Max-SAT under
   the same lane: define `answer_type` switch + raw_metric +
   normalization. Otherwise ship as a separate lane.

## Open design gates (must resolve before weight > 0)

These are platform-level decisions, not lane-level. They block enabling
SAT weight on mainnet regardless of whether the lane code is ready.

1. **Reward rule.** v1's `weighted_score` is correctness only. The
   first-to-solve dynamic is determined by the platform's
   `first_unique_verified` dedup. We need to decide before mainnet
   weight whether to also normalize against reference solve time or
   require trace-bundle provenance. Pure first-to-solve at scale
   becomes a hardware race; the lane doesn't try to solve that.
2. **claim_key for SAT submissions.** The platform's atomic
   first-unique dedup needs a canonical dedup key. Proposed:
   `sha256(task_id | sha256(canonical_assignment_bytes))`. Must be
   specified and shipped at platform layer before SAT can win
   emissions; copy-farms beat us otherwise (this was the v1
   gameability bug, repeated).
3. **Public feed lifecycle.** Public reads of `lifecycle_state ==
   active` must NOT include solution. The lifecycle module enforces
   this, but the publisher feed endpoint needs to use
   `record.to_public_payload()` not `record.public.model_dump()`. The
   wiring PR will plumb this.
4. **Reveal policy.** When does a retired challenge move to
   `revealed`? Default proposal: never (operator-only manual reveal,
   never automatic). Decision belongs to operations, not the lane.
5. **Validator verification-cost ceiling.** Validators that re-verify
   pulled rows (out of v1 scope but on the roadmap) need a per-tier
   cost ceiling so they can size their hardware. Tied to gate 1
   (calibration baseline).

## What the smoke suite proves

`tests/lanes/test_synthetic_boolean_v1_smoke.py` (10 tests):

| test                                                | proves                                                  |
| --------------------------------------------------- | ------------------------------------------------------- |
| `test_generate_produces_public_and_hidden`          | generator produces correct shapes from seed/tier        |
| `test_public_payload_excludes_planted_assignment`   | wire payload has no hidden_payload, even on deep walk   |
| `test_planted_solver_output_scores_one`             | canonical solver_output answer scores 1.0               |
| `test_structured_assignment_scores_one`             | dict-of-bools answer scores 1.0                         |
| `test_flipped_assignment_scores_zero`               | wrong answer scores 0.0 with unsatisfied_clause         |
| `test_garbage_answer_scores_zero`                   | malformed answer scores 0.0 with missing_answer         |
| `test_lifecycle_transitions_walk_to_reveal`         | state machine reaches revealed via legal transitions    |
| `test_illegal_transition_rejected`                  | bad transitions raise                                   |
| `test_sidecar_can_reference_hidden_metadata`        | sidecar carries hidden refs; wire row never does        |
| `test_verifier_is_pure_python_no_subprocess`        | verifier does not spawn subprocesses                    |

Plus the 9 contract tests
(`tests/lanes/test_contract.py`):

| test                                                | proves                                                  |
| --------------------------------------------------- | ------------------------------------------------------- |
| `test_lane_has_no_banned_imports`                   | no network / clock / subprocess imports                 |
| `test_generate_is_deterministic`                    | same seed/tier -> byte-identical output                 |
| `test_generate_returns_correct_family_id`           | family_id and schema_version stamped correctly          |
| `test_generate_obeys_pydantic_models`               | typed models, not dicts                                 |
| `test_verify_is_total_on_garbage`                   | verify never raises                                     |
| `test_score_is_bounded`                             | weighted_score clamped to [0, 1] for hostile inputs     |
| `test_malformed_submission_scores_zero`             | malformed -> 0.0 + rejection reason                     |
| `test_golden_fixtures_reproduce`                    | golden fixtures replay deterministically                |
| `test_adversarial_fixtures_score_zero_cleanly`      | every adversarial fixture scores 0.0                    |

Total CI gate: 19 tests passing.

## What mainnet must not expose

Hard requirements for any mainnet feed that surfaces this lane:

1. **No planted assignment, ever, while the challenge is active or
   retired-but-not-revealed.** Enforced by
   `ChallengeRecord.to_public_payload()`.
2. **No Hermes trace bundle contents.** The bundle hash may surface;
   the contents stay in encrypted private storage.
3. **No miner stdout / prompt completions.** Stdout may include the
   answer plus model chain-of-thought; the wire row carries only the
   parsed answer's small excerpt plus the score.
4. **No `s UNSATISFIABLE` rows.** v1 doesn't produce UNSAT challenges
   and the verifier rejects UNSAT submissions; nothing to expose.
5. **No raw generator state.** `HiddenMetadata` is private; only the
   serialized public DIMACS text is shipped.

## Out of scope for this PR

* Publisher `score_and_sign` dispatch wiring
* `build_dimacs_prompt` and `parse_submission_from_trace` helpers
* Signed wire-row schema for `synthetic_boolean_v1`
* Validator-side `verify_synthetic_boolean_v1_row`
* Weight allocation entry (stays 0%)
* Linux jail for verifier (verifier is pure Python, no jail needed)
* Mainnet rollout

All deferred to follow-up PRs. This PR establishes the lane, the
verifier, the lifecycle, the fixtures, the smoke suite, and the
integration plan. Subsequent PRs land each item above behind env gates
without changing the lane code.
