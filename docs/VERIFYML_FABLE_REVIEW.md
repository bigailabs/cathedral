# VerifyML Independent Fable Review

Date: 2026-07-19
Scope: `cathedral_thin/ml_receipts.py`, `cathedral_thin/verifyml_cli.py`,
`cathedral_thin/score_classes.py`, `tests/thin/test_ml_receipts.py`,
`docs/VERIFYML.md`, the example class policy, and the evidence ledger.

Fable reviewed the implementation in four fresh, non-persistent, read-only
sessions. It had no edit, wallet, deployment, chain-write, or internet role.
Plan-mode restrictions prevented Fable from executing pytest; the test results
in the evidence ledger were run separately in the project environment.

## Review sequence

1. The first review returned a conditional release-candidate verdict. It found
   three high-priority defects: request IDs were only deduplicated inside one
   bundle, the CLI did not persist bundle checkpoints, and one invalid receipt
   could invalidate otherwise valid work. It also identified the absence of a
   validator-signed demand signal, conflated image/runner allowlists, an
   unpinned-verifier footgun, evidence-file edge cases, aggregation bounds,
   verifier executable time-of-check/time-of-use, and asserted verifier
   provenance.
2. The second review confirmed nine original findings closed but rejected the
   remediation because one validator authorization could still be used for a
   fresh execution in a later epoch.
3. The third fresh review traced the final `source_epoch` binding through the
   request ID, validator authorization signature, receipt/miner signature, TDX
   report data, bundle ID, and bundle admission. It concluded that a fresh
   later-epoch execution cannot reuse the authorization and returned
   **REMEDIATION ACCEPTED**. It retained two low operator-hardening notes about
   concurrent checkpoint commands and path-validation ordering.
4. After those two notes were fixed with a sidecar `flock`, early path
   validation, atomic replacement, and directory fsync, a final fresh
   follow-up checked the complete lock boundary and returned
   **FOLLOW-UP ACCEPTED — no actionable defects found**.

## Resulting controls

- Validator-signed requests bind the miner, nonce, epoch, model, image, runner,
  input, parameters, time, and block window.
- A request authorization is valid for exactly one source epoch. A new receipt
  in another epoch fails even when it contains a genuine fresh execution.
- The O(1) checkpoint binds network, netuid, epoch, bundle ID, and generation
  boundary; no owner-operated request database is required.
- Receipt-level failures are isolated deterministically. Structural failure or
  a bundle with zero admitted receipts still fails closed.
- Model, image, runner, attestation policy, verifier executable, and
  request-authorizing validator are separate validator-local pins.
- The score body can only name the verifier digest observed on every admitted
  receipt; the source still emits facts, never final weights.
- Concurrent local checkpoint commands serialize across checkpoint read,
  verification, score output, and atomic checkpoint persistence.

## Residual risks and non-claims

The review does not turn TEE evidence into mathematical proof. Hardware/vendor
attestation, the correctness of each validator's pinned verifier and measured
runner, validator policy quality, separately funded coldkeys, metadata leakage,
and validator-stake capture remain explicit assumptions. No genuine TDX quote
for this schema, admitted SN39 validator, on-chain weight broadcast, or live
third-party subnet-owner contribution was represented as completed.
