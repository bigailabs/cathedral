# Audit Arena v0

## Scope

- Live scanner path: signed miner package -> deterministic replay -> private trace -> signed `audit_replay_v1` eval row -> bounded signed-weight bonus.
- Offline/Subtensor shadow path: DIMACS witness -> CNF check -> SAT-bound decode -> deterministic replay -> private trace.
- Source files:
  - `game/arena/scanner.py`
  - `game/arena/audit_scanner_smoke.py`
  - `game/arena/tests/test_publisher_audit_scanner.py`
  - `scaffold/publisher/app.py` (`/v1/audit-scanner/*`)
  - `scaffold/publisher/weights.py` (`audit_replay` bonus term)
  - `scaffold/lanes/audit_arena.py`
  - `scaffold/lanes/subtensor_replay.py`
  - `audit_arena_verify.py`

## Live Scanner Payment Path

- Endpoints:
  - `GET /v1/audit-scanner/status`
  - `GET /v1/audit-scanner/catalog`
  - `GET /v1/audit-scanner/task?index=0`
  - `POST /v1/audit-scanner/replay`
  - `POST /v1/audit-scanner/submit`
- Signing:
  - Uses the same Cathedral hotkey header contract as SAT submits.
  - `card_id`: `cathedral_audit_scanner_v1`
  - `challenge_id`: `task_id`
  - `dimacs_solution_sha256`: `sha256(canonical_submission_artifact)`
- Payment:
  - Accepted `submit` calls emit signed `eval_runs` rows with `task_type=audit_replay_v1`.
  - `weights.py` adds a capped normalized audit replay bonus to the existing signed vector.
  - Existing SAT scoring remains unchanged when no accepted audit replay rows exist.
- Operator knobs:
  - `CATHEDRAL_AUDIT_SCANNER_ENABLED=0` disables the scanner surface.
  - `CATHEDRAL_AUDIT_SCANNER_PAYMENT_WEIGHTS_ENABLED=0` keeps replay/ledger on but disables payment rows.
  - `CATHEDRAL_AUDIT_SCANNER_REQUIRE_ATTESTATION=1` records accepted rows but excludes them from weights until `/v1/attest` upgrades the row.
  - `CATHEDRAL_AUDIT_REPLAY_BONUS_MULT=0.25` controls the default audit replay bonus term.

## Miner Submission Format

- Wire schema: `cathedral.audit_submission.v1`
- Required fields:
  - `task_id`: exact audit task id.
  - `miner_hotkey`: miner identity for attribution.
  - `dimacs_solution`: standard DIMACS solver output, for example:

```text
s SATISFIABLE
v 1 -2 3 0
```

- Optional fields:
  - `agent_trace`: private miner-side notes/tool trace. It is kept private by default.

```json
{
  "schema_version": "cathedral.audit_submission.v1",
  "task_id": "audit-production-bit-projection",
  "miner_hotkey": "5AuditMiner",
  "dimacs_solution": "s SATISFIABLE\nv 1 2 0\n",
  "agent_trace": {}
}
```

- Private traces store `dimacs_solution_sha256`, not the raw DIMACS witness.

## Accepted Example

- CNF hash matches the pinned task package.
- DIMACS solution satisfies the CNF.
- Decode map uses SAT-bound bit projections and declares `required_fields`.
- Replay returns `reproduced: true`.
- Verdict:
  - `accepted: true`
  - `stage: accepted`
  - `label: reproduced_witness`

## Rejected Examples

- `task_id_mismatch`: submission is for another audit task.
- `cnf_sha256_required`: task lacks a pinned CNF hash.
- `cnf_sha256_mismatch`: CNF text does not match the task package.
- `solution_unsatisfied`: DIMACS assignment fails the CNF.
- `static_witness_decode_requires_allow_static_witness`: static witnesses are not allowed outside explicit smoke/corpus tasks.
- `decode_map_missing_witness_or_fields`: labels such as `decode_inputs` cannot replay by themselves.
- `sparse_decode_requires_required_fields`: production decode maps must declare replay-critical fields.
- `replay_did_not_reproduce` or replay-specific reason: SAT is valid but deterministic replay does not reproduce the claim.
- `replay_reproduced_must_be_boolean`: replay output must use a real boolean, not a truthy string.

## Replay Provenance

