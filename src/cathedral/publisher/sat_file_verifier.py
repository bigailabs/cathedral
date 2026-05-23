"""Publisher-private file-backed SAT verifier helpers."""

from __future__ import annotations

import hashlib
import os
import stat
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


def sha256_file(path: str | Path, *, max_bytes: int | None = None) -> str:
    """Return the SHA-256 digest of a file without materializing it."""
    digest, _bytes_seen = sha256_file_with_size(path, max_bytes=max_bytes)
    return digest


def sha256_file_with_size(
    path: str | Path,
    *,
    max_bytes: int | None = None,
) -> tuple[str, int]:
    """Return ``(sha256, bytes_read)`` for one opened regular file."""
    digest = hashlib.sha256()
    expanded = Path(path).expanduser()
    with expanded.open("rb") as handle:
        _ensure_open_regular_file_within_limit(handle, max_bytes=max_bytes)
        bytes_seen = 0
        while True:
            chunk = handle.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            bytes_seen += len(chunk)
            if max_bytes is not None and bytes_seen > max_bytes:
                raise ValueError("cnf_oversized")
            digest.update(chunk)
    return digest.hexdigest(), bytes_seen


def parse_dimacs_cnf_metadata_file(
    path: str | Path,
    *,
    max_bytes: int | None = None,
) -> CnfMetadata:
    """Parse DIMACS CNF metadata from disk without materializing the full CNF."""
    expanded = Path(path).expanduser()
    try:
        with expanded.open("r", encoding="utf-8") as handle:
            _ensure_open_regular_file_within_limit(handle.buffer, max_bytes=max_bytes)
            scanned = _scan_dimacs_cnf_lines(handle, collect_clauses=False)
    except UnicodeDecodeError:
        return _empty_metadata("cnf_invalid_character")
    except ValueError as exc:
        return _empty_metadata(str(exc))
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
        state = _HashingReaderState(hashlib.sha256())
        with expanded.open("rb") as handle:
            # The announced challenge records the original CNF size (and,
            # when configured, the operator launch cap). Check the opened
            # descriptor before scanning so a replaced file cannot force the
            # publisher to hash/evaluate arbitrary bytes before returning a
            # digest mismatch.
            _ensure_open_regular_file_within_limit(handle, max_bytes=max_bytes)
            verification = _evaluate_cnf_streaming_lines(
                _hashing_utf8_lines(handle, state, max_bytes=max_bytes),
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
                if max_bytes is not None and state.bytes_seen + len(chunk) > max_bytes:
                    return DimacsVerification(False, False, "cnf_oversized")
                state.bytes_seen += len(chunk)
                state.digest.update(chunk)
        if expected_sha256 is not None and state.digest.hexdigest() != expected_sha256:
            return DimacsVerification(False, False, "cnf_hash_mismatch")
        return verification
    except UnicodeDecodeError:
        return DimacsVerification(False, False, "cnf_invalid_character")
    except ValueError as exc:
        return DimacsVerification(False, False, str(exc))
    except OSError:
        return DimacsVerification(False, False, "cnf_unreadable")


def _ensure_open_regular_file_within_limit(
    handle: BinaryIO,
    *,
    max_bytes: int | None,
) -> None:
    stat_result = os.fstat(handle.fileno())
    if not stat.S_ISREG(stat_result.st_mode):
        raise OSError("CNF path is not a regular file")
    if max_bytes is not None and stat_result.st_size > max_bytes:
        raise ValueError("cnf_oversized")


class _HashingReaderState:
    def __init__(self, digest: Any) -> None:
        self.digest = digest
        self.bytes_seen = 0


def _hashing_utf8_lines(
    handle: BinaryIO,
    state: _HashingReaderState,
    *,
    max_bytes: int | None,
) -> Any:
    for raw_line in handle:
        if max_bytes is not None and state.bytes_seen + len(raw_line) > max_bytes:
            raise ValueError("cnf_oversized")
        state.bytes_seen += len(raw_line)
        state.digest.update(raw_line)
        yield raw_line.decode("utf-8")


__all__ = [
    "parse_dimacs_cnf_metadata_file",
    "sha256_file",
    "sha256_file_with_size",
    "verify_dimacs_solution_file",
]
