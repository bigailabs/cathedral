"""Audit Arena v0: SAT witness -> deterministic replay -> distillation trace.

This module is deliberately offline and side-effect free. It does not contact
Polaris, Bittensor, or a live target subnet. The goal is the first reviewable
slice for Cathedral's audit lane:

* a miner submits a DIMACS satisfying assignment,
* Cathedral verifies it against the exact CNF,
* the assignment is decoded into an audit witness,
* a deterministic replay function proves whether the witness reproduces, and
* the result is normalized into a trace suitable for distillation.

Live earning, disclosure, attestation, and paid infrastructure belong outside
this pure verifier seam.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import inspect
import json
import math
from typing import Any, Callable

from ..publisher.sat_solution import verify_dimacs_solution


ReplayFn = Callable[[dict[str, Any], "AuditTask"], "ReplayEvidence | dict[str, Any]"]
AUDIT_SUBMISSION_SCHEMA_VERSION = "cathedral.audit_submission.v1"
AUDIT_TRACE_SCHEMA_VERSION = "cathedral.audit_trace.v1"


@dataclass(frozen=True)
class AuditTarget:
    """Pinned target context for a Bittensor audit task."""

    target_id: str
    repo_url: str
    commit: str
    netuid: int | None = None
    validator_entrypoint: str = ""
    scoring_entrypoint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "repo_url": self.repo_url,
            "commit": self.commit,
            "netuid": self.netuid,
            "validator_entrypoint": self.validator_entrypoint,
            "scoring_entrypoint": self.scoring_entrypoint,
        }


@dataclass(frozen=True)
class AuditTask:
    """One audit challenge.

    `decode_map` supports the current audit-hunter smoke format:

        {"witness": {"amount": 1, "fee_rate": 49152}}

    and the fuller bit projection format we need for real bit-blasted CNFs:

        {"fields": {"amount": {"bits": [1, 2, 3], "signed": false}}}

    In the bit format, list position is the bit index by default (lsb0).
    """

    task_id: str
    target: AuditTarget
    invariant_id: str
    invariant: str
    challenge_id: str
    cnf_sha256: str
    decode_map: dict[str, Any]
    expected: str = "SAT = candidate exploit witness"
    severity_hint: str = ""
    replay_kind: str = "shadow_replay"
    earning_policy: str = "shadow_replay_only"
    source: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        decode_map_text = json.dumps(
            self.decode_map, sort_keys=True, separators=(",", ":"), default=str)
        return {
            "task_id": self.task_id,
            "target": self.target.to_dict(),
            "invariant_id": self.invariant_id,
            "invariant": self.invariant,
            "challenge_id": self.challenge_id,
            "cnf_sha256": self.cnf_sha256,
            "decode_kind": decode_kind(self.decode_map),
            "decode_map": self.decode_map,
            "decode_map_sha256": sha256_text(decode_map_text),
            "audit_package_sha256": sha256_text(
                f"{self.challenge_id}:{self.cnf_sha256}:{decode_map_text}"),
            "expected": self.expected,
            "severity_hint": self.severity_hint,
            "replay_kind": self.replay_kind,
            "earning_policy": self.earning_policy,
            "source": self.source,
        }


@dataclass(frozen=True)
class MinerAuditSubmission:
    """Miner output for an audit task.

    The raw wire payload is:

        task_id, miner_hotkey, dimacs_solution, optional agent_trace

    `dimacs_solution` is a standard DIMACS solver output blob. The private trace
    stores only its hash, so verifier provenance can be shared without leaking
    the full witness by default.
    """

    task_id: str
    miner_hotkey: str
    dimacs_solution: str
    schema_version: str = AUDIT_SUBMISSION_SCHEMA_VERSION
    agent_trace: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "miner_hotkey": self.miner_hotkey,
            "dimacs_solution_sha256": sha256_text(self.dimacs_solution),
            "agent_trace": self.agent_trace,
        }


@dataclass(frozen=True)
class ReplayEvidence:
    """Output from the deterministic target replay."""

    reproduced: bool
    reason: str = ""
    score_before: float | None = None
    score_after: float | None = None
    estimated_earning: float | None = None
    severity: float | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reproduced": self.reproduced,
            "reason": self.reason,
            "score_before": self.score_before,
            "score_after": self.score_after,
            "estimated_earning": self.estimated_earning,
            "severity": self.severity,
            "artifacts": self.artifacts,
        }


@dataclass(frozen=True)
class AuditVerdict:
    """Verifier output for scoring/review/training."""

    accepted: bool
    stage: str
    rejection_reason: str | None
    decoded_witness: dict[str, Any]
    replay: ReplayEvidence | None
    distillation_trace: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "stage": self.stage,
            "rejection_reason": self.rejection_reason,
            "decoded_witness": self.decoded_witness,
            "replay": self.replay.to_dict() if self.replay else None,
            "distillation_trace": self.distillation_trace,
        }


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_and_replay(
    task: AuditTask,
    submission: MinerAuditSubmission,
    *,
    cnf_text: str,
    replay_fn: ReplayFn,
) -> AuditVerdict:
    """Verify a miner's SAT witness and replay it against a deterministic oracle."""
    if submission.task_id != task.task_id:
        return _reject(task, submission, "sat_verify", "task_id_mismatch")

    if not task.cnf_sha256:
        return _reject(task, submission, "sat_verify", "cnf_sha256_required")
    if sha256_text(cnf_text) != task.cnf_sha256:
        return _reject(task, submission, "sat_verify", "cnf_sha256_mismatch")

    check = verify_dimacs_solution(cnf_text, submission.dimacs_solution)
    if not check.ok:
        return _reject(task, submission, "sat_verify", check.rejection_reason or "bad_solution")

    allow_smoke_decode = _allows_smoke_decode(task)
    try:
        decoded = decode_witness(
            check.assignment,
            task.decode_map,
            allow_static=allow_smoke_decode,
            allow_sparse=allow_smoke_decode,
        )
    except ValueError as e:
        return _reject(task, submission, "decode", str(e))

    try:
        replay = _coerce_replay_evidence(replay_fn(decoded, task))
        replay = _with_replay_adapter(replay, replay_fn)
    except ValueError as e:
        return _reject(task, submission, "replay", str(e))
    except Exception as e:
        return _reject(task, submission, "replay", f"replay_exception:{type(e).__name__}")

    accepted = bool(replay.reproduced)
    reason = None if accepted else (replay.reason or "replay_did_not_reproduce")
    verdict = AuditVerdict(
        accepted=accepted,
        stage="accepted" if accepted else "replay",
        rejection_reason=reason,
        decoded_witness=decoded,
        replay=replay,
        distillation_trace={},
    )
    trace = build_distillation_trace(task, submission, verdict)
    return AuditVerdict(
        accepted=verdict.accepted,
        stage=verdict.stage,
        rejection_reason=verdict.rejection_reason,
        decoded_witness=verdict.decoded_witness,
        replay=verdict.replay,
        distillation_trace=trace,
    )


