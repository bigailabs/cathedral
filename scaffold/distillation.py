"""Distillation trace redaction and export policy.

Audit traces are useful training data, but raw traces can contain exploit
witnesses, target details, and miner-agent reasoning. This module keeps the safe
default private and makes public export an explicit policy decision.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from typing import Any


_PUBLIC_ALLOWED_DISCLOSURE = {"fixed", "public", "opt_in", "cathedral_owned"}
_PUBLIC_MIN_HASH_SECRET_CHARS = 32
_PUBLIC_HASH_GUIDANCE = (
    "Use a 128-bit-or-stronger random redaction salt or server-side HMAC secret; "
    "never publish the secret with public traces."
)


@dataclass(frozen=True)
class RedactionPolicy:
    """Operator policy for exporting a distillation trace.

    `salt` is treated as a redaction secret. Public exports require a strong
    value; operators should prefer a server-side HMAC secret and keep it private.
    """

    audience: str = "private"  # private | public
    disclosure_status: str = "private"
    salt: str = ""
    include_agent_trace: bool = False
    include_raw_witness: bool = False
    include_replay_artifacts: bool = False


def export_trace(trace: dict[str, Any], policy: RedactionPolicy | None = None) -> dict[str, Any]:
    """Return a policy-safe trace export.

    Private export keeps enough structure for internal training while hashing
    identities and dropping raw agent notes by default. Public export is stricter
    and refuses undisclosed targets unless the operator policy says the issue is
    fixed/public/opt-in/Cathedral-owned.
    """
    policy = policy or RedactionPolicy()
    audience = str(policy.audience or "private").lower()
    if audience not in {"private", "public"}:
        raise ValueError("invalid_export_audience")
    if audience == "public" and policy.disclosure_status not in _PUBLIC_ALLOWED_DISCLOSURE:
        raise ValueError("public_export_requires_fixed_public_opt_in_or_cathedral_owned")
    if audience == "public" and not str(policy.salt or "").strip():
        raise ValueError("public_export_requires_non_empty_redaction_salt")
    if audience == "public" and not _public_hash_secret_is_strong(policy.salt):
        raise ValueError("public_export_requires_strong_salt_or_hmac_secret")
    if not isinstance(trace, dict):
        raise ValueError("trace_must_be_object")

    out: dict[str, Any] = {
        "schema_version": "cathedral.audit_trace.export.v1",
        "source_schema_version": str(trace.get("schema_version") or ""),
        "audience": audience,
        "disclosure_status": policy.disclosure_status,
        "source_trace_hash": str(trace.get("trace_hash") or ""),
        "private": audience != "public",
        "task": _redact_task(trace.get("task"), policy),
        "submission": _redact_submission(trace.get("submission"), policy),
        "verdict": _redact_verdict(trace, policy),
        "supervision": _redact_supervision(trace, policy),
        "dataset": _dataset_metadata(trace, audience),
        "redaction": {
            "miner_hotkey_hashed": True,
            "hash_algorithm": "hmac-sha256" if str(policy.salt or "").strip() else "sha256",
            "raw_witness_included": bool(policy.include_raw_witness and audience == "private"),
            "agent_trace_included": bool(policy.include_agent_trace and audience == "private"),
            "replay_artifacts_included": bool(policy.include_replay_artifacts and audience == "private"),
            "public_export_requires_disclosure_gate": True,
            "public_hash_secret_required": audience == "public",
            "public_hash_secret_min_chars": _PUBLIC_MIN_HASH_SECRET_CHARS,
            "public_hash_guidance": _PUBLIC_HASH_GUIDANCE if audience == "public" else "",
        },
    }
    out["export_hash"] = _hash_obj(out)
    return out


def _redact_task(value: Any, policy: RedactionPolicy) -> dict[str, Any]:
    task = value if isinstance(value, dict) else {}
    target = task.get("target") if isinstance(task.get("target"), dict) else {}
    audience = str(policy.audience or "private").lower()
    public = audience == "public"
    return {
        "task_id_hash": _hash_value(task.get("task_id"), policy),
        "target_id_hash": _hash_value(target.get("target_id"), policy),
        "repo_url": "" if public else str(target.get("repo_url") or ""),
        "commit": "" if public else str(target.get("commit") or ""),
        "netuid": target.get("netuid") if not public else None,
        "invariant_id": str(task.get("invariant_id") or ""),
        "invariant_hash": _hash_value(task.get("invariant"), policy),
        "challenge_id_hash": _hash_value(task.get("challenge_id"), policy),
        "cnf_sha256": str(task.get("cnf_sha256") or ""),
        "decode_kind": str(task.get("decode_kind") or ""),
        "decode_map_sha256": str(task.get("decode_map_sha256") or ""),
        "audit_package_sha256": str(task.get("audit_package_sha256") or ""),
        "severity_hint": str(task.get("severity_hint") or ""),
        "replay_kind": str(task.get("replay_kind") or ""),
        "earning_policy": str(task.get("earning_policy") or ""),
    }


def _redact_submission(value: Any, policy: RedactionPolicy) -> dict[str, Any]:
    submission = value if isinstance(value, dict) else {}
    out = {
        "miner_hotkey_hash": _hash_value(submission.get("miner_hotkey"), policy),
        "dimacs_solution_sha256": str(submission.get("dimacs_solution_sha256") or ""),
    }
    if policy.include_agent_trace and policy.audience == "private":
        out["agent_trace"] = submission.get("agent_trace") if isinstance(
            submission.get("agent_trace"), dict) else {}
    else:
        out["agent_trace_hash"] = _hash_value(submission.get("agent_trace"), policy)
    return out


def _redact_verdict(value: Any, policy: RedactionPolicy) -> dict[str, Any]:
    trace = value if isinstance(value, dict) else {}
    supervision = trace.get("supervision") if isinstance(trace.get("supervision"), dict) else {}
    verdict = trace.get("verdict") if isinstance(trace.get("verdict"), dict) else {}
    decoded_witness = verdict.get("decoded_witness", trace.get("decoded_witness"))
    replay = verdict.get("replay", trace.get("replay"))
    replay = replay if isinstance(replay, dict) else {}
    out = {
        "accepted": bool(verdict.get("accepted", supervision.get("accepted"))),
        "stage": str(verdict.get("stage", supervision.get("stage")) or ""),
        "rejection_reason": str(
            verdict.get("rejection_reason", supervision.get("rejection_reason")) or ""),
        "decoded_witness_hash": _hash_value(decoded_witness, policy),
        "replay": {
            "reproduced": bool(replay.get("reproduced")),
            "reason": str(replay.get("reason") or ""),
            "score_before": _safe_number(replay.get("score_before")),
            "score_after": _safe_number(replay.get("score_after")),
            "estimated_earning": _safe_number(replay.get("estimated_earning")),
            "severity": _safe_number(replay.get("severity")),
        },
    }
    if policy.include_raw_witness and policy.audience == "private":
        out["decoded_witness"] = decoded_witness
    if policy.include_replay_artifacts and policy.audience == "private":
        out["replay"]["artifacts"] = replay.get("artifacts") if isinstance(
            replay.get("artifacts"), dict) else {}
    else:
        out["replay"]["artifacts_hash"] = _hash_value(replay.get("artifacts"), policy)
    return out


def _redact_supervision(value: Any, _policy: RedactionPolicy) -> dict[str, Any]:
    trace = value if isinstance(value, dict) else {}
    supervision = trace.get("supervision") if isinstance(trace.get("supervision"), dict) else {}
    accepted = bool(supervision.get("accepted"))
    return {
        "label": str(trace.get("label") or supervision.get("label") or ""),
        "live_earning_policy": str(supervision.get("live_earning_policy") or ""),
        "accepted": accepted,
        "stage": str(supervision.get("stage") or ""),
        "rejection_reason": str(supervision.get("rejection_reason") or ""),
        "training_value": (
            "positive_replay_example" if accepted else "valuable_negative_control"
        ),
    }


def _dataset_metadata(trace: dict[str, Any], audience: str) -> dict[str, Any]:
    supervision = trace.get("supervision") if isinstance(trace.get("supervision"), dict) else {}
    accepted = bool(supervision.get("accepted"))
    label = str(trace.get("label") or "")
    if accepted or label == "reproduced_witness":
        category = "accepted_reproduced_witness"
        retention_value = "positive_replay_example"
    elif label == "rejected_claim" or not accepted:
        category = "rejected_claim_negative_control"
        retention_value = "valuable_negative_control"
    else:
        category = "unknown_audit_trace"
        retention_value = "operator_review_required"
    return {
        "category": category,
        "retention_value": retention_value,
        "visibility": "public_redacted" if audience == "public" else "private_training",
        "rejected_traces_are_valuable": True,
    }


def _safe_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hash_value(value: Any, policy: RedactionPolicy) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    secret = str(policy.salt or "")
    if secret:
        return hmac.new(secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_obj(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _public_hash_secret_is_strong(value: str) -> bool:
    secret = str(value or "").strip()
    return len(secret) >= _PUBLIC_MIN_HASH_SECRET_CHARS and len(set(secret)) >= 8
