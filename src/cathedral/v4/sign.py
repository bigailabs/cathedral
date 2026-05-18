"""Sign a v4 wire row.

v4 publisher rows are signed by the same ``EvalSigner`` instance the
publisher uses for v3 (see ``cathedral.v3.sign``). We do NOT
reinvent signing: the goal is one signer object, one pubkey pinned on
chain, all wire rows going through the same canonical-JSON +
Ed25519-signature pipeline.

This module:

  * Builds the v4 signed subset (a frozen keyset, mirrored on the
    validator side by a test we will add when v4 wires into the
    pull loop).
  * Canonicalizes via the same ``cathedral.v1_types.canonical_json``
    used by v3.
  * Signs with the supplied ``EvalSigner`` (typed as ``Any`` to keep
    this module decoupled from the publisher's eval package; same
    pattern as ``cathedral.v3.sign``).
  * Returns a wire-shape dict ready to persist into the publisher DB
    and serve from a future ``/v4/leaderboard/recent`` (out of scope
    for this PR -- feed stays off).

**The validator never imports this module.** Validators only verify
already-signed rows. See ``cathedral.v4.verify`` for the validator
side.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from cathedral.v4.schemas import ValidationPayload

# v4 signed keyset. Tests pin this against the validator-side mirror
# (to be added when v4 wires into the pull loop in a follow-up PR).
_V4_SIGNED_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "miner_hotkey",
        "task_type",
        "task_id",
        "difficulty_tier",
        "language",
        "injected_fault_type",
        "weighted_score",
        "outcome",
        "total_turns",
        "deterministic_hash",
        "ran_at",
    }
)


def _canonical_json(obj: dict[str, Any]) -> bytes:
    """Stable JSON byte encoding used for signing.

    Matches the convention in ``cathedral.v1_types.canonical_json``
    (sorted keys, no spaces). We re-implement locally rather than
    import v1_types so that v4 can be unit-tested without the v1
    publisher import chain.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_signed_v4_row(
    *,
    eval_run_id: str,
    miner_hotkey: str,
    payload: ValidationPayload,
    weighted_score: float,
    outcome: str,
    total_turns: int,
    ran_at_iso: str,
    signer: Any,
) -> dict[str, Any]:
    """Assemble + sign a v4 wire row.

    ``signer`` is the existing publisher ``EvalSigner`` instance
    (typed as ``Any`` to keep this module decoupled from the
    publisher's eval package -- same pattern as ``cathedral.v3.sign``).

    Returns a dict with the signed subset + ``cathedral_signature`` +
    ``eval_output_schema_version: 4``. Caller is responsible for
    persisting to the publisher DB.
    """
    signed_subset: dict[str, Any] = {
        "id": eval_run_id,
        "miner_hotkey": miner_hotkey,
        "task_type": "v4_patch",
        "task_id": payload.task_id,
        "difficulty_tier": payload.difficulty_tier,
        "language": payload.language,
        "injected_fault_type": payload.injected_fault_type,
        "weighted_score": float(weighted_score),
        "outcome": outcome,
        "total_turns": int(total_turns),
        "deterministic_hash": payload.deterministic_hash,
        "ran_at": ran_at_iso,
    }

    # Sanity: every key in the signed subset must be in the v4 keyset.
    extra = set(signed_subset.keys()) - set(_V4_SIGNED_KEYS)
    missing = set(_V4_SIGNED_KEYS) - set(signed_subset.keys())
    if extra or missing:
        raise RuntimeError(
            f"v4 signed subset diverged from keyset: extra={sorted(extra)} "
            f"missing={sorted(missing)}"
        )

    payload_bytes = _canonical_json(signed_subset)
    # Same private-attr access pattern as v3.sign; tracked there for
    # follow-up refactor to a public sign_bytes() method. NaCl's
    # SigningKey.sign() returns a SignedMessage; the validator
    # verifies the *detached* 64-byte signature, so we extract
    # ``.signature`` here.
    signed = signer._sk.sign(payload_bytes)
    sig_bytes = getattr(signed, "signature", signed[:64])
    sig_b64 = base64.b64encode(sig_bytes).decode("ascii")

    row = dict(signed_subset)
    row["cathedral_signature"] = sig_b64
    row["eval_output_schema_version"] = 4
    return row


__all__ = [
    "build_signed_v4_row",
]