def decode_witness(
    assignment: list[int],
    decode_map: dict[str, Any],
    *,
    allow_static: bool = False,
    allow_sparse: bool = False,
) -> dict[str, Any]:
    """Decode a satisfying assignment into target inputs.

    Current audit-hunter maps often include a pre-decoded `witness` object. That
    is accepted only when `allow_static_witness` is true and the task is in a
    smoke/corpus replay mode, so a production package cannot accidentally replay
    inputs that are decoupled from the miner's SAT assignment.

    Production encoders should emit `fields` with DIMACS variable projections
    and a non-empty `required_fields` list. The verifier cannot infer which SAT
    variables are auxiliary solver variables, but it can require the package to
    declare the replay-critical input fields and map all of them.
    """
    if not isinstance(decode_map, dict):
        raise ValueError("decode_map_must_be_object")

    direct = decode_map.get("witness")
    if isinstance(direct, dict):
        if not (allow_static and bool(decode_map.get("allow_static_witness"))):
            raise ValueError("static_witness_decode_requires_allow_static_witness")
        return dict(direct)

    fields = decode_map.get("fields")
    if not isinstance(fields, dict):
        raise ValueError("decode_map_missing_witness_or_fields")
    if not fields:
        raise ValueError("decode_map_fields_empty")

    _validate_decode_scope(fields, decode_map, allow_sparse=allow_sparse)
    values = _assignment_values(assignment)
    decoded: dict[str, Any] = {}
    for name, spec in fields.items():
        if not isinstance(spec, dict):
            raise ValueError(f"decode_field_{name}_must_be_object")
        bits = spec.get("bits") or spec.get("vars")
        if not isinstance(bits, list) or not bits:
            raise ValueError(f"decode_field_{name}_missing_bits")
        decoded[str(name)] = _decode_int_field(values, bits, bool(spec.get("signed")))
    return decoded


