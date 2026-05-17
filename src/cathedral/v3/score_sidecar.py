"""v3 score-record sidecar.

Builds a private JSON record that pairs the signed v3 wire row with
the hidden oracle, the trace-bundle handles, and corpus provenance.
Lives next to the encrypted Hermes bundle in Hippius so offline
verifiers can re-score later and training pipelines can catalog rows
without re-running the eval.

Strict scope: never goes near the public feed. This module is
called by the orchestrator AFTER the bundle has been published and
BEFORE the eval_run lands in the publisher DB. Failure to upload the
sidecar must not crash the eval: the caller wraps in try/except.
"""

from __future__ import annotations

import os
from typing import Any

from cathedral.v3.corpus.schema import ChallengeRow

# Versioned so future schema additions can be detected by readers.
SCORE_RECORD_SCHEMA: str = "cathedral.v3.score_record/1"
SCORER_VERSION: str = "v3.bug_isolation/1"


def _commit_hash() -> str:
    """Caller env supplies the deployed commit so the sidecar carries
    enough provenance to reproduce the score offline."""
    return os.environ.get("CATHEDRAL_GIT_COMMIT") or "unknown"


def build_score_record(
    *,
    signed_row: dict[str, Any],
    challenge: ChallengeRow,
    submission: dict[str, Any],
    duration_ms: int,
    repair_was_attempted: bool,
    package_blake3: str,
    manifest_hash: str,
) -> dict[str, Any]:
    """Assemble the private score record dict for upload.

    The dict shape is the v3 score-record/1 contract. Reader code
    (offline verifier, training catalog) keys on every field, so
    additions must keep the same names and types.
    """
    # corpus_version is optional on ChallengeRow today; fall back rather
    # than tightening the schema, since adding a required field would
    # break the existing private seed corpus mid-flight.
    corpus_version = getattr(challenge, "corpus_version", None) or "unknown"

    score_parts = signed_row.get("score_parts") or {}
    if not isinstance(score_parts, dict):
        score_parts = dict(score_parts)

    return {
        "schema": SCORE_RECORD_SCHEMA,
        "eval_run_id": str(signed_row.get("id") or ""),
        "miner_hotkey": str(signed_row.get("miner_hotkey") or submission.get("miner_hotkey") or ""),
        "agent_id": str(signed_row.get("agent_id") or submission.get("id") or ""),
        "task_type": "bug_isolation_v1",
        "challenge_id_public": str(signed_row.get("challenge_id_public") or ""),
        "corpus_row_id": str(challenge.id),
        "corpus_version": str(corpus_version),
        "scorer_version": SCORER_VERSION,
        "cathedral_commit": _commit_hash(),
        "ran_at": str(signed_row.get("ran_at") or ""),
        "duration_ms": int(duration_ms),
        "claim": signed_row.get("claim"),
        "hidden_oracle": {
            "culprit_file": challenge.culprit_file,
            "culprit_symbol": challenge.culprit_symbol,
            "line_range": [int(challenge.line_range[0]), int(challenge.line_range[1])],
            "required_failure_keywords": list(challenge.required_failure_keywords),
        },
        "score_parts": dict(score_parts),
        "weighted_score": float(signed_row.get("weighted_score") or 0.0),
        "failure_reason": signed_row.get("failure_reason"),
        "repair_was_attempted": bool(repair_was_attempted),
        "package_blake3": str(package_blake3),
        "manifest_hash": str(manifest_hash),
        "cathedral_signature": str(signed_row.get("cathedral_signature") or ""),
    }


__all__ = [
    "SCORER_VERSION",
    "SCORE_RECORD_SCHEMA",
    "build_score_record",
]
