"""Verification primitives — REUSED by lanes, never re-implemented per-lane.

  * verify_witness        SAT assignment check (REAL, dimacs.py).
  * verify_unsat_cert     DRAT/LRAT proof-cert check (STUB — shells to
                          drat-trim in real deployment; here it does a shape
                          check and reports stub=True honestly).
  * verify_attestation    binds an attested run to an expected image (MRTD)
                          via PolarisClient.attest — the /v1/attest seam.

The point of centralizing these: the three-outcome grade (grading.py) and all
three lanes call the SAME witness/cert/attest checks, so correctness lives in
one place.
"""
from __future__ import annotations

from dataclasses import dataclass

from .dimacs import verify_witness
from .polaris import AttestResult, PolarisClient

# re-export the SAT primitive under the verify namespace
__all__ = ["verify_witness", "verify_unsat_cert", "verify_attestation", "UnsatCheck"]


@dataclass
class UnsatCheck:
    ok: bool
    stub: bool
    reason: str


def verify_unsat_cert(cnf_text: str, drat_text: str) -> UnsatCheck:
    """Check an UNSAT proof certificate against the CNF.

    STUB: a real validator runs `drat-trim <cnf> <drat>` (or lrat-check) and
    trusts the verified UNSAT verdict. Wiring a vendored drat-trim here is the
    only thing standing between this and a real check; we do a conservative
    shape check and flag stub=True so nothing pretends a proof was verified.
    """
    if not drat_text.strip():
        return UnsatCheck(False, stub=False, reason="empty_cert")
    # shape: a DRAT proof is lines of integers, clause additions/deletions
    has_terminator = any(line.strip().endswith("0") for line in drat_text.splitlines())
    if not has_terminator:
        return UnsatCheck(False, stub=False, reason="malformed_drat")
    return UnsatCheck(True, stub=True, reason="shape_ok_drat_trim_not_run")


def verify_attestation(
    client: PolarisClient, *, nonce: str, pubkey_b64: str,
    expected_mrtd: str, workload: str,
) -> tuple[bool, AttestResult]:
    """An attested run is valid iff Intel verified the quote, report_data binds
    our nonce+pubkey, AND the launched image's MRTD equals the pinned digest."""
    res = client.attest(
        nonce=nonce, e2e_pubkey_b64=pubkey_b64,
        expected_mrtd=expected_mrtd, workload=workload,
    )
    ok = res.intel_verified and res.report_data_match and res.mrtd == expected_mrtd
    return ok, res