def decode_kind(decode_map: dict[str, Any]) -> str:
    if isinstance(decode_map.get("fields"), dict):
        return "bit_projection"
    if isinstance(decode_map.get("witness"), dict):
        return "static_witness"
    if isinstance(decode_map.get("decode_inputs"), list):
        return "decode_inputs_only"
    return "unknown"


def _allows_smoke_decode(task: AuditTask) -> bool:
    return task.replay_kind in {"corpus_smoke", "known_answer_smoke"}


def _validate_decode_scope(
    fields: dict[str, Any],
    decode_map: dict[str, Any],
    *,
    allow_sparse: bool,
) -> None:
    sparse_flag = any(
        bool(decode_map.get(key))
        for key in ("allow_sparse_decode", "sparse_decode", "sparse")
    )
    if sparse_flag and not allow_sparse:
        raise ValueError("sparse_decode_requires_smoke_or_corpus")

    required = decode_map.get("required_fields")
    if required is None:
        if allow_sparse:
            return
        raise ValueError("sparse_decode_requires_required_fields")

    if (
        not isinstance(required, list)
        or not required
        or any(not isinstance(name, str) or not name for name in required)
    ):
        raise ValueError("required_fields_must_be_non_empty_string_list")

    missing = sorted(name for name in required if name not in fields)
    if missing:
        raise ValueError("decode_map_missing_required_fields:" + ",".join(missing))


def build_distillation_trace(
    task: AuditTask,
    submission: MinerAuditSubmission,
    verdict: AuditVerdict,
) -> dict[str, Any]:
    """Normalize the verifier output into a private training-data record."""
    label = "reproduced_witness" if verdict.accepted else "rejected_claim"
    task_doc = task.to_dict()
    replay = verdict.replay.to_dict() if verdict.replay else None
    trace = {
        "schema_version": AUDIT_TRACE_SCHEMA_VERSION,
        "private": True,
        "label": label,
        "task": task_doc,
        "submission": submission.to_dict(),
        "decoded_witness": verdict.decoded_witness,
        "replay": replay,
        "replay_provenance": _replay_provenance(task_doc, replay),
        "supervision": {
            "accepted": verdict.accepted,
            "stage": verdict.stage,
            "rejection_reason": verdict.rejection_reason,
            "deterministic_replay_required": True,
            "live_earning_policy": task.earning_policy,
        },
    }
    trace["trace_hash"] = _hash_obj(trace)
    return trace


def fixedpoint_fee_silent_zero_replay(
    decoded: dict[str, Any],
    task: AuditTask,
    *,
    scale_bits: int = 16,
) -> ReplayEvidence:
    """Reference replay for the audit-hunter AMM B2 smoke invariant.

    The violation is: amount > 0 and fee_rate > 0, but integer fixed-point fee
    floors to 0. This is mechanically reproducible, usually dust severity.
    """
    amount = int(decoded.get("amount", 0))
    fee_rate = int(decoded.get("fee_rate", 0))
    fee = (amount * fee_rate) >> scale_bits
    reproduced = amount > 0 and fee_rate > 0 and fee == 0
    return ReplayEvidence(
        reproduced=reproduced,
        reason="" if reproduced else "fee_not_silent_zero",
        score_before=0.0,
        score_after=1.0 if reproduced else 0.0,
        estimated_earning=0.0,
        severity=1.0 if reproduced else 0.0,
        artifacts={
            "target": task.target.target_id,
            "amount": amount,
            "fee_rate": fee_rate,
            "scale_bits": scale_bits,
            "computed_fee": fee,
            "triage": "dust" if reproduced else "not_reproduced",
        },
    )


def _assignment_values(assignment: list[int]) -> dict[int, bool]:
    values: dict[int, bool] = {}
    for lit in assignment:
        if not isinstance(lit, int) or lit == 0:
            raise ValueError("assignment_contains_non_literal")
        var = abs(lit)
        val = lit > 0
        if var in values and values[var] != val:
            raise ValueError("assignment_contains_contradiction")
        values[var] = val
    return values


