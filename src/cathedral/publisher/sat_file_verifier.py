"""Publisher-private file-backed SAT verifier helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, BinaryIO

from cathedral.lanes.synthetic_boolean_v1.dimacs import (
    CnfMetadata,
    DimacsVerification,
    _empty_metadata,
    _evaluate_cnf_streaming_lines,
    _parse_solution_bits,
    _scan_dimacs_cnf_lines,
)

_READ_CHUNK_BYTES = 1024 * 1024


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 digest of a file without materializing it."""
    digest = hashlib.sha256()
    expanded = Path(path).expanduser()
    with expanded.open("rb") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def parse_dimacs_cnf_metadata_file(
    path: str | Path,
    *,
    max_bytes: int | None = None,
) -> CnfMetadata:
    """Parse DIMACS CNF metadata from disk without materializing the full CNF."""
    expanded = Path(path).expanduser()
    try:
        if max_bytes is not None and expanded.stat().st_size > max_bytes:
            return _empty_metadata("cnf_oversized")
        with expanded.open("r", encoding="utf-8") as handle:
            scanned = _scan_dimacs_cnf_lines(handle, collect_clauses=False)
    except UnicodeDecodeError:
        return _empty_metadata("cnf_invalid_character")
    except OSError:
        return _empty_metadata("cnf_unreadable")
    assert isinstance(scanned, CnfMetadata)
    return scanned


def verify_dimacs_solution_file(
    cnf_path: str | Path,
    solution_text: str,
    *,
    max_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> DimacsVerification:
    """Verify a solver answer against a file-backed CNF without loading the CNF."""
    metadata = parse_dimacs_cnf_metadata_file(cnf_path, max_bytes=max_bytes)
    if not metadata.ok:
        return DimacsVerification(False, False, metadata.rejection_reason or "cnf_unparseable")

    reason, status, assignment = _parse_solution_bits(solution_text, metadata.num_vars)
    if reason is not None:
        return DimacsVerification(
            False,
            False,
            reason,
            status=status,
            num_vars=metadata.num_vars,
            assigned=assignment.assigned_count if assignment is not None else 0,
            clause_count=metadata.num_clauses,
        )
    assert assignment is not None
    expanded = Path(cnf_path).expanduser()
    try:
        digest = hashlib.sha256()
        with expanded.open("rb") as handle:
            verification = _evaluate_cnf_streaming_lines(
                _hashing_utf8_lines(handle, digest),
                metadata,
                assignment,
            )
            # The evaluator may return early (for example on the first
            # unsatisfied clause). Finish hashing the same opened file before
            # accepting or rejecting the miner answer so scoring is bound to
            # the announced CNF digest.
            while True:
                chunk = handle.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            return DimacsVerification(False, False, "cnf_hash_mismatch")
        return verification
    except UnicodeDecodeError:
        return DimacsVerification(False, False, "cnf_invalid_character")
    except OSError:
        return DimacsVerification(False, False, "cnf_unreadable")


def _hashing_utf8_lines(handle: BinaryIO, digest: Any) -> Any:
    for raw_line in handle:
        digest.update(raw_line)
        yield raw_line.decode("utf-8")


__all__ = [
    "parse_dimacs_cnf_metadata_file",
    "sha256_file",
    "verify_dimacs_solution_file",
]
