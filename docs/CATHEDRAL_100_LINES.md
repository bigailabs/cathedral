# Cathedral in 100 lines

`cathedral_loop.py` is a reference kernel for Cathedral's complete model loop.
The implementation is exactly 100 physical lines. It composes security
boundaries that remain separate production systems.

The kernel does five things:

1. It executes the same witness against pinned vulnerable and patched
   executables. The vulnerable process must terminate from a signal and the
   patched process must exit successfully.
2. It admits only active, revision-pinned teacher entries with a pinned licence
   digest.
3. It binds the verified corpus, teacher, recipe, and trainer digest into the
   report data checked by a pinned attestation verifier.
4. It publishes a checkpoint only when the same sealed evaluator scores the
   candidate strictly above the current model.
5. It derives a signed integer weight vector from explicit epoch entitlements.
   The fixed burn receives 10%, all missing proof, and integer remainder.

Every accepted transition is an Ed25519-signed receipt in one hash chain.
Actor registry, licence, work, corpus, compute, training, evaluation,
checkpoint, and weight receipts can be replayed with `Cathedral.verify()`.
Future work receipts carry the checkpoint digest that produced the solution.

## Trusted seams

The 100-line kernel does not replace these components:

- `quote_verifier` must be the production, digest-pinned TDX or confidential
  GPU verifier. The test verifier is synthetic.
- `trainer` must run Cathedral's recipe against the receipt-addressed corpus.
  The test trainer returns fixture bytes.
- `evaluator` must own the sealed task set and execute both checkpoints under
  identical conditions. The test evaluator uses fixed booleans.
- `work()` executes binaries directly. Production callers must place those
  binaries in the existing disposable exploit sandbox with no production
  credentials or network access.
- `weights()` returns a signed decision receipt. It does not call
  `set_weights`.

Kimi K3 is represented symbolically in the tests. A production registry entry
must use the exact reviewed model identifier, immutable revision, and licence
digest. Missing or revoked licence evidence fails closed.

## Evidence status

| Gate | Status |
|---|---|
| 100 physical lines | PASS |
| Focused local replay and failure-path tests | PASS |
| Real exploit sandbox integration | NOT PROVEN |
| Real Kimi K3 licence clearance or teacher call | NOT PROVEN |
| Real TDX or confidential GPU quote | NOT PROVEN |
| Real training run and published checkpoint | NOT PROVEN |
| On-chain weight submission | NOT PROVEN |

Run the focused checks:

```bash
python3 -m pytest -q tests/thin/test_cathedral_loop.py
ruff check cathedral_loop.py tests/thin/test_cathedral_loop.py
```