- Every private trace binds:
  - `cnf_sha256`
  - `decode_map_sha256`
  - `audit_package_sha256`
  - `target.commit`
  - `replay_adapter_id`
  - `replay_adapter_sha256`
  - `replay_code_sha256`

- The accepted bit is derived from deterministic replay, not from miner text.

## Subtensor Clone Shadow Replay

- Wire schema: `cathedral.subtensor_replay.v1`
- Scope:
  - validates a task-pinned replay package hash, target commit, runtime hash, clone-state hash, script hash, witness binding, and invariant checks.
  - accepts only when the SAT-decoded witness matches the replay package and an injected observation violates the declared invariant.
  - rejects missing observations instead of starting a node or running a script implicitly.
- Task requirement:
  - `task.source.subtensor_replay_package_sha256` must equal the canonical replay package hash.
- Required package fields:
  - `target_commit`
  - `runtime_sha256`
  - `clone_state_sha256`
  - `script_sha256`
  - `script_steps`
  - `invariant_id`
  - `expected_witness`
  - `checks`, with at least one required invariant check
- Additional rejection reasons:
  - `subtensor_replay_package_unpinned`: task did not pin the replay package hash.
  - `subtensor_replay_package_sha256_mismatch`: task pin does not match the supplied package.
  - `invariant_required_check_missing`: package has checks, but none are required.
  - `invariant_number_must_be_finite`: observed invariant input is not a finite number.
- This remains a shadow/offline seam. It does not yet clone real Subtensor state, execute extrinsics, attest runtime output, score live miners, or pay emissions.

## Coinbase Conservation SAT Oracle

- Canonical invariant id:
  - `subtensor.run_coinbase.childkey_conservation.v1`
- Source target:
  - `pallets/subtensor/src/coinbase/run_coinbase.rs:1027-1039`
- Files:
  - `game/arena/coinbase_encoder_agent.py`
  - `scaffold/lanes/coinbase_oracle.py`
  - `scaffold/lanes/clone_replay.py`
  - `scaffold/lanes/verifiable_sat_pipeline.py`
  - `scaffold/publisher/solver_artifacts.py`
  - `coinbase_oracle_verify.py`
- What it models:
  - `parent_emission = floor(validating_emission * parent_factor / u64::MAX)`
  - `burn_take = floor(parent_emission * ck_burn_rate / u64::MAX)`
  - `child_take = floor(parent_emission * child_take_rate / u16::MAX)`
  - `parent_left = parent_emission.saturating_sub(burn_take).saturating_sub(child_take)`
  - violation iff `burn_take + child_take + parent_left > parent_emission`
  - `child_take_rate <= 11796 / 65535`, matching the runtime default `SubtensorInitialMaxChildKeyTake` cap.
  - CI uses width-scaled denominators for small bounded proofs; launch-width challenges use u64 parent/CKBurn rates and capped u16 child-take rates.
- Oracle:
  - `CKBurn>0` side: SAT. A decoded assignment must replay as a real conservation break.
  - `CKBurn=0` side: UNSAT. A solver must return a DRAT proof and `drat-trim` must verify it.
- Anti-gaming gates:
  - canonical invariant required
  - clause/source map required
  - decode map required
  - SAT assignment must satisfy the CNF and replay against the real arithmetic
  - challenges must be signed, server-issued, assigned to the submitting hotkey, and present in `lane_challenges`; body-minted challenge artifacts are rejected
  - agent image digests must be allowlisted by `CATHEDRAL_VERIFIABLE_SAT_AGENT_IMAGE_DIGESTS`
  - SAT payment requires an operator-configured clone replay receipt when `CATHEDRAL_VERIFIABLE_SAT_REQUIRE_SYSTEM_REPLAY=1` (default)
  - UNSAT proof must pass `drat-trim`; UNSAT is accepted but not paid by default unless `CATHEDRAL_VERIFIABLE_SAT_REWARD_UNSAT=1`
  - TDX quote verification is required by default via `CATHEDRAL_VERIFIABLE_SAT_REQUIRE_ATTESTATION=1`; report-data-only checks require explicit `CATHEDRAL_VERIFIABLE_SAT_ALLOW_REPORT_DATA_ONLY=1` and are shadow/CI only
  - accepted payment rows are marked attested only after a real TDX quote verifies; report-data-only shadow rows are not scored
  - real TDX verification must also bind the measured agent image to the allowlisted digest (`CATHEDRAL_VERIFIABLE_SAT_REQUIRE_TDX_MEASUREMENT=1` by default)
  - SAT payment rows dedupe globally by CNF hash and outcome; this is one bounty per canonical bug/challenge, not one payment per alternate witness
  - `agent_id` is allowlisted by `CATHEDRAL_VERIFIABLE_SAT_AGENT_IDS` (`hermes-coinbase-encoder-v1` by default); miners cannot mint fresh payable artifacts by varying agent ids
  - payment requires `width >= CATHEDRAL_VERIFIABLE_SAT_MIN_PAYMENT_WIDTH` (`64` by default); small CI widths are verification smoke only
