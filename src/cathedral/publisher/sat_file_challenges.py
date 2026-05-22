"""Publisher helpers for file-backed SAT challenge rows."""

from __future__ import annotations

from pathlib import Path

from cathedral.lanes.challenge_ops import challenge_id_for_cnf_sha256
from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_PENDING,
    ChallengeRecord,
    ChallengeSourceError,
)
from cathedral.lanes.synthetic_boolean_v1 import FAMILY_ID as SYNTHETIC_BOOLEAN_FAMILY_ID
from cathedral.publisher.sat_file_verifier import (
    parse_dimacs_cnf_metadata_file,
    sha256_file,
    sha256_file_with_size,
)


def build_synthetic_boolean_file_challenge_record(
    *,
    cnf_path: str | Path,
    tier: int,
    challenge_id: str | None = None,
    status: str = CHALLENGE_STATUS_PENDING,
    source: str = "operator_cnf_path",
    max_bytes: int | None = None,
) -> ChallengeRecord:
    """Build a private SAT challenge row that points at a local CNF file."""
    expanded = Path(cnf_path).expanduser().resolve()
    parsed = parse_dimacs_cnf_metadata_file(expanded, max_bytes=max_bytes)
    if not parsed.ok:
        raise ChallengeSourceError(
            f"synthetic_boolean_v1 CNF is not valid DIMACS: {parsed.rejection_reason}"
        )
    try:
        digest, cnf_bytes = sha256_file_with_size(expanded, max_bytes=max_bytes)
    except OSError as exc:
        raise ChallengeSourceError("synthetic_boolean_v1 CNF could not be read") from exc
    except ValueError as exc:
        raise ChallengeSourceError(
            f"synthetic_boolean_v1 CNF is not valid DIMACS: {exc}"
        ) from exc
    audit_metadata = {
        "source": source,
        "storage": "file",
        "cnf_sha256": digest,
        "cnf_bytes": cnf_bytes,
        "num_vars": parsed.num_vars,
        "num_clauses": parsed.num_clauses,
    }
    if max_bytes is not None:
        audit_metadata["max_cnf_bytes"] = int(max_bytes)
    return ChallengeRecord(
        challenge_id=challenge_id or challenge_id_for_cnf_sha256(digest),
        family_id=SYNTHETIC_BOOLEAN_FAMILY_ID,
        tier=max(0, int(tier)),
        cnf_text="",
        cnf_path=str(expanded),
        status=status,
        audit_metadata=audit_metadata,
    )


__all__ = [
    "build_synthetic_boolean_file_challenge_record",
    "sha256_file",
]