def _decode_int_field(values: dict[int, bool], bits: list[Any], signed: bool) -> int:
    value = 0
    width = 0
    for idx, item in enumerate(bits):
        if isinstance(item, dict):
            var = int(item.get("var") or item.get("variable") or 0)
            bit_idx = int(item.get("bit", idx))
        else:
            var = int(item)
            bit_idx = idx
        if var <= 0:
            raise ValueError("decode_bit_var_must_be_positive")
        if bit_idx < 0:
            raise ValueError("decode_bit_index_must_be_non_negative")
        if var not in values:
            raise ValueError("decode_var_missing_from_assignment")
        width = max(width, bit_idx + 1)
        if values.get(var, False):
            value |= 1 << bit_idx
    if signed and width > 0 and value >= (1 << (width - 1)):
        value -= 1 << width
    return value


def _coerce_replay_evidence(value: ReplayEvidence | dict[str, Any]) -> ReplayEvidence:
    if isinstance(value, ReplayEvidence):
        return value
    if not isinstance(value, dict):
        raise TypeError("replay_fn_must_return_replay_evidence_or_dict")
    return ReplayEvidence(
        reproduced=_required_bool(value.get("reproduced"), "reproduced"),
        reason=str(value.get("reason") or ""),
        score_before=_optional_float(value.get("score_before")),
        score_after=_optional_float(value.get("score_after")),
        estimated_earning=_optional_float(value.get("estimated_earning")),
        severity=_optional_float(value.get("severity")),
        artifacts=dict(value.get("artifacts") or {}),
    )


def _required_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"replay_{field}_must_be_boolean")


def _with_replay_adapter(replay: ReplayEvidence, replay_fn: ReplayFn) -> ReplayEvidence:
    adapter_id = _replay_adapter_id(replay_fn)
    artifacts = dict(replay.artifacts)
    artifacts.setdefault("replay_adapter_id", adapter_id)
    artifacts.setdefault("replay_adapter_sha256", sha256_text(adapter_id))
    artifacts.setdefault("replay_code_sha256", _callable_code_sha256(replay_fn))
    return ReplayEvidence(
        reproduced=replay.reproduced,
        reason=replay.reason,
        score_before=replay.score_before,
        score_after=replay.score_after,
        estimated_earning=replay.estimated_earning,
        severity=replay.severity,
        artifacts=artifacts,
    )


def _replay_adapter_id(replay_fn: ReplayFn) -> str:
    return (
        f"{getattr(replay_fn, '__module__', '')}."
        f"{getattr(replay_fn, '__qualname__', repr(replay_fn))}"
    )


def _callable_code_sha256(replay_fn: ReplayFn) -> str:
    try:
        source = inspect.getsource(replay_fn)
    except (OSError, TypeError):
        source = _replay_adapter_id(replay_fn)
    return sha256_text(source)


def _replay_provenance(
    task_doc: dict[str, Any],
    replay: dict[str, Any] | None,
) -> dict[str, Any]:
    artifacts = replay.get("artifacts") if isinstance(replay, dict) else {}
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    target = task_doc.get("target") if isinstance(task_doc.get("target"), dict) else {}
    return {
        "deterministic_replay_required": True,
        "replay_kind": str(task_doc.get("replay_kind") or ""),
        "replay_adapter_id": str(artifacts.get("replay_adapter_id") or ""),
        "replay_adapter_sha256": str(artifacts.get("replay_adapter_sha256") or ""),
        "replay_code_sha256": str(artifacts.get("replay_code_sha256") or ""),
        "cnf_sha256": str(task_doc.get("cnf_sha256") or ""),
        "decode_map_sha256": str(task_doc.get("decode_map_sha256") or ""),
        "audit_package_sha256": str(task_doc.get("audit_package_sha256") or ""),
        "target_commit": str(target.get("commit") or ""),
    }


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    out = float(value)
    if not math.isfinite(out):
        raise ValueError("non_finite_replay_number")
    return out


def _reject(
    task: AuditTask,
    submission: MinerAuditSubmission,
    stage: str,
    reason: str,
) -> AuditVerdict:
    verdict = AuditVerdict(
        accepted=False,
        stage=stage,
        rejection_reason=reason,
        decoded_witness={},
        replay=None,
        distillation_trace={},
    )
    trace = build_distillation_trace(task, submission, verdict)
    return AuditVerdict(
        accepted=False,
        stage=stage,
        rejection_reason=reason,
        decoded_witness={},
        replay=None,
        distillation_trace=trace,
    )


def _hash_obj(obj: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
