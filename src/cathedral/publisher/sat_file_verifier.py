"""Publisher-private file-backed SAT verifier helpers."""

from __future__ import annotations

from pathlib import Path

from cathedral.lanes.synthetic_boolean_v1.dimacs import (
    CnfMetadata,
    DimacsVerification,
    _empty_metadata,
    _evaluate_cnf_streaming_lines,
    _parse_solution_bits,
    _scan_dimacs_cnf_lines,
)


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
        with expanded.open("r", encoding="utf-8") as handle:
            return _evaluate_cnf_streaming_lines(handle, metadata, assignment)
    except UnicodeDecodeError:
        return DimacsVerification(False, False, "cnf_invalid_character")
    except OSError:
        return DimacsVerification(False, False, "cnf_unreadable")


__all__ = [
    "parse_dimacs_cnf_metadata_file",
    "verify_dimacs_solution_file",
]
