"""Pure Subtensor clone replay package adapter for Audit Arena v0.

This module does not start a node, run Docker, call Subtensor, or execute miner
scripts. It validates a pinned replay package against an injected observation or
runner output, then evaluates deterministic invariant checks. Real clone
execution belongs behind a later runner that feeds this same pure seam.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any, Callable

from .audit_arena import AuditTask, ReplayEvidence, ReplayFn


SUBTENSOR_REPLAY_SCHEMA_VERSION = "cathedral.subtensor_replay.v1"
RunnerFn = Callable[[dict[str, Any], AuditTask], dict[str, Any]]


@dataclass(frozen=True)
class SubtensorReplayPackage:
    """Pinned replay package for a Subtensor clone shadow task."""

    target_commit: str
    runtime_sha256: str
    clone_state_sha256: str
    script_sha256: str
    script_steps: list[dict[str, Any]]
    invariant_id: str
    expected_witness: dict[str, Any]
    checks: list[dict[str, Any]]
    artifact_sha256: dict[str, str] = field(default_factory=dict)
    schema_version: str = SUBTENSOR_REPLAY_SCHEMA_VERSION
    clone_block: int | None = None
    clone_state_root: str = ""
    allowed_environment: str = "subtensor-clone-shadow"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SubtensorReplayPackage":
        if not isinstance(value, dict):
            raise ValueError("subtensor_replay_package_must_be_object")
        artifact_sha256 = value.get("artifact_sha256", value.get("artifact_hashes", {}))
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            target_commit=str(value.get("target_commit") or ""),
            runtime_sha256=str(value.get("runtime_sha256") or ""),
            clone_state_sha256=str(value.get("clone_state_sha256") or ""),
            clone_block=_optional_int(value.get("clone_block")),
            clone_state_root=str(value.get("clone_state_root") or ""),
            script_sha256=str(value.get("script_sha256") or ""),
            script_steps=_list_of_dicts(value.get("script_steps"), "script_steps"),
            invariant_id=str(value.get("invariant_id") or ""),
            expected_witness=dict(value.get("expected_witness") or {}),
            checks=_list_of_dicts(value.get("checks"), "checks"),
            artifact_sha256=_str_dict(artifact_sha256, "artifact_sha256"),
            allowed_environment=str(value.get("allowed_environment") or "subtensor-clone-shadow"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "target_commit": self.target_commit,
            "runtime_sha256": self.runtime_sha256,
            "clone_state_sha256": self.clone_state_sha256,
            "clone_block": self.clone_block,
            "clone_state_root": self.clone_state_root,
            "script_sha256": self.script_sha256,
            "script_steps": self.script_steps,
            "invariant_id": self.invariant_id,
            "expected_witness": self.expected_witness,
            "checks": self.checks,
            "artifact_sha256": self.artifact_sha256,
            "allowed_environment": self.allowed_environment,
        }

    def sha256(self) -> str:
        return _hash_obj(self.to_dict())


def make_subtensor_replay_adapter(
    package: SubtensorReplayPackage | dict[str, Any],
    *,
    observed_result: dict[str, Any] | None = None,
    runner: RunnerFn | None = None,
) -> ReplayFn:
    """Return an Audit Arena ReplayFn bound to one Subtensor replay package."""
    replay_package = (
        package if isinstance(package, SubtensorReplayPackage)
        else SubtensorReplayPackage.from_dict(package)
    )

    def _adapter(decoded: dict[str, Any], task: AuditTask) -> ReplayEvidence:
        observed = observed_result
        if observed is None and runner is not None:
            observed = runner(decoded, task)
        return verify_subtensor_replay(
            decoded,
            task,
            package=replay_package,
            observed_result=observed,
        )

    return _adapter


def verify_subtensor_replay(
    decoded: dict[str, Any],
    task: AuditTask,
    *,
    package: SubtensorReplayPackage,
    observed_result: dict[str, Any] | None,
) -> ReplayEvidence:
    """Validate one injected Subtensor replay observation and evaluate checks."""
    _validate_package(package, task)
    _validate_witness_binding(decoded, package.expected_witness)
    if observed_result is None:
        raise ValueError("subtensor_replay_missing_observation")
    if not isinstance(observed_result, dict):
        raise ValueError("subtensor_replay_observation_must_be_object")
    _validate_observed_hashes(package, observed_result)

    results = [_evaluate_check(observed_result, check) for check in package.checks]
    required = [result for result in results if result["required"]]
    reproduced = any(not result["holds"] for result in required)
    reason = "" if reproduced else "invariant_not_violated"

    artifacts = {
        "subtensor_replay_package_sha256": package.sha256(),
        "subtensor_replay_schema_version": package.schema_version,
        "target_commit": package.target_commit,
        "runtime_sha256": package.runtime_sha256,
        "clone_state_sha256": package.clone_state_sha256,
        "clone_block": package.clone_block,
        "clone_state_root": package.clone_state_root,
        "script_sha256": package.script_sha256,
        "script_steps_sha256": _hash_obj({"script_steps": package.script_steps}),
        "invariant_id": package.invariant_id,
        "observed_result_sha256": _hash_obj(observed_result),
        "check_results": results,
        "triage": "candidate_replay" if reproduced else "not_reproduced",
    }
    return ReplayEvidence(
        reproduced=reproduced,
        reason=reason,
        score_before=0.0,
        score_after=1.0 if reproduced else 0.0,
        estimated_earning=0.0,
        severity=0.0,
        artifacts=artifacts,
    )


def _validate_package(package: SubtensorReplayPackage, task: AuditTask) -> None:
    if package.schema_version != SUBTENSOR_REPLAY_SCHEMA_VERSION:
        raise ValueError("subtensor_replay_schema_mismatch")
    if not package.target_commit:
        raise ValueError("target_commit_required")
    if package.target_commit != task.target.commit:
        raise ValueError("target_commit_mismatch")
    expected_package_sha = str(task.source.get("subtensor_replay_package_sha256") or "")
    if not expected_package_sha:
        raise ValueError("subtensor_replay_package_unpinned")
    if expected_package_sha != package.sha256():
        raise ValueError("subtensor_replay_package_sha256_mismatch")
    for field_name in ("runtime_sha256", "clone_state_sha256", "script_sha256"):
        if not _is_sha256(getattr(package, field_name)):
            raise ValueError(f"{field_name}_invalid")
    if not package.script_steps:
        raise ValueError("script_steps_required")
    if not package.invariant_id:
        raise ValueError("invariant_id_required")
    if package.invariant_id != task.invariant_id:
        raise ValueError("invariant_id_mismatch")
    if not package.expected_witness:
        raise ValueError("witness_binding_required")
    if not package.checks:
        raise ValueError("invariant_checks_required")
    if not any(bool(check.get("required", True)) for check in package.checks):
        raise ValueError("invariant_required_check_missing")
    for name, digest in package.artifact_sha256.items():
        if not _is_sha256(digest):
            raise ValueError(f"artifact_sha256_invalid:{name}")


def _validate_witness_binding(
    decoded: dict[str, Any],
    expected_witness: dict[str, Any],
) -> None:
    for key, expected in expected_witness.items():
        if key not in decoded or decoded[key] != expected:
            raise ValueError("witness_mismatch")


def _validate_observed_hashes(
    package: SubtensorReplayPackage,
    observed_result: dict[str, Any],
) -> None:
    for field_name in ("runtime_sha256", "clone_state_sha256", "script_sha256"):
        observed = str(observed_result.get(field_name) or "")
        if observed != getattr(package, field_name):
            raise ValueError(f"{field_name}_mismatch")
    observed_artifacts = _str_dict(observed_result.get("artifact_sha256", {}), "artifact_sha256")
    for name, digest in package.artifact_sha256.items():
        if observed_artifacts.get(name) != digest:
            raise ValueError(f"artifact_sha256_mismatch:{name}")


def _evaluate_check(observed_result: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(check, dict):
        raise ValueError("invariant_check_must_be_object")
    check_id = str(check.get("id") or "")
    if not check_id:
        raise ValueError("invariant_check_id_required")
    kind = str(check.get("kind") or "")
    if kind != "numeric_delta":
        raise ValueError("unsupported_invariant_check_kind:" + kind)

    before = _number_at_path(observed_result, str(check.get("before_path") or ""))
    after = _number_at_path(observed_result, str(check.get("after_path") or ""))
    expected_delta = _finite_float(check.get("expected_delta"))
    actual_delta = after - before
    operator = str(check.get("operator") or "delta_eq")
    holds = _compare(actual_delta, expected_delta, operator)
    return {
        "id": check_id,
        "kind": kind,
        "operator": operator,
        "required": bool(check.get("required", True)),
        "before": before,
        "after": after,
        "actual_delta": actual_delta,
        "expected_delta": expected_delta,
        "holds": holds,
    }


def _compare(actual: float, expected: float, operator: str) -> bool:
    if operator == "delta_eq":
        return actual == expected
    if operator == "delta_ne":
        return actual != expected
    if operator == "delta_gt":
        return actual > expected
    if operator == "delta_gte":
        return actual >= expected
    if operator == "delta_lt":
        return actual < expected
    if operator == "delta_lte":
        return actual <= expected
    raise ValueError("unsupported_invariant_operator:" + operator)


def _number_at_path(value: dict[str, Any], path: str) -> float:
    if not path:
        raise ValueError("invariant_path_required")
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ValueError("invariant_path_missing:" + path)
        current = current[part]
    return _finite_float(current)


def _finite_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("invariant_number_must_not_be_boolean")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError("invariant_number_must_be_finite") from None
    if not math.isfinite(out):
        raise ValueError("invariant_number_must_be_finite")
    return out


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _list_of_dicts(value: Any, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name}_must_be_list")
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{field_name}_items_must_be_objects")
        out.append(dict(item))
    return out


def _str_dict(value: Any, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name}_must_be_object")
    return {str(key): str(val) for key, val in value.items()}


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value)


def _hash_obj(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
