# Cathedral Thin Subnet Evidence

Date: 2026-07-19  
Candidate branch: `codex/production-ready-subnet`  
Base: `origin/main` at `f9843df`

This record separates locally proven behavior from launch gates. It is not a
claim that the subnet is uncheatable or already broadcasting weights.

Review scope is `cathedral_thin/`, `tests/thin/`, `deploy/thin/`, the thin-subnet
documents and score-policy example, packaging, and the added CI job. The review
question was: can an invalid, stale, copied, replayed, identity-swapped,
Sybil-duplicated, or currently offline miner receive weight; can an external
score source bypass validator-local class policy, provenance, or replay gates;
or can a failed chain submission be misdirected on retry? Legacy publisher and
Arena behavior are out of scope except for the clean-baseline regression
comparison below.

## Focused production path

Command:

```bash
python -m pytest -q tests/thin
```

Result: **88 passed** in 0.73 seconds. The only output was two upstream
Bittensor/Pydantic deprecation warnings from reading `Synapse.body_hash`.

The suite covers deterministic HMAC challenge generation, strict payload
bounds, complete witness verification, copied/replayed/identity-swapped
answers, response body-hash binding, validator-observed timing, current-round
eligibility, coldkey Sybil collapse, miner permit/rate/round controls, semantic
retry caching, concurrency timing, state permissions/locking/corruption,
config migration, pending-vector recovery, UID reassignment cancellation,
ambiguous retry preservation across pre-submission RPC and registration
failures, continuous validator recovery from raw SDK exceptions, miner permit
snapshot retention across transient RPC failures, chain constraint processing,
Bittensor response shapes, registration
preflight, and the multi-miner E2E. Score-class coverage includes canonical
Ed25519 reports, wrong-key/tamper/network/time/block rejection, strict JSON,
validator-selected metric versus asserted-score modes, required reasons and
evidence kinds, coldkey collapse within each class, exact budget composition,
mirror selection/equivocation, rollback and broken-chain checkpoints,
source-only validation with zero miner queries, immutable report publication,
decision-record integrity, and vector/provenance binding across retry.
Owner-registration coverage verifies source-owner SR25519 signatures, live
source ownership, target delegate hotkey/coldkey registration, exact
source/target/class binding, delegated report keys, time/block expiry,
ownership transfer, persistent registration checkpoints, rollback, broken
rotation links, and same-sequence mirror equivocation. A full validator-runner
test proves that the registered contributor can supply a class while making
zero miner queries and never receiving a weight-setting key.
Registered report URLs must exactly equal validator-pinned HTTPS mirrors,
preventing a contributor-selected fetch/SSRF target. Pending-vector tests bind
registration IDs into the vector digest and prove that owner transfer,
delegate deregistration, or registration/key rotation cancels a retry before
any weight call.

Formatting and import checks:

```text
ruff check cathedral_thin tests/thin        All checks passed
ruff format --check cathedral_thin tests/thin  19 files already formatted
python -m compileall -q cathedral_thin      passed
miner, validator, preflight, report, contributor --help  passed
```

## Multi-miner local E2E

Command:

```bash
python -m cathedral_thin.e2e --pretty
```

Result summary:

```json
{
  "ok": true,
  "owner_hosted_services": 0,
  "miners": 8,
  "verified": ["honest-a", "honest-a2", "honest-b"],
  "attacks": {
    "copier": "witness_failed",
    "replayer": "challenge_mismatch",
    "swapper": "miner_identity_mismatch",
    "invalid": "assignment bitset length mismatch",
    "offline": "axon_unavailable"
  },
  "sybil_no_multiplier": true,
  "historical_offline_gated": true,
  "miner_timing_ignored": true,
  "score_classes": {
    "allocations": {"local_sat": 0.6, "confidential_compute": 0.4},
    "confidential_checkpoint": 7,
    "owner_registration_verified": true,
    "delegate_registered": true,
    "owner_registration_sequence": 0,
    "validator_assignment": "verified_work_units",
    "decision_record_written": true
  },
  "weight_sum": 1.0,
  "retry_identical_after_restart": true,
  "secret_stable_after_restart": true,
  "confirmed_after_retry": true
}
```

This uses real generated DIMACS formulas, the reference solver, wire-shaped
responses, deterministic verification/scoring, and a real Ed25519-signed
Cathedral Confidential-shaped report with receipt IDs, reason requirements,
fixed 60/40 composition, source checkpoint, and immutable decision record. The
source-subnet owner signs a bounded delegation, the target delegate
hotkey/coldkey pair is checked, the delegated report key is materialized, and
the decision record retains the owner, delegate, and registration ID. A
fake-chain failure is followed by restart/retry of the identical
decision-bound vector. It uses no API, database, queue, object store, owner
solver, registry service, or owner score proxy.

## Built artifact

`pip wheel . --no-deps --no-build-isolation` produced a 794,041-byte universal
wheel after the reviewed remediation:

