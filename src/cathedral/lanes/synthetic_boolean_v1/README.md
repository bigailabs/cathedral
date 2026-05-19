# synthetic_boolean_v1 lane brief

This lane is the authoring target for the first boolean challenge family.
The lane author owns the challenge corpus and scoring mechanism inside this
directory. Platform code owns miner transport, Hermes trace collection,
signing, publisher feeds, validator pulls, weights, and rollout.

The author does not need to decide Cathedral's rails. The author does need
to decide exactly what boolean problem miners solve, what answer they return,
and how Cathedral verifies and scores that answer.

## The contract to implement

Three pure functions on `SyntheticBooleanV1` in `__init__.py`:

```python
def generate(ctx: GenerateCtx) -> tuple[PublicProblem, HiddenMetadata]
def verify(problem, hidden, submission) -> VerifierResult
def score(problem, verifier) -> ScoreResult
```

Rules are in `src/cathedral/lanes/contract.py`. Summary: deterministic
from `(seed, tier)`, zero I/O, no clock, no unseeded randomness, bounded
`[0.0, 1.0]` scores, and `verify` never raises.

## What the lane author must provide

### 1. Challenge formulation

Choose the exact boolean task family. Acceptable shapes include:

- satisfiable SAT with binary scoring
- weighted Max-SAT with partial credit
- bounded boolean CSP
- another verifier-friendly boolean challenge with explicit schemas

State the choice in `DESIGN.md` before implementing the generator.

### 2. Corpus or generator

Provide either concrete challenge rows, a deterministic generator, or both.
Every generated challenge must be reproducible from `(seed, tier)` and must
produce:

- `PublicProblem`: the miner-visible challenge
- `HiddenMetadata`: publisher-only verifier material, if needed

For binary SAT, keep v1 to known-satisfiable instances unless you also
provide deterministic UNSAT proof verification.

### 3. Submission schema

Define the exact shape of `Submission.answer`. Examples:

```json
{
  "assignment": {
    "1": true,
    "2": false
  }
}
```

or, if the formulation supports it:

```json
{
  "answer_type": "assignment",
  "assignment": {
    "1": true
  }
}
```

Free-form explanations may be collected in the Hermes trace, but `verify`
must not trust prose or hidden reasoning.

### 4. Deterministic verifier

`verify` must parse `Submission.answer`, evaluate it against the challenge,
and return a `VerifierResult`.

Requirements:

- no LLM calls
- no network
- no subprocess
- no file I/O
- no trust in miner explanation text
- malformed, incomplete, timeout, or unverifiable answers return a rejected
  `VerifierResult`, not an exception

### 5. Scoring policy

Pick and document the scoring rule in `DESIGN.md`.

For binary SAT:

```text
correct assignment -> weighted_score = 1.0
incorrect, malformed, incomplete, timeout, unverifiable -> weighted_score = 0.0
```

For Max-SAT or another partial-credit task, define:

- raw metric
- normalization base
- difficulty tier behavior
- clamp behavior
- rejection reasons

If you normalize against an optimum, explain how the optimum is known or
bounded for every challenge row.

The full Hermes trace is collected as a sidecar for provenance, debugging,
and dataset value. It should affect score only if `DESIGN.md` explicitly
defines a hard-to-fake trace requirement and the verifier can enforce it
deterministically.

### 6. Fixtures and tests

Ship fixtures under:

```text
fixtures/golden/
fixtures/adversarial/
```

Golden fixtures prove correct scoring. Adversarial fixtures prove bad
submissions fail cleanly. Cover at least:

- correct answer
- wrong answer
- malformed answer
- incomplete assignment
- wrong types
- oversized payload
- irrelevant prose or extra fields, if your schema accepts metadata

Each fixture should include `name`, `seed`, `tier`, `submission`, and either
`expected_weighted_score` or `expected_rejection_reason`.

## Local development

```bash
PYTHONPATH=src pytest tests/lanes/test_contract.py -k synthetic_boolean_v1 -v
```

Manual generator smoke:

```bash
PYTHONPATH=src python -c "
from cathedral.lanes.synthetic_boolean_v1 import SyntheticBooleanV1
from cathedral.lanes.contract import GenerateCtx
public, hidden = SyntheticBooleanV1().generate(GenerateCtx(seed=42, tier=1, issued_at_iso='2026-05-18T00:00:00.000Z'))
print(public.model_dump_json(indent=2))
"
```

## Merge checklist

1. `DESIGN.md` states the exact boolean formulation and scoring policy.
2. `generate`, `verify`, and `score` are implemented.
3. Golden and adversarial fixtures exist.
4. `PYTHONPATH=src pytest tests/lanes/test_contract.py -k synthetic_boolean_v1 -v` is green.
5. Lane is registered in `src/cathedral/lanes/registry.py`.
6. The PR touches only this lane unless platform maintainers explicitly ask
   for contract changes.

## Out of scope

- publisher wiring
- signing
- validator pull loop
- weight allocation
- Linux jail
- chain operations
- Hermes transport

If the lane needs any of those, stop and call it out in the PR. The lane
author owns the corpus and scoring mechanism, not Cathedral rails.
