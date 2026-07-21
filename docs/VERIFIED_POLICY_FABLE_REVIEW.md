# Verified Policy Work: independent Fable review

Date: 2026-07-19

Artifact reviewed:

- `cathedral_thin/verified_policy.py`
- `tests/thin/test_verified_policy.py`
- the existing score-class consumer used by the protocol

The review was performed in fresh, read-only Fable sessions. Fable was asked to
inspect the actual protocol and tests, cite concrete code paths, try to disprove
every finding, and end with an explicit verdict. It did not edit the tree.

## Initial verdict: REQUEST CHANGES

Fable found three concrete paths:

1. **High: rare-default reward capture.** A miner could use the rare label as
   its default, cite one valid rare example, and receive full rare-recall and
   evidence-faithfulness measurements even while balanced accuracy was poor.
   The existing floor only zeroed compactness.
2. **Medium: hidden-suite reuse.** The hidden suite commitment covered salt and
   cases but not the individualized task identity or task nonce, leaving an
   avoidable reuse-and-leak path.
3. **Low: score-report repackaging.** A verified evaluation did not carry enough
   task provenance for the report builder to reject a different network,
   netuid, source epoch, or block window.

Fable identified the most likely production failure as miners converging on a
rare-default policy that passed every signature and replay check while capturing
auxiliary class budgets without doing useful policy work.

## Remediation

The implementation now:

- gates rare recall, cited-example faithfulness, and compactness to zero unless
  both validator-signed fidelity and rare-case floors pass;
- embeds a task-nonce digest in the hidden-suite bytes;
- binds the suite commitment to network, netuid, source epoch, task class,
  validator, miner, nonce, issue time, and block window;
- carries network, netuid, source epoch, and task block bounds inside the signed
  evaluation;
- rejects score reports whose evaluation provenance does not match the report
  network, netuid, epoch, generation time, or block window;
- distinguishes boolean and integer rule values during replay;
- evaluates the reference miner on a hidden combination not present in the
  public examples;
- adds regression tests for the rare-default strategy, task-nonce suite reuse,
  and report repackaging.

## Follow-up verdict: ACCEPT

In a second fresh pass, Fable reconstructed each prior strategy and confirmed:

- the rare-default policy now receives zero rare, evidence, and compactness
  measurements because balanced accuracy misses the signed floor;
- suite bytes have both an embedded task-nonce digest and a commitment over the
  full task binding;
- cross-network, cross-netuid, cross-epoch, out-of-window, and pre-evaluation
  score reports fail closed;
- evaluation replay rejects receipt asymmetry and cross-task artifact/receipt
  reuse;
- invalid or missing support examples can only reduce faithfulness.

Fable reported no new blocking implementation issue and ended with `ACCEPT`.

## Evidence after remediation

```text
PYTHONPATH=. /Users/dreamboat/Documents/PROJECTS/cathedralsubnet/.venv/bin/python -m pytest -q tests/thin
117 passed, 2 existing Bittensor/Pydantic deprecation warnings

ruff check cathedral_thin tests/thin
All checks passed

ruff format --check cathedral_thin tests/thin
25 files already formatted
```

This review accepts the implementation in scope. It is not evidence of an
SN39 chain write, a real TDX quote for this artifact, production task-pack
quality, or immunity to copied digital content.
