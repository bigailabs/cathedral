# synthetic_boolean_v1 -- design

## Formulation

Random 3-SAT with a **planted satisfying assignment**, DIMACS CNF on the
wire, binary 1.0 / 0.0 scoring on the verified answer.

The lane is named `synthetic_boolean_v1` (boolean challenge family) so a
future generator can ship Max-SAT, bounded boolean CSP, or pseudo-boolean
inside the same schema without renaming. The v1 release ships satisfiable
DIMACS SAT only.

## Why this formulation

* **Verifier is cheap.** O(num_clauses) per submission. A validator that
  pulls already-scored rows from the publisher pays no SAT cost at all;
  the publisher pays the verification cost during scoring, and that
  cost is microseconds.
* **Verifier is honest.** SAT verification is the textbook example of
  "verify is in P, solve is NP." A miner cannot fake a satisfying
  assignment; any assignment we accept actually satisfies the formula.
* **Generator is honest.** Planted-3-SAT is the standard adversarial
  benchmark generator. Same seed deterministically produces the same
  formula; different seeds produce uncorrelated formulas.
* **Solution is small and copyable.** This is also the failure mode.
  See "Anti-gaming" below.

## Scope (v1)

In scope:

* DIMACS CNF formulas, generated deterministically from `(seed, tier)`.
* Solver-style `s SATISFIABLE` / `v <lit> ... 0` answers.
* Structured `{"assignment": {...}}` answers as an alternative.
* Binary scoring: full satisfying assignment -> 1.0, anything else -> 0.0.
* Tier-based difficulty (variables, clauses).
* Pure-Python verifier with no subprocess, network, filesystem, or LLM.

Out of scope (deferred):

* **UNSAT formulas.** Requires DRAT/LRAT proof verification, which we do
  not implement in v1. Generator only emits satisfiable instances.
* **Max-SAT / partial-credit scoring.** A future schema bump can add
  `answer_type: "max_sat"` with a continuous score. Until then, v1 stays
  binary.
* **Bounded CSP / pseudo-boolean.** Plug-shape supports it; v1 generator
  doesn't ship it.
* **Solver provenance / trace verification.** Hermes trace is collected
  by the platform as a sidecar (for provenance and training data) but
  does not contribute to v1 score. A later schema bump may add hard-to-
  fake trace requirements when the verifier can enforce them
  deterministically.

## Schemas

### `PublicProblem.public_input`

```jsonc
{
  "format": "dimacs_cnf",
  "dimacs": "p cnf 10 30\n1 -2 3 0\n...",
  "num_vars": 10,
  "num_clauses": 30,
  "answer_format": "solver_output",
  "instructions": "..."  // miner-facing instruction string
}
```

### `Submission.answer` (two accepted shapes)

Canonical:

```jsonc
{
  "solver_output": "s SATISFIABLE\nv 1 -2 3 0\n"
}
```

Structured:

```jsonc
{
  "assignment": {
    "1": true,
    "2": false,
    "3": true
  }
}
```

Every variable in `1..num_vars` must appear in the assignment; partial
assignments are rejected with `partial_assignment`.

### `HiddenMetadata.hidden_payload`

```jsonc
{
  "planted_assignment": {"1": true, "2": false, ...},
  "num_vars": 10,
  "num_clauses": 30
}
```

The planted assignment is the witness used to prove that the generated
formula is satisfiable. It is **not** the only acceptable answer: the
verifier checks any submitted assignment against the formula and accepts
any satisfying one.

### `VerifierResult`

| field              | shape                                             |
| ------------------ | ------------------------------------------------- |
| `parsed_ok`        | bool                                              |
| `raw_metric`       | 1.0 if every clause satisfied, else 0.0           |
| `rejection_reason` | one of the constants in `verifier.py`             |
| `details`          | for debugging: which clause failed, etc.          |

### `ScoreResult`

| `weighted_score`   | 1.0 if `raw_metric == 1.0`, else 0.0              |
| `score_parts`      | `{"binary": 1.0}` or `{}`                         |

## Scoring policy

```
correct full satisfying assignment        -> 1.0
incorrect assignment                       -> 0.0  (unsatisfied_clause)
partial assignment                         -> 0.0  (partial_assignment)
malformed solver output                    -> 0.0  (solver_output_parse_failed)
wrong types / missing keys                 -> 0.0  (wrong_answer_type / missing_answer)
malformed public CNF (should never happen) -> 0.0  (formula_parse_failed)
```

Rejection reason strings are part of the wire contract; downstream audit
and dataset tooling key on them.

## Anti-gaming considerations

Cathedral's `first_unique_verified` semantics (claim_key dedup) live at
the platform layer; this lane just produces a verifiable score per
submission. The platform-side ordering decides who wins emissions when
multiple miners submit the same satisfying assignment.

Things this lane intentionally does **not** try to solve here:

1. **Copy-farm relay attacks.** If miner A submits a satisfying
   assignment and 49 miner-B nodes echo it, the platform's
   first-unique-verified rule determines winner; the lane returns 1.0
   for all of them. The platform must dedup. This is the same shape as
   v2's first-unique-source-event problem.
2. **Hardware-only races.** Pure first-to-solve on stock kissat
   rewards CPU frequency, not algorithms. The lane does not score
   solve time. If the platform wants reference-normalized solve time
   or trace-bundle provenance to dominate, that scoring goes in a
   future lane (or a future schema version of this lane that
   incorporates the Hermes trace into `score`). Tracked as a release
   gate; see `docs/lanes/synthetic_boolean_v1-release-plan.md`.

## Tier table

| tier | num_vars | num_clauses | clause/var ratio | notes                |
| ---- | -------- | ----------- | ---------------- | -------------------- |
| 0    | 10       | 30          | 3.00             | smoke / unit tests   |
| 1    | 20       | 80          | 4.00             | testnet warmup       |
| 2    | 40       | 170         | 4.25             |                      |
| 3    | 80       | 340         | 4.25             |                      |
| 4    | 160      | 680         | 4.25             |                      |
| 5    | 320      | 1360        | 4.25             | hard limit (`_MAX_CLAUSES=4096`) |

Ratios near 4.25 sit on the satisfiable side of the well-studied 3-SAT
phase transition. Generated formulas are guaranteed satisfiable by
construction (planted) so phase-transition difficulty does not block
solving; the ratio just controls how non-trivial solving is.

## Determinism contract

Same `(seed, tier)` produces byte-identical `PublicProblem` and
`HiddenMetadata`. This is enforced by
`tests/lanes/test_contract.py::test_generate_is_deterministic`.

If you change the generator (`problem.py`), bump
`_GENERATOR_VERSION` so the `task_id` for the same `(seed, tier)`
changes -- old corpora and new corpora must never collide on task_id.

## Banned imports (lane-level invariant)

This lane must not import: `requests`, `httpx`, `aiohttp`, `urllib.*`,
`urllib3`, `socket`, `subprocess`, `os.system`, `time`, `datetime`.
The contract test walks the AST and fails the suite if any appear.
The publisher provides any timestamp via `GenerateCtx.issued_at_iso`.
