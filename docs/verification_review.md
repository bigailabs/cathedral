# Verification Feature Review

This document summarizes the end-to-end verification flow (veritas → basilica-2) and the changes
implemented to enforce strict binary outcomes, propagate failure reasons, and harden proofs.

## Scope
- veritas: executor + validator binaries, PoW proofs (CPU/storage/bandwidth), and verification logic.
- basilica-2: validator orchestration, parsing, persistence, and node profile publication.

## Strict Binary Outcome
**Goal:** Any failed check yields overall failure; no partial success or scoring.

Implemented changes:
- `ValidatorBinaryOutput.success` is **strict**: `success && failure_reasons.is_empty()`.
- Validation score is binary: `1.0` for success, `0.0` for failure.
- Failure reasons are captured and propagated to:
  - DB `verification_logs.details.failure_reasons`
  - Node profile CR `status.failureReasons`
  - Error message surface (joined reasons)

## Failure Reasons
Failure reasons are now carried end-to-end:
- veritas emits `failure_reasons` in JSON.
- basilica-2 parses and persists them.
- Node profile CR includes them for external visibility.

## Bandwidth PoW Fix (Network-Proving)
**Issue:** Original bandwidth PoW hashed a deterministic RNG stream locally on executor/validator,
which did **not** require network transfer.

**Fix:** Introduced streaming download path so the validator must receive actual bytes over the
network to compute the proof:
- Executor exposes `POST /bandwidth_stream` (download-only).
- Executor streams deterministic bytes (seed/nonce) to the validator.
- Validator hashes the received bytes and measures duration to compute proof.

This makes the proof dependent on actual network throughput between validator and executor.

## Remaining Assumptions / Risks
1) **Upload mode** still measures *client-side* send time. It proves sender→receiver bandwidth,
   but can be sensitive to TCP buffering; consider server-side timing if needed.
2) **Failure reasons on pre-validations** are still limited to high-level reasons unless explicitly
   added for Docker/NAT/storage.
3) **Node profile on failure** is only updated if a successful node_result exists; failures won’t
   publish CRs unless explicitly added.

## References
- `veritas` executor: `crates/executor-binary/src/server/handlers.rs`
- `veritas` validator: `crates/validator-binary/src/executor_client.rs`, `executor_manager.rs`
- `basilica-2` validator: `crates/basilica-validator/src/miner_prover/validation_binary.rs`

