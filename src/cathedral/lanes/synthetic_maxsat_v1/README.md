# synthetic_maxsat_v1 — lane brief

The lane author owns everything inside
`src/cathedral/lanes/synthetic_maxsat_v1/` and nothing outside it. When
the contract tests at `tests/lanes/test_contract.py` pass, the lane is
mergeable. Validator wiring, weight allocation, and on-chain rollout are
separate PRs by the platform team.

## The contract to implement

Three pure functions on `MaxsatV1` (already stubbed in `__init__.py`):

```python
def generate(ctx: GenerateCtx) -> tuple[PublicProblem, HiddenMetadata]
def verify(problem, hidden, submission) -> VerifierResult
def score(problem, verifier) -> ScoreResult
```

Rules are in `src/cathedral/lanes/contract.py` docstring. Summary:
deterministic from `(seed, tier)`, zero I/O, no clock, no unseeded
randomness, bounded `[0.0, 1.0]` scores, `verify` never raises.

## Suggested shape (not prescriptive — author's call)

**`public_input`:** a weighted CNF formula. JSON-serializable shape, e.g.

```json
{
  "n_variables": 200,
  "clauses": [
    {"lits": [1, -3, 5], "weight": 7},
    {"lits": [-2, 4], "weight": 3}
  ]
}
```

**`hidden_payload`:** at minimum the hidden optimum (or a proven upper
bound) so `raw_score` can be normalized against optimum, not against
total clause weight. Also: the generator state needed to reproduce the
instance from `(seed, tier)`.

**`submission.answer`:** the variable assignment, e.g.
`{"assignment": {"1": true, "2": false, ...}}`.

**`raw_metric`:** `satisfied_weight / hidden_optimum`. A 0.9 at a hard
instance is more impressive than 0.95 of total weight at an easy one.

**`weighted_score`:** `clamp(raw_metric, 0.0, 1.0) * tier_multiplier`,
then clamped to `[0.0, 1.0]`. Tier multipliers are a design call.

## The three load-bearing decisions

### 1. AI-vs-solver (the critical one)

Max-SAT has 30 years of classical solver engineering (RC2, Loandra,
EvalMaxSAT). Off-the-shelf solvers will dominate any LLM-driven approach
at any size where they fit in memory. Pick one and justify it in a
`DESIGN.md` next to this README:

- **(a) Require a model trace.** Lane requires a captured agent trace
  bundle as a precondition for positive score (mirrors the evidence
  lane pattern). Solver-only miners score 0.
- **(b) Accept solver-engineering.** Public position: this lane rewards
  whoever solves it, model or not. Be honest about it.
- **(c) Adversarial instances.** Generate Max-SAT instances where
  classical heuristics are known to fail (community structure,
  phase-transition tuning, etc.). Hard mode but cleanest.

Pick one. Do not ship without picking.

### 2. Difficulty tier calibration

A tier is a fiction until measured. Deliverables alongside the generator:

- `calibration.py` — script that runs a baseline LLM agent
  (`gpt-4o-mini` or similar) across N instances per tier.
- `calibration_results.json` — checked into the repo. Shows the curve:
  tier -> expected weighted_score, expected wall-clock seconds.
- The contract test reads this file and asserts: tier monotonicity
  (higher tier => lower expected score), `time_limit_seconds` in
  `generate` matches the calibrated bound.

### 3. Fixture suite

Two folders, both gated by the contract test:

- `fixtures/golden/` — `(seed, tier, expected public_input,
  expected hidden_payload, optimal_submission, expected weighted_score)`
  tuples. Proves the generator and scorer are byte-reproducible.
- `fixtures/adversarial/` — submissions that should all score 0
  cleanly without raising:
  - malformed JSON (missing `assignment` key)
  - partial assignment (only half the variables)
  - all-zero assignment
  - one-bit-off-optimal (sanity check that scoring is not a step function)
  - oversized payload
  - unicode garbage

Format: each fixture is a single JSON file with `name`, `seed`, `tier`,
`submission`, and either `expected_weighted_score` (golden) or
`expected_rejection_reason` (adversarial).

## Local development

```bash
# Run the contract suite against this lane only:
PYTHONPATH=src pytest tests/lanes/test_contract.py -k synthetic_maxsat_v1 -v

# Run the generator manually:
PYTHONPATH=src python -c "
from cathedral.lanes.synthetic_maxsat_v1 import MaxsatV1
from cathedral.lanes.contract import GenerateCtx
public, hidden = MaxsatV1().generate(GenerateCtx(seed=42, tier=1, issued_at_iso='2026-05-18T00:00:00.000Z'))
print(public.model_dump_json(indent=2))
"
```

## Merge checklist

1. All contract tests green.
2. `DESIGN.md` answers the AI-vs-solver question.
3. `calibration_results.json` checked in with real numbers from a real
   baseline run.
4. At least 5 golden fixtures and 6 adversarial fixtures.
5. Lane registered in `src/cathedral/lanes/registry.py` (uncomment the
   two lines at the bottom).
6. Open the PR. Platform team reviews the design call and merges.
7. Weight stays at 0 on mainnet until validator wiring lands (separate
   PR).

## What's NOT in scope for this lane

- The publisher (`src/cathedral/publisher/`)
- The signer (`src/cathedral/eval/scoring_pipeline.py`)
- The Linux jail (`src/cathedral/v4/...`)
- The validator pull loop
- Anything network, anything on-chain, anything in `eval/`

If a lane needs to import from any of those, stop and escalate. The lane
should be hermetic.