- Challenge issuance:
  - `GET /v1/verifiable-sat/coinbase/challenge?ckb_enabled=true&width=64&agent_image_digest=sha256:<digest>`
  - requires `X-Cathedral-Hotkey`, `X-Cathedral-Signature`, and `X-Cathedral-Submitted-At`
  - the issue signature signs `card_id=cathedral_verifiable_sat_v1`, `challenge_id=issue:sha256({ckb_enabled,width,agent_image_digest,agent_id})`, and `dimacs_solution_sha256` equal to the same issue hash
  - returns a server-derived miner-bound `work_nonce` and `artifact_sha256`
  - submit/verify reconstruct the artifact and reject it unless the artifact was issued by this publisher instance/database for the submitting hotkey
- Clone replay controls:
  - `CATHEDRAL_VERIFIABLE_SAT_REQUIRE_SYSTEM_REPLAY=1` rejects SAT payouts unless a clone replay receipt verifies.
  - `CATHEDRAL_VERIFIABLE_SAT_REPLAY_CMD="..."` points to an operator-controlled command, not miner input.
  - The command receives `cathedral.subtensor_clone_replay_request.v1` JSON on stdin and returns `cathedral.subtensor_clone_replay_receipt.v1` JSON on stdout.
  - `CATHEDRAL_VERIFIABLE_SAT_REPLAY_ALLOWED_RUNNERS=subtensor_clone_rust_v1` is the production default.
  - CI uses `subtensor_clone_shadow_v1` only as a deterministic fixture; it is not a launch substitute for a real Rust/Subtensor clone runner.
- Encoder-agent command:

```bash
echo '{"schema_version":"cathedral.hermes_encoder_packet.v1","agent_id":"hermes-coinbase-encoder-v1","agent_image_digest":"sha256:<allowlisted-image-digest>","work_nonce":"<server-issued-nonce>","ckb_enabled":true,"width":64}' \
  | python -m game.arena.coinbase_encoder_agent
```

- The output includes:
  - `cnf_text`
  - `decode_map`
  - `clause_source_map`
  - `public_artifact`
  - `tdx_report_data_hex`
- Current limitation:
  - the oracle now derives takes from bounded fixed-point rates; it is still a focused one-parent model of the bug-bearing childkey split, not full `run_coinbase.rs` node execution.
  - the CI smoke uses small bit widths for tractability; the encoder is width-parametric up to u64.
  - the payment path now fails closed when system replay is required but no clone runner is configured.
  - the payment path now fails closed when TDX attestation is required but no real DCAP verifier/quote is configured.
  - report-data-only mode and `subtensor_clone_shadow_v1` are CI/shadow fixtures, not production launch evidence.

## Scoring Ladder

- Live scanner v0 earning policy: `accepted deterministic replay -> audit_replay_v1 row -> signed-vector audit bonus`.
- Subtensor clone package policy: `earning_policy = shadow_replay_only` until a real clone runner is wired.
- Ladder:
  - Malformed task, CNF, decode map, or DIMACS witness: zero.
  - SAT-valid witness that fails deterministic replay: zero.
  - Reproduced scanner witness: accepted trace and live audit replay bonus unless payment rows are disabled.
  - Reproduced scanner witness with `CATHEDRAL_AUDIT_SCANNER_REQUIRE_ATTESTATION=1`: accepted trace, pending payment until attested.
  - Reproduced Subtensor clone package: accepted shadow trace only until the clone runner is promoted.

## Local Check

```bash
python3 audit_arena_verify.py
python3 -m game.arena.audit_scanner_smoke
```
