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
