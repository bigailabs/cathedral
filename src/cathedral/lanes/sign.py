"""Signed rows for Task Family lanes.

These rows are the generic wire shape for lanes implemented under
``cathedral.lanes``. The publisher signs the score result and validators
verify the signature before weighting. The row deliberately does not expose
the raw problem, hidden metadata, or submitted answer. Public feeds get only
stable hashes until an explicit reveal/export path is added.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import blake3

from cathedral.lanes.contract import PublicProblem, ScoreResult, Submission, VerifierResult
from cathedral.v1_types import canonical_json

TASK_FAMILY_SCHEMA_VERSION = 5

TASK_FAMILY_SIGNED_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "agent_id",
        "agent_display_name",
        "miner_hotkey",
        "task_type",
        "task_id_public",
        "epoch_salt",
        "difficulty_tier",
        "weighted_score",
        "score_parts",
        "answer_hash",
        "verifier_details_hash",
        "rejection_reason",
        "ran_at",
    }
)

# v6 — PAR-2 facts. Adds the open-window scoring inputs:
#   challenge_value — per-challenge budget w_c (from lane_challenges.score_multiplier)
#   solve_rank      — operator's rank among valid solvers (publisher receipt order)
#   solved          — whether this row is a valid solve (vs a rejection/loss)
#   operator        — operator identity (coldkey ss58) for the Sybil-resistant
#                     per-operator merit; weighted_score stays in [0,1].
# Validators verify v6 (forward-compatible); the publisher only EMITS v6 when a
# caller passes schema_version=6 (gated cutover) — v5 remains the default so
# live behavior and existing signatures are byte-identical until the flip.
# MUST stay byte-identical to _SIGNED_KEYS_BY_VERSION[6] in
# eval/v2_payload.py and validator/pull_loop.py (cross-module contract).
TASK_FAMILY_SCHEMA_VERSION_V6 = 6

TASK_FAMILY_SIGNED_KEYS_V6: frozenset[str] = TASK_FAMILY_SIGNED_KEYS | frozenset(
    {
        "challenge_value",
        "solve_rank",
        "solved",
        "operator",
    }
)

_TASK_ID_HASH_PREFIX_LEN = 16


def public_task_id(task_id: str, *, epoch_salt: str) -> str:
    """Return the public hash of a private lane task id."""
    body = f"{epoch_salt}:{task_id}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:_TASK_ID_HASH_PREFIX_LEN]


def canonical_hash(value: dict[str, Any]) -> str:
    """BLAKE3 hash of Cathedral canonical JSON."""
    return blake3.blake3(canonical_json(value)).hexdigest()


def build_signed_task_family_row(
    *,
    eval_run_id: str,
    submission_id: str,
    agent_display_name: str,
    miner_hotkey: str,
    problem: PublicProblem,
    submission: Submission,
    verifier: VerifierResult,
    score: ScoreResult,
    ran_at_iso: str,
    signer: Any,
    epoch_salt: str,
    schema_version: int = TASK_FAMILY_SCHEMA_VERSION,
    challenge_value: float | None = None,
    solve_rank: int | None = None,
    solved: bool | None = None,
    operator: str | None = None,
) -> dict[str, Any]:
    """Build and sign a generic Task Family row.

    The signed subset is enough for validators to authenticate the score
    and assign weight. It is not enough for miners to recover the original
    formula or another miner's solution.

    ``schema_version`` defaults to 5 (the live wire shape, byte-identical to
    existing signatures). Pass ``schema_version=6`` to emit the PAR-2 fact row,
    which additionally signs ``challenge_value`` / ``solve_rank`` / ``solved`` /
    ``operator``. This is gated by the caller (the publisher emits v6 only after
    the open-window cutover); validators are v6-verify-capable regardless.
    """
    rejection_reason = score.rejection_reason or verifier.rejection_reason
    signed_subset: dict[str, Any] = {
        "id": eval_run_id,
        "agent_id": submission_id,
        "agent_display_name": agent_display_name,
        "miner_hotkey": miner_hotkey,
        "task_type": problem.task_family,
        "task_id_public": public_task_id(problem.task_id, epoch_salt=epoch_salt),
        "epoch_salt": epoch_salt,
        "difficulty_tier": int(problem.difficulty_tier),
        "weighted_score": float(score.weighted_score),
        "score_parts": dict(score.score_parts),
        "answer_hash": canonical_hash(submission.answer),
        "verifier_details_hash": canonical_hash(verifier.details),
        "rejection_reason": rejection_reason,
        "ran_at": ran_at_iso,
    }

    if schema_version >= TASK_FAMILY_SCHEMA_VERSION_V6:
        keyset = TASK_FAMILY_SIGNED_KEYS_V6
        signed_subset["challenge_value"] = float(
            challenge_value if challenge_value is not None else 0.0
        )
        signed_subset["solve_rank"] = int(solve_rank) if solve_rank is not None else 0
        signed_subset["solved"] = (
            bool(solved) if solved is not None else float(score.weighted_score) > 0.0
        )
        signed_subset["operator"] = operator or ""
    else:
        keyset = TASK_FAMILY_SIGNED_KEYS

    extra = set(signed_subset) - set(keyset)
    missing = set(keyset) - set(signed_subset)
    if extra or missing:
        raise RuntimeError(
            f"task family signed subset diverged from keyset (v{schema_version}): "
            f"extra={sorted(extra)} missing={sorted(missing)}"
        )

    sign = getattr(signer, "sign", None)
    if sign is None:
        raise TypeError("task family signer must expose sign(payload)")
    sig_b64 = str(sign(signed_subset))
    row = dict(signed_subset)
    row["cathedral_signature"] = sig_b64
    row["eval_output_schema_version"] = int(schema_version)
    return row


def resign_task_family_score(
    row: dict[str, Any],
    *,
    signer: Any,
    weighted_score: float,
    score_parts: dict[str, Any],
    rejection_reason: str | None,
) -> dict[str, Any]:
    """Re-sign an existing hash-only Task Family row with a final score.

    Receipt ordering can verify a later-valid answer before the earlier
    receipt resolves. The publisher stores the validated hash-only row
    privately, then re-signs it as a zero-score loser once the durable
    first-submitted winner is known.
    """
    signed_subset = {key: row[key] for key in TASK_FAMILY_SIGNED_KEYS if key in row}
    missing = set(TASK_FAMILY_SIGNED_KEYS) - set(signed_subset)
    if missing:
        raise RuntimeError(f"cannot re-sign task family row, missing={sorted(missing)}")

    signed_subset["weighted_score"] = float(weighted_score)
    signed_subset["score_parts"] = dict(score_parts)
    signed_subset["rejection_reason"] = rejection_reason
    sig_b64 = base64.b64encode(signer._sk.sign(canonical_json(signed_subset))).decode("ascii")
    out = dict(row)
    out.update(signed_subset)
    out["cathedral_signature"] = sig_b64
    out["eval_output_schema_version"] = TASK_FAMILY_SCHEMA_VERSION
    return out


__all__ = [
    "TASK_FAMILY_SCHEMA_VERSION",
    "TASK_FAMILY_SCHEMA_VERSION_V6",
    "TASK_FAMILY_SIGNED_KEYS",
    "TASK_FAMILY_SIGNED_KEYS_V6",
    "build_signed_task_family_row",
    "canonical_hash",
    "public_task_id",
    "resign_task_family_score",
]