```text
cathedral_scaffold-4.0.0rc4-py3-none-any.whl
sha256 e503e4c6d588bb1f8514e31bd1fbaaead9dd4cbfd92481441551c02e4ebe1b2f
```

The wheel was installed into an isolated target outside the checkout. Import
resolved from that target, the packaged registration preflight, score-report,
and source-owner contributor tools were present, and the packaged composed E2E
returned `ok=true`, `owner_hosted_services=0`,
`owner_registration_verified=true`, and `delegate_registered=true`.

## Current Bittensor compatibility

A read-only public-chain probe used Bittensor SDK 10.5.0 against Finney SN39.
At block 8,646,669, the candidate parser accepted a live metagraph with 256
UIDs, 256 unique hotkeys, present coldkeys, and current Axon objects. The probe
used no wallet transaction and made no chain write.

Local SDK inspection also confirmed that the installed `Subtensor.set_weights`
accepts the commit-reveal and MEV compatibility arguments used by the adapter.
The installed SDK's `Subtensor.subnet(netuid, block=...)` exposes the current
owner coldkey used by the contributor gate, while the target metagraph exposes
the registered hotkey/coldkey pair. Fake-chain tests cover owner lookup plus
confirmed, failed, ambiguous, and retry responses.

## Whole-repository baseline comparison

Candidate full suite:

```text
79 failed, 1029 passed, 5410 warnings in 79.50s
```

Clean `origin/main` full suite in a detached worktree:

```text
80 failed, 940 passed, 5409 warnings in 81.03s
```

The candidate has no branch-only failing node. Its 79 failures are a strict
subset of the clean base's 80 failures. The base-only failure is the
order-sensitive legacy publisher test
`test_solution_manifest_v2_submit_bitset_rejects_auth_failures_without_rows`,
which passes under the candidate's expanded collection order. The remaining
failures come from absent external audit corpora, forbidden local socket,
process, or renderer capabilities, and legacy publisher global state. The
candidate adds 88 focused passing tests and introduces no full-suite failure.

## Live SN39 Finney dry-run

On 2026-07-19, the configured `cathedral/default` validator identity was
checked read-only against mainnet SN39 with Bittensor SDK 10.5.0. The preflight
returned hotkey `5FF6FtDUhn7XdPYmEdH5XjLAmLfmwLTCNVBgcrj3A4sstwaw`,
`registered=false`, `uid=null`, and `validator_permit=false`.

One live validator tick then ran with a temporary state directory, small valid
SAT parameters, and no `--broadcast` flag. The process printed
`netuid=39 broadcast=False` and held with `configured class local_sat has no
positive scores`. No decision vector or weight submission was produced. This
proves live Finney connectivity, real metagraph loading, Dendrite/Axon request
execution, fail-closed admission behavior, and clean client-session shutdown.
It does **not** prove scoring by an admitted validator because the configured
hotkey is not registered on SN39.

## Independent Fable review

The user authorized a scoped external review. Fable ran in fresh,
non-persistent, read-only sessions with no edit, wallet, chain-write, deployment,
or internet capability. The first pass found no blocking issue and retained
four actionable implementation or operations findings: transient miner permit
refresh could terminate the process; continuous validator ticks could terminate
on raw SDK exceptions; two pre-submission retry paths could clear an existing
ambiguous outcome; and the runbook omitted clock-synchronization requirements.

All four were corrected and covered by regression tests. A second fresh Fable
session reviewed only the remediation files and returned: **remediation
accepted; no blocking findings remain**. The review's remaining observations
are fail-closed or operational: monitor stale permit snapshots, keep the
documented 30-second skew tolerance aligned with code, consider narrowing the
small in-memory/disk mutation window, and replace string-prefix exception
classification with typed exceptions if that path grows. The full record is
[`THIN_SUBNET_FABLE_REVIEW.md`](THIN_SUBNET_FABLE_REVIEW.md).

## Launch gates not represented as completed

- No subnet create/register/start transaction or weight broadcast was made.
  A mainnet SN39 dry-run was completed, but the configured validator hotkey is
  unregistered and has no permit. Registration and later broadcast are explicit
  operator gates because they can spend or lock funds and alter chain state.
- The source-owner registration path was exercised with real Bittensor
  SR25519 keypairs and simulated chain snapshots, but no third-party testnet
  owner has yet registered a delegate or published a live artifact. That live
  multi-operator exercise remains part of the testnet gate.
- The sandbox does not permit a real local Axon socket bind. Protocol and
  transport-shaped E2E paths are proven; an operator should still exercise the
  two-process Axon/Dendrite flow on testnet before mainnet.
- The generic Cathedral Confidential class contract, signer, validator
  consumer, and realistic signed E2E are complete. The inspected
  `cathedralconfidential` implementation still emits its older normalized HMAC
  ingest stream; it must export `verified_work_units`, exact assurance-receipt
  IDs and digests, explicit zeros, and the new Ed25519 class report before this
  class is enabled by a real validator.
Until the remaining testnet gates are closed, this is an independently reviewed
release candidate, not a
mainnet-production attestation.
