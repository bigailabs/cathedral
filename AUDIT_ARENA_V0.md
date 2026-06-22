# Audit Arena v0

## Scope

- Offline verifier only: no live services, no target subnet calls, no validator emissions.
- Launch path: DIMACS witness -> CNF check -> SAT-bound decode -> deterministic replay -> private trace.
- Source files:
  - `scaffold/lanes/audit_arena.py`
  - `audit_arena_verify.py`

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

## Scoring Ladder

- v0 is shadow-only: `earning_policy = shadow_replay_only`.
- Ladder:
  - Malformed task, CNF, decode map, or DIMACS witness: zero.
  - SAT-valid witness that fails deterministic replay: zero.
  - Reproduced low-severity/dust witness: accepted trace, zero live emissions.
  - Reproduced material witness: accepted trace, eligible for manual review/disclosure policy.
  - Future live rewards require a separate policy flip and should consume replay-verified traces only.

## Local Check

```bash
python3 audit_arena_verify.py
```
