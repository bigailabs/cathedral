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

import hashlib
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
    expected_image: str, workload: str,
) -> tuple[bool, AttestResult]:
    """An attested run is valid iff (verified against the real /v1/attest recipe,
    measured 2026-06-04):

        intel_verified                                                    AND
        report_data[0:32]  == sha256(nonce || pubkey)                     AND
        report_data[32:64] == sha256(image_digest || sha256hex(stdout))   AND
        the image the box ran is the one Lane B pinned.

    The MRTD is NOT checked — it measures the base VM (invariant across
    workloads, proven), not the image. Image identity rides on report_data[32:64].
    """
    res = client.attest(nonce=nonce, e2e_pubkey_b64=pubkey_b64,
                         image=expected_image, workload=workload)
    rd = res.report_data
    if len(rd) != 64:
        return False, res
    lo_ok = rd[0:32] == hashlib.sha256((nonce + pubkey_b64).encode()).digest()
    res_hex = hashlib.sha256(res.stdout.encode()).hexdigest()
    hi_ok = rd[32:64] == hashlib.sha256((res.image_digest + res_hex).encode()).digest()
    # the box ran the image Lane B pinned. Pinning is by CONTENT DIGEST (a tag is
    # mutable): if the miner committed a ref carrying a 64-hex digest it MUST
    # equal the bound one; a bare tag is accepted and the resolved digest is
    # recorded (the box must have run some image -> got_hex non-empty).
    import re
    def _hex(s: str) -> str:
        m = re.search(r"[a-f0-9]{64}", s or "")
        return m.group(0) if m else ""
    exp_hex, got_hex = _hex(expected_image), _hex(res.image_digest)
    img_ok = bool(got_hex) and (exp_hex == "" or exp_hex == got_hex)
    ok = res.intel_verified and lo_ok and hi_ok and img_ok
    return ok, res
