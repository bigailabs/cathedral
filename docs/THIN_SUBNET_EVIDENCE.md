# Cathedral Thin Subnet Evidence

> **Historical checkpoint — superseded for SN39 launch decisions.** This
> document records the repository and chain state inspected on 2026-07-19.
> Its statements about missing broadcasts and TDX evidence were true at that
> checkpoint, not claims about the current launch candidate. Use
> [`SN39_MAINNET_RELEASE_20260724.md`](SN39_MAINNET_RELEASE_20260724.md) and its
> root-signed public release record for the current gate.

Date: 2026-07-19  
Candidate branch: `codex/production-ready-subnet`  
Base: `origin/main` at `f9843df`

This record separates locally proven behavior from launch gates. It is not a
claim that the subnet is uncheatable or already broadcasting weights.

Review scope is `cathedral_thin/`, `tests/thin/`, `deploy/thin/`, the thin-subnet
and VerifyML documents, score-policy examples, packaging, and the added CI job. The review
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

Result after the VerifyML/Fable remediation: **106 passed** in 1.91 seconds. The
only output was two upstream
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
VerifyML coverage adds validator-signed, miner-targeted, epoch-bound requests;
separate model/image/runner/policy/verifier pins; TDX report-data reconstruction;
cross-epoch fresh-execution rejection; deterministic partial bundle admission;
network-bound O(1) checkpoints; serialized checkpoint transactions; verifier
provenance matching; bounded aggregation; and the score-body-to-validator-class
handoff.
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

`pip wheel . --no-deps --no-build-isolation` produced an 811,574-byte universal
wheel after the reviewed remediation:

```text
cathedral_scaffold-4.0.0rc4-py3-none-any.whl
sha256 737aaabe675bd99f057eb50fd084ce06c14976961d249297b49487a42fb8692e
```

The wheel was installed into an isolated target outside the checkout. Import
resolved from that target; the packaged registration preflight, score-report,
source-owner contributor, and VerifyML tools were present; and the packaged
composed E2E returned `ok=true`, `owner_hosted_services=0`,
`owner_registration_verified=true`, and `delegate_registered=true`.
The installed VerifyML parser exposed `authorize`, `issue`, `run-local`,
`verify`, `bundle`, `verify-bundle`, and `score-body`.

## Real open-model receipt proof on Polaris

One explicitly authorized Polaris `CPU Small (4 vCPU / 16 GB)` rental ran
`smollm2:135m-instruct-q8_0` through a container pinned to
`sha256:6345fbc18bd73a1e16404be681dbc6fd291a027cab43ed541abe78c4c81051b0`.
The 144,811,072-byte model blob was independently hashed on the rental as
`sha256:40f7094960b6ede829145d102ca79451b364b27d9d8694d4406e002024cff357`;
the runner binary hash was
`sha256:2318056c74f47b813860e7ef80ab2e67aca7e3935a5d97c0a6115575cff66480`.

The model answered a public prompt in 1.074848034 seconds with 48 input and 28
output tokens. The `cathedral/serge_sat_test` hotkey signed the resulting input,
parameter, output, model, and runtime commitments. Independent CLI
verification reproduced every reveal commitment and accepted receipt
`sha256:cc11d04839a5e4d5563bd306953341e2b6fc90a9ef70d4656421a1bb18b62f72`
at Finney block 8,653,533.

This is deliberately recorded as `attestation_verified=false`: the cheapest
ordinary CPU rental proves the real-model and portable-receipt plumbing, not a
genuine TDX execution. The receipt therefore cannot produce production
`verified_work_units`. The final schema also records
`validator_request_authorized=false`; the historical run predates the added
validator-signed demand gate and cannot be upgraded into production credit.
Exact prompt, parameters, output, signed receipt, model hashes, runtime hashes,
latency/token metadata, rental ID, and proof scope are
under [`docs/evidence/`](evidence/verifyml-polaris-smollm2-2026-07-19-run.json).

The rental deployment was
`c990822f-0df3-43fe-9d55-dbdb47f800a1`. It was terminated immediately after
the hashes were captured; the API then reported `status=stopped`,
`provider_status=stopped`, and zero active rentals. Account balance moved from
$181.50 to $181.45, an observed $0.05 test cost.

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
75 failed, 1051 passed, 5410 warnings in 129.28s
```

Clean `origin/main` full suite in a detached worktree:

```text
75 failed, 945 passed, 5408 warnings in 121.80s
```

The candidate and clean base have **exactly the same 75 failing node IDs**:
candidate-only 0, base-only 0. The failures come from absent external audit
corpora, forbidden local socket, process, or renderer capabilities, and legacy
publisher global state. The candidate adds 106 passing thin-subnet tests and
two upstream Bittensor warnings without adding a whole-repository failure.

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

VerifyML then received four fresh read-only Fable passes. The first exposed
three high-priority receipt/bundle defects; the second rejected an incomplete
fix because one validator authorization could still mint a fresh receipt in a
later epoch. The final protocol binds each authorization to one source epoch at
every signature and attestation layer. The third pass returned **REMEDIATION
ACCEPTED**; a fourth narrow pass reviewed the last checkpoint-lock hardening and
returned **FOLLOW-UP ACCEPTED — no actionable defects found**. Fable could not
run pytest under its read-only plan mode, so its static review is independent
of the locally executed test evidence. The full finding/remediation record is
[`VERIFYML_FABLE_REVIEW.md`](VERIFYML_FABLE_REVIEW.md).

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
- The VerifyML receipt schema, pinned-verifier adapter, bundle replay gates,
  score-body handoff, and real open-model unattested run are complete. A
  genuine TDX quote whose report data follows this new schema has not yet been
  verified independently; until that happens, no VerifyML receipt should earn
  production `verified_work_units`.
Until the remaining testnet gates are closed, this is an independently reviewed
release candidate, not a
mainnet-production attestation.
