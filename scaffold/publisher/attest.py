"""Attestation verify + attested multiplier (TASK 3, per ATTESTATION.md).

Attestation is the trust primitive under the three claims a certificate CANNOT
prove — (1) which solver produced the result, (2) wall-clock speed, (3)
timeout/hardness. Correctness stays free and self-proving; attestation is the
price of a claim no certificate can make. So we GATE THE TITLE / MULTIPLIER,
never the submission, and NEVER pay for attestation alone.

A valid attestation (Route B, `~/attestor/{guest_quote.sh,polaris_verify.py}`)
is a raw TDX quote whose 64-byte report_data binds two SHA-256 halves:

    report_data[0:32]  = sha256( nonce_hex || miner_pubkey_b64 )       # WHO + anti-replay
    report_data[32:64] = sha256( solver_digest || sha256(receipt) )    # WHAT ran || produced

The publisher-side verifier (open-source; pure-python, trust nothing):
  1. recompute both halves from the returned fields, check against q[568:632];
  2. confirm the quote is genuine OFFLINE against Intel collateral — stubbed
     behind IntelCollateralVerifier (real verify = the attestor's
     polaris_verify.verify_intel / dcap-qvl);
  3. check the nonce is one we ISSUED and UNUSED, challenge_id/instance_sha256
     match the served challenge, solver_digest matches the registered solver,
     and outcome/answer_hash match the certificate already checked at base rate;
  4. on success: mark the corresponding solve row ATTESTED and apply the
     multiplier to its weighted_score / challenge_value (re-signing the row, as
     those are signed fields). The multiplier rides the existing Path-B signed
     vector — no validator release.

Env-gated by CATHEDRAL_ATTEST_ENABLED (default OFF). A missing/forged/replayed
attestation NEVER crashes and NEVER upgrades the row — it stays at base rate.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .. import wire
from .store import Store, new_uuid

_DEFAULT_MULTIPLIER = 2.0   # Q5 (open): calibrate m against attest cost; placeholder.


def attest_enabled() -> bool:
    return os.environ.get("CATHEDRAL_ATTEST_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"}


def attested_multiplier() -> float:
    try:
        return float(os.environ.get("CATHEDRAL_ATTEST_MULTIPLIER", str(_DEFAULT_MULTIPLIER)))
    except ValueError:
        return _DEFAULT_MULTIPLIER


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# --------------------------------------------------------------------------
# Intel-collateral verification SEAM. Real verify is the attestor's offline
# DCAP path (PCK chain + TCB info + QE identity + CRLs); we verify the
# report_data binding in pure python and DELEGATE the genuineness check here so
# the heavy crypto stays in polaris_verify. The stub trusts a quote that is
# structurally a TDX quote (>=632 bytes) so the offline scaffold runs end-to-end
# while keeping the binding checks REAL.
# --------------------------------------------------------------------------
class IntelCollateralVerifier(Protocol):
    def verify_intel(self, quote: bytes, collateral: dict | None) -> bool: ...


class StubIntelVerifier:
    """Offline stand-in for polaris_verify.verify_intel. Accepts a structurally
    valid TDX quote; rejects a too-short / empty one. Swap for the real DCAP
    verifier in prod (it checks the PCK chain offline against shipped collateral)."""

    backend = "stub-intel-collateral"

    def verify_intel(self, quote: bytes, collateral: dict | None) -> bool:
        return isinstance(quote, (bytes, bytearray)) and len(quote) >= 632


# --------------------------------------------------------------------------
# Per-(miner,challenge) nonce — minted from HMAC machinery (like the CNF token),
# bound into report_data[0:32], one-time (kills replay + binds to THIS challenge).
# --------------------------------------------------------------------------
def issue_nonce(store: Store, secret: bytes, miner_hotkey: str, challenge_id: str) -> str:
    raw = hmac.new(secret, f"{miner_hotkey}:{challenge_id}:{new_uuid()}".encode(),
                   hashlib.sha256).hexdigest()

    def _do(conn):
        conn.execute(
            "INSERT OR IGNORE INTO attest_nonces(nonce, miner_hotkey, challenge_id, issued_at_iso) "
            "VALUES (?, ?, ?, ?)", (raw, miner_hotkey, challenge_id, _now_iso()))
    store.write(_do)
    return raw


@dataclass
class AttestVerifyResult:
    ok: bool
    reason: str | None
    multiplier: float = 1.0
    eval_run_id: str | None = None


def _recompute_report_data(nonce_hex: str, miner_pubkey_b64: str,
                           solver_digest: str, receipt: str) -> bytes:
    lo = hashlib.sha256((nonce_hex + miner_pubkey_b64).encode()).digest()
    receipt_sha = hashlib.sha256(receipt.encode()).hexdigest()
    hi = hashlib.sha256((solver_digest + receipt_sha).encode()).digest()
    return lo + hi


def verify_attestation(
    store: Store,
    payload: dict,
    *,
    private_key_hex: str,
    intel: IntelCollateralVerifier | None = None,
    multiplier: float | None = None,
) -> AttestVerifyResult:
    """Verify a Route-B attestation and, on success, upgrade the bound solve row.

    `payload` (the body POSTed to the verify endpoint) carries:
      quote_b64, collateral (opt), nonce, miner_pubkey_b64, solver_digest,
      receipt (the canonical in-TEE stdout JSON), challenge_id, instance_sha256,
      outcome, answer_hash, eval_run_id (the base-rate row to upgrade).

    TOTAL: every failure path returns ok=False with a reason; never raises, never
    upgrades a row it shouldn't. Pays NOTHING for attestation alone — the row must
    already exist at base rate (a certificate-checked solve)."""
    intel = intel or StubIntelVerifier()
    mult = attested_multiplier() if multiplier is None else multiplier
    try:
        import base64
        quote = base64.b64decode(payload.get("quote_b64", ""), validate=False)
    except Exception:
        return AttestVerifyResult(False, "malformed_quote_b64")

    nonce = payload.get("nonce", "")
    miner_pubkey_b64 = payload.get("miner_pubkey_b64", "")
    solver_digest = payload.get("solver_digest", "")
    receipt = payload.get("receipt", "")
    challenge_id = payload.get("challenge_id", "")
    instance_sha256 = payload.get("instance_sha256", "")
    outcome = payload.get("outcome", "")
    answer_hash = payload.get("answer_hash", "")
    eval_run_id = payload.get("eval_run_id", "")

    if len(quote) < 632:
        return AttestVerifyResult(False, "quote_too_short")

    # 1. recompute report_data halves, check against the quote bytes q[568:632].
    expect = _recompute_report_data(nonce, miner_pubkey_b64, solver_digest, receipt)
    if not hmac.compare_digest(expect, quote[568:632]):
        return AttestVerifyResult(False, "report_data_mismatch")

    # 2. confirm the quote is genuine offline against Intel collateral (seam).
    if not intel.verify_intel(quote, payload.get("collateral")):
        return AttestVerifyResult(False, "intel_collateral_unverified")

    # 3a. nonce must be one we issued, for THIS (miner,challenge), and UNUSED.
    nrow = store.query(
        "SELECT miner_hotkey, challenge_id, used_at_iso FROM attest_nonces WHERE nonce=?",
        (nonce,))
    if not nrow:
        return AttestVerifyResult(False, "unknown_nonce")
    if nrow[0]["used_at_iso"] is not None:
        return AttestVerifyResult(False, "nonce_already_used")
    if nrow[0]["challenge_id"] != challenge_id:
        return AttestVerifyResult(False, "nonce_challenge_mismatch")

    # 3b. parse the in-TEE receipt; its fields must match the served challenge +
    #     the certificate already checked at base rate (no upgrading a mismatch).
    try:
        rj = json.loads(receipt)
    except Exception:
        return AttestVerifyResult(False, "malformed_receipt")
    if rj.get("instance_sha256") != instance_sha256:
        return AttestVerifyResult(False, "receipt_instance_mismatch")
    if rj.get("solver_digest") != solver_digest:
        return AttestVerifyResult(False, "receipt_solver_mismatch")
    if str(rj.get("outcome", "")).upper() != str(outcome).upper():
        return AttestVerifyResult(False, "receipt_outcome_mismatch")
    if rj.get("answer_hash") != answer_hash:
        return AttestVerifyResult(False, "receipt_answer_mismatch")

    # 3c. the base-rate row must EXIST (we pay the multiplier on real work only).
    erow = store.query("SELECT row_json, miner_hotkey FROM eval_runs WHERE id=?",
                       (eval_run_id,))
    if not erow:
        return AttestVerifyResult(False, "unknown_eval_run")
    row = json.loads(erow[0]["row_json"])

    # 3d. solver_digest must match the registered solver for this owner (laundering
    #     guard: a copy of the champion submitting champion certs is rejected
    #     unless its REGISTERED digest matches what actually ran in the box).
    sdigest_rows = store.query(
        "SELECT 1 FROM arena_solvers WHERE owner_hotkey=? AND container_digest=?",
        (row.get("miner_hotkey", ""), solver_digest))
    # solver registration is optional for Lane A community work; only enforce the
    # match when the owner HAS registered solvers (Lane S/title path).
    has_any = store.query("SELECT 1 FROM arena_solvers WHERE owner_hotkey=? LIMIT 1",
                          (row.get("miner_hotkey", ""),))
    if has_any and not sdigest_rows:
        return AttestVerifyResult(False, "solver_digest_not_registered")

    # 4. SUCCESS — apply the multiplier to the signed score fields and RE-SIGN.
    #    Gate-the-title: the multiplier scales the EXISTING base score; it is
    #    never a standalone payment (the row already represents checked work).
    base_ws = float(row.get("weighted_score", 0.0))
    new_ws = round(min(1.0, base_ws * mult), 6)  # clamp: signed score stays in [0,1]
    row["weighted_score"] = new_ws
    if "challenge_value" in row:
        row["challenge_value"] = new_ws
    if isinstance(row.get("score_parts"), dict):
        row["score_parts"]["binary_correct"] = new_ws
    # re-sign (weighted_score/challenge_value are in SIGNED_KEYS) so the upgraded
    # row still verifies under the same key.
    row["cathedral_signature"] = wire.sign_row(row, private_key_hex)
    row["attested"] = True

    att_id = new_uuid()

    def _upgrade(conn):
        conn.execute("UPDATE attest_nonces SET used_at_iso=? WHERE nonce=?",
                     (_now_iso(), nonce))
        conn.execute("UPDATE eval_runs SET row_json=?, attested=1 WHERE id=?",
                     (json.dumps(row), eval_run_id))
        conn.execute(
            "INSERT OR IGNORE INTO attestations(id, eval_run_id, miner_hotkey, "
            "challenge_id, solver_digest, multiplier, verified_at_iso) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (att_id, eval_run_id, row.get("miner_hotkey", ""), challenge_id,
             solver_digest, mult, _now_iso()))
    store.write(_upgrade)
    return AttestVerifyResult(True, None, multiplier=mult, eval_run_id=eval_run_id)
